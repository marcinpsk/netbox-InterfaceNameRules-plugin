# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Discover and plan installed interface families."""

import logging
import re

from dcim.models import Interface
from django.db import DEFAULT_DB_ALIAS

from ..choices import BreakoutModeChoices
from ..naming import evaluate_name_template
from .domain import (
    FamilyTopology,
    InstalledFamilyPlan,
    InstalledFamilyPlanSet,
    InterfaceSnapshot,
    MemberRole,
    PlannedMember,
)
from .template_names import resolved_template_names

logger = logging.getLogger(__name__)

_BASE_SENTINEL = "InrBaseSentinelEnd"


def _is_plain_interface(interface) -> bool:
    """Return whether *interface* can be a flat-family member."""
    return (
        getattr(interface, "parent_id", None) is None
        and getattr(interface, "channel_id", None) is None
        and getattr(interface, "channels", None) is None
    )


def _is_channelized_parent(interface) -> bool:  # pragma: no cover - requires channelization support
    """Return whether *interface* declares a channelized family."""
    return getattr(interface, "channels", None) is not None


def _is_channel(interface) -> bool:  # pragma: no cover - requires channelization support
    """Return whether *interface* is bound to a parent channel."""
    return getattr(interface, "channel_id", None) is not None


def _child_name_suffix(child_name, parent_name):  # pragma: no cover - requires channelization support
    """Return the non-alphanumeric suffix that *child_name* adds to its parent."""
    if not parent_name or not child_name.startswith(parent_name):
        return None
    suffix = child_name[len(parent_name) :]
    if not suffix or suffix[0].isalnum():
        return None
    return suffix


def _template_channel_suffixes(templates):  # pragma: no cover - requires channelization support
    """Return the possible resolved suffixes for each template channel."""
    parents = {template.pk: template.resolved for template in templates if template.channels is not None}
    suffixes: dict[int, set[str]] = {}
    for template in templates:
        if template.channel_id is None or template.parent_id not in parents:
            continue
        suffix = _child_name_suffix(template.resolved, parents[template.parent_id])
        if suffix is not None:
            suffixes.setdefault(template.channel_id, set()).add(suffix)
    return suffixes


def _simple_child_target(child, parent_name, parent_target, suffixes):  # pragma: no cover - channelization only
    """Return a simple rule's child target or the reason it cannot be derived."""
    suffix = _child_name_suffix(child.name, parent_name)
    if suffix is None:
        candidates = suffixes.get(child.channel_id, set())
        if len(candidates) == 1:
            suffix = next(iter(candidates))
    if suffix is None:
        return None, "channel suffix is ambiguous or unavailable"
    return parent_target + suffix, ""


def _flat_names(rule, variables, base_name):
    """Return the names of the flat family that *rule* defines on *base_name*."""
    family_variables = {**variables, "base": base_name}
    return tuple(
        evaluate_name_template(
            rule.name_template,
            {**family_variables, "channel": str(rule.channel_start + offset)},
        )
        for offset in range(rule.channel_count)
    )


def _historical_bases(rule, variables, template, interfaces):  # pragma: no cover - requires VC token support
    """Return every historical base claimed by *template*."""
    if template.historical_pattern is None:
        return ()
    try:
        marked = evaluate_name_template(
            rule.name_template,
            {**variables, "base": _BASE_SENTINEL, "channel": str(rule.channel_start)},
        )
    except (TypeError, ValueError):
        return ()
    if _BASE_SENTINEL not in marked:
        return ()
    escaped = re.escape(marked)
    head, _, tail = escaped.partition(_BASE_SENTINEL)
    tail = tail.replace(_BASE_SENTINEL, "(?P=base)")
    pattern = re.compile(f"{head}(?P<base>{template.historical_pattern.pattern}){tail}")
    bases = {
        match.group("base") for interface in interfaces if (match := pattern.fullmatch(interface.name)) is not None
    }
    return tuple(sorted(bases))


def _flat_candidates(rule, variables, interfaces, templates):
    """Return complete, unambiguous flat-family candidates for *module*."""
    if rule.channel_count <= 0 or rule.breakout_mode != BreakoutModeChoices.FLAT:
        return []

    by_name = {interface.name: interface for interface in interfaces if _is_plain_interface(interface)}
    historical_by_template = {
        template.pk: _historical_bases(rule, variables, template, interfaces) for template in templates
    }
    ambiguous_bases = {base_name for bases in historical_by_template.values() if len(bases) > 1 for base_name in bases}
    single_claims: dict[str, int] = {}
    for bases in historical_by_template.values():
        if len(bases) == 1:  # pragma: no cover - historical matchers require VC token support
            single_claims[bases[0]] = single_claims.get(bases[0], 0) + 1
    ambiguous_bases.update(base_name for base_name, count in single_claims.items() if count > 1)

    candidates = []
    for template in templates:
        target_names = _flat_names(rule, variables, template.resolved)
        historical_bases = tuple(
            base_name
            for base_name in historical_by_template[template.pk]
            if len(historical_by_template[template.pk]) == 1 and base_name not in ambiguous_bases
        )
        source_bases = (template.resolved, *historical_bases)
        for source_base in source_bases:
            source_names = _flat_names(rule, variables, source_base)
            if len(set(source_names)) != len(source_names) or not all(name in by_name for name in source_names):
                continue
            members = tuple(by_name[name] for name in source_names)
            candidate = (template.resolved, target_names, members)
            if candidate not in candidates:  # pragma: no branch - duplicates require historical matchers
                candidates.append(candidate)

    claims: dict[int, int] = {}
    for _base_name, _names, members in candidates:
        for member in members:
            claims[member.pk] = claims.get(member.pk, 0) + 1
    return [candidate for candidate in candidates if all(claims[member.pk] == 1 for member in candidate[2])]


