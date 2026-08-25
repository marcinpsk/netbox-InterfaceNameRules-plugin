# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Compute the names a rule intends for one interface family.

Every planner names a family through this module, so an installed plan, a prospective plan and a
structural plan cannot spell the same family differently.  Nothing here reads or writes a row: the
inputs are names, channel identifiers and the rule.
"""

from dataclasses import dataclass

from ..choices import BreakoutModeChoices
from ..naming import evaluate_name_template
from .domain import FamilyStatus

AMBIGUOUS_SUFFIX_REASON = "channel suffix is ambiguous or unavailable"


@dataclass(frozen=True, slots=True)
class FamilyTargets:
    """The names a rule intends for one channelized family, and why it may refuse."""

    parent_name: str
    channels: tuple[tuple[str, str], ...]
    status: FamilyStatus | None = None
    reason: str = ""


def child_name_suffix(child_name, parent_name):  # pragma: no cover - requires channelization support
    """Return the suffix *child_name* adds to *parent_name*, or None when it adds none.

    The first character must be non-alphanumeric so ``et0``/``et01`` is never mistaken for a
    family; the punctuation itself is free-form (``:``, ``-``, ``_`` and ``@`` all occur in the
    wild), so it is not restricted to a fixed separator.
    """
    if not parent_name or not child_name.startswith(parent_name):
        return None
    suffix = child_name[len(parent_name) :]
    if not suffix or suffix[0].isalnum():
        return None
    return suffix


def template_channel_suffixes(templates):  # pragma: no cover - requires channelization support
    """Map each channel identifier to the name suffixes the module type's templates give it.

    The suffix comes from the template family itself, so a child that lost its parent's prefix in
    an earlier partial rename can still be repaired.  A module type with several families may spell
    the same channel differently in each (``et0:2`` vs ``sw0.2``), so the suffixes are collected per
    channel rather than overwritten: recovery only uses one when every family agrees on it.
    """
    parents = {template.pk: template.resolved for template in templates if template.channels is not None}
    suffixes: dict[int, set[str]] = {}
    for template in templates:
        if template.channel_id is None or template.parent_id not in parents:
            continue
        suffix = child_name_suffix(template.resolved, parents[template.parent_id])
        if suffix is not None:
            suffixes.setdefault(template.channel_id, set()).add(suffix)
    return suffixes


def flat_family_names(rule, variables, base_name):
    """Return the names of the flat sibling family that *rule* defines on *base_name*."""
    family_variables = {**variables, "base": base_name}
    return tuple(
        evaluate_name_template(
            rule.name_template,
            {**family_variables, "channel": str(rule.channel_start + offset)},
        )
        for offset in range(rule.channel_count)
    )


def channelized_family_names(rule, base_name, variables):  # pragma: no cover - channelization only
    """Return ``(parent_name, ((channel_id, name), ...))`` for the family *rule* builds on *base_name*.

    ``{base}`` is the base interface's current name for the parent and every channel; ``{channel}``
    is ``channel_start + channel_id - 1``.  A blank parent template leaves the base's name alone.
    Takes the name rather than the interface so prediction can reuse it without a row to point at.
    """
    family_variables = {**variables, "base": base_name}
    parent_name = base_name
    if rule.parent_name_template:
        parent_name = evaluate_name_template(rule.parent_name_template, family_variables)
    channels = tuple(
        (
            channel_id,
            evaluate_name_template(
                rule.name_template, {**family_variables, "channel": str(rule.channel_start + channel_id - 1)}
            ),
        )
        for channel_id in range(1, rule.channel_count + 1)
    )
    return parent_name, channels


def _simple_child_target(child_name, channel_id, parent_name, parent_target, suffixes):  # pragma: no cover
    """Return a simple rule's child target, or None plus the reason it cannot be derived."""
    suffix = child_name_suffix(child_name, parent_name)
    if suffix is None:
        candidates = suffixes.get(channel_id, set())
        if len(candidates) == 1:
            suffix = next(iter(candidates))
    if suffix is None:
        return None, AMBIGUOUS_SUFFIX_REASON
    return parent_target + suffix, ""


def _failed(parent_name, children, error):  # pragma: no cover - requires channelization support
    """Return targets that leave the whole family alone because a template could not be evaluated."""
    reason = f"failed to evaluate family targets: {error}"
    return FamilyTargets(
        parent_name=parent_name,
        channels=tuple((child_name, reason) for child_name, _channel_id in children),
        status=FamilyStatus.FAILED,
        reason=reason,
    )


def _breakout_targets(rule, variables, parent_name, parent_channels, children):  # pragma: no cover
    """Return the names a breakout rule intends for an existing channelized family."""
    if parent_channels != rule.channel_count:
        reason = f"installed parent declares {parent_channels} channels but the rule defines {rule.channel_count}"
        return FamilyTargets(
            parent_name=parent_name,
            channels=tuple((child_name, reason) for child_name, _channel_id in children),
            status=FamilyStatus.BLOCKED,
            reason=reason,
        )
    try:
        parent_target = parent_name
        if rule.breakout_mode == BreakoutModeChoices.CHANNELIZED and rule.parent_name_template:
            parent_target = evaluate_name_template(rule.parent_name_template, {**variables, "base": parent_name})
        channels = tuple(
            (
                evaluate_name_template(
                    rule.name_template,
                    {**variables, "base": parent_name, "channel": str(rule.channel_start + channel_id - 1)},
                ),
                "",
            )
            for _child_name, channel_id in children
        )
    except (TypeError, ValueError) as error:
        return _failed(parent_name, children, error)
    return FamilyTargets(parent_name=parent_target, channels=channels)


def _lockstep_targets(rule, variables, parent_name, children, suffixes):  # pragma: no cover
    """Return the names a simple rule intends for a family renamed in lockstep with its parent."""
    try:
        parent_target = evaluate_name_template(rule.name_template, {**variables, "base": parent_name})
    except (TypeError, ValueError) as error:
        return _failed(parent_name, children, error)
    channels = tuple(
        _simple_child_target(child_name, channel_id, parent_name, parent_target, suffixes)
        for child_name, channel_id in children
    )
    return FamilyTargets(parent_name=parent_target, channels=channels)


def channelized_family_targets(  # pragma: no cover - requires channelization support
    rule, variables, parent_name, parent_channels, children, suffixes
) -> FamilyTargets:
    """Return the names *rule* intends for the channelized family named *parent_name*.

    *children* pairs each channel's current name with its channel identifier, in channel order.
    A breakout rule renames the channels it describes; any other rule carries the family along with
    its parent, keeping each channel's own suffix.
    """
    if rule.channel_count > 0:
        return _breakout_targets(rule, variables, parent_name, parent_channels, children)
    return _lockstep_targets(rule, variables, parent_name, children, suffixes)