def _flat_plan(module, db_alias, base_name, target_names, interfaces):
    """Build one immutable plan from a complete flat-family candidate."""
    members = tuple(
        PlannedMember(
            snapshot=InterfaceSnapshot.from_interface(interface),
            target_name=target_name,
            role=MemberRole.FLAT_MEMBER,
        )
        for interface, target_name in zip(interfaces, target_names, strict=True)
    )
    return InstalledFamilyPlan(
        family_id=f"flat:{members[0].snapshot.pk}",
        topology=FamilyTopology.FLAT,
        device_id=module.device_id,
        module_id=module.pk,
        db_alias=db_alias,
        members=members,
    )


def _channelized_plan(module, rule, variables, db_alias, parent, children, suffixes):  # pragma: no cover
    """Build one plan for an existing channelized family."""
    blocked_reason = ""
    parent_target = parent.name
    child_targets = []
    if rule.channel_count > 0:
        if parent.channels != rule.channel_count:
            blocked_reason = (
                f"installed parent declares {parent.channels} channels but the rule defines {rule.channel_count}"
            )
            child_targets = [(child.name, blocked_reason) for child in children]
        else:
            if rule.breakout_mode == BreakoutModeChoices.CHANNELIZED and rule.parent_name_template:
                parent_target = evaluate_name_template(
                    rule.parent_name_template,
                    {**variables, "base": parent.name},
                )
            child_targets = [
                (
                    evaluate_name_template(
                        rule.name_template,
                        {
                            **variables,
                            "base": parent.name,
                            "channel": str(rule.channel_start + child.channel_id - 1),
                        },
                    ),
                    "",
                )
                for child in children
            ]
    else:
        parent_target = evaluate_name_template(rule.name_template, {**variables, "base": parent.name})
        child_targets = [_simple_child_target(child, parent.name, parent_target, suffixes) for child in children]

    members = [
        PlannedMember(
            snapshot=InterfaceSnapshot.from_interface(parent),
            target_name=parent_target,
            role=MemberRole.PARENT,
        )
    ]
    members.extend(
        PlannedMember(
            snapshot=InterfaceSnapshot.from_interface(child),
            target_name=target,
            role=MemberRole.CHANNEL,
            reason=reason,
        )
        for child, (target, reason) in zip(children, child_targets, strict=True)
    )
    return InstalledFamilyPlan(
        family_id=f"channelized:{parent.pk}",
        topology=FamilyTopology.CHANNELIZED,
        device_id=module.device_id,
        module_id=module.pk,
        db_alias=db_alias,
        members=tuple(members),
        parent_pk=parent.pk,
        blocked_reason=blocked_reason,
    )


def _channelized_plans(module, rule, variables, db_alias, interfaces, templates):  # pragma: no cover
    """Return one plan for every structurally discovered channelized family."""
    parents = [interface for interface in interfaces if _is_channelized_parent(interface)]
    children_by_parent: dict[int, list] = {}
    for interface in interfaces:
        if _is_channel(interface) and interface.parent_id is not None:
            children_by_parent.setdefault(interface.parent_id, []).append(interface)
    suffixes = _template_channel_suffixes(templates)
    plans = []
    for parent in parents:
        children = children_by_parent.get(parent.pk, [])
        children.sort(key=lambda child: (child.channel_id, child.pk))
        plans.append(_channelized_plan(module, rule, variables, db_alias, parent, children, suffixes))
    return plans


def plan_installed_families(module, rule, variables) -> InstalledFamilyPlanSet:
    """Return immutable plans for the installed families owned by *module*."""
    db_alias = module._state.db or DEFAULT_DB_ALIAS
    interfaces = list(Interface.objects.using(db_alias).filter(module_id=module.pk).order_by("pk"))
    templates = resolved_template_names(module)
    plans = _channelized_plans(module, rule, variables, db_alias, interfaces, templates)
    plans.extend(
        _flat_plan(module, db_alias, base_name, target_names, members)
        for base_name, target_names, members in _flat_candidates(rule, variables, interfaces, templates)
    )
    plans.sort(key=lambda plan: plan.member_pks[0])
    return InstalledFamilyPlanSet(module_id=module.pk, plans=tuple(plans))
