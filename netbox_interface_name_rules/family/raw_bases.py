# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Recover the raw template name a module rule reads as ``{base}``.

NetBox keeps no link from an interface to the template that created it, so a rule that reads
``{base}`` claims each live name for the template whose raw name it is, or whose raw name the rule
renames to it. A name that no single template claims has no base, and nothing renames it.
"""

import logging
import re

from ..choices import BreakoutModeChoices
from ..name_template import evaluate_name_template, references_variable
from .claims import TemplateClaim, resolve_template_claims
from .targets import builds_channelized_family
from .template_names import VC_POSITION_DIGITS

logger = logging.getLogger(__name__)

# PostgreSQL text cannot hold NUL, so no stored name, template or value can spell these markers.
BASE_MARKER = "\x00base\x00"
_MARKERS = {"base": BASE_MARKER, "vc_position": "\x00vc_position\x00"}


def rule_reads_base(rule) -> bool:
    """Return whether any of *rule*'s name templates substitutes ``{base}``."""
    return any(references_variable(template, "base") for template in (rule.name_template, rule.parent_name_template))


def _renaming_templates(rule):
    """Return the templates that give a family's base interface its name under *rule*."""
    if rule.channel_count <= 0:
        return (rule.name_template,)
    if rule.breakout_mode == BreakoutModeChoices.CHANNELIZED and rule.parent_name_template:
        return (rule.parent_name_template,)
    return ()


def _parent_name_needs_channels(rule):
    """Return whether *rule* gives every parent one name, so only the family's channels carry its base."""
    return (
        builds_channelized_family(rule)
        and bool(rule.parent_name_template)
        and not references_variable(rule.parent_name_template, "base")
    )


def _marked_evaluation(template, variables, raw):
    """Return the template evaluated with markers for ``{base}`` and ``{vc_position}``, or None.

    Arithmetic cannot take a marker, so a variable used in arithmetic gets its real value instead.
    """
    vc_values = (_MARKERS["vc_position"], variables["vc_position"]) if "vc_position" in variables else (None,)
    for vc_position in vc_values:
        for base in (_MARKERS["base"], raw):
            marked_variables = {**variables, "base": base}
            if vc_position is not None:
                marked_variables["vc_position"] = vc_position
            try:
                return evaluate_name_template(template, marked_variables)
            except (TypeError, ValueError):
                continue
    return None


def _renamed_pattern(template, variables, raw, historical):
    """Return a matcher for the name *template* gives *raw*, at any virtual-chassis position, or None."""
    marked = _marked_evaluation(template, variables, raw)
    if marked is None:
        return None
    groups = {
        _MARKERS["base"]: (
            "base",
            re.escape(raw) if historical is None else f"(?:{re.escape(raw)}|{historical.pattern})",
        ),
        _MARKERS["vc_position"]: ("vc", VC_POSITION_DIGITS),
    }
    pattern = []
    opened = set()
    for part in re.split(f"({_MARKERS['base']}|{_MARKERS['vc_position']})", marked):
        if part not in groups:
            pattern.append(re.escape(part))
            continue
        name, alternatives = groups[part]
        pattern.append(f"(?P={name})" if name in opened else f"(?P<{name}>{alternatives})")
        opened.add(name)
    return re.compile("".join(pattern))


class RawBases:
    """The raw template name each live interface name stands for under one module rule.

    *families* maps each of the module's interface names outside any channel to its channels, as
    ``(name, channel_id)`` pairs. A rule that does not read ``{base}`` takes every name as its own
    base, so its claims are never computed. So does a module type without interface templates: it
    has no raw names to recover. *catalog* is shared with the family planners, so the module's
    templates load once.
    """

    def __init__(self, module, rule, variables, families, catalog):
        self._module = module
        self._rule = rule
        self._variables = variables
        self._families = dict(families)
        self._names = tuple(self._families)
        self.catalog = catalog
        self._reads_base = rule_reads_base(rule)
        self._claimed = False
        self._by_name = None
        self._ambiguous = frozenset()

    def base_for(self, name):
        """Return the raw template name *name* stands for, or None when no single template claims it."""
        if not self._reads_base:
            return name
        self._load()
        if self._by_name is None:
            return name
        return self._by_name.get(name)

    def is_ambiguous(self, name):
        """Return whether more than one template claims *name*, or its template claims another name too."""
        if not self._reads_base:
            return False
        self._load()
        return name in self._ambiguous

    def _load(self):
        """Compute the claims on first use."""
        if not self._claimed:
            self._by_name, self._ambiguous = self._claim()
            self._claimed = True

    def _claim(self):
        """Return ``(raw name by claimed name, ambiguous names)``, or ``(None, empty)`` without templates.

        A template claims its raw name, its historical raw forms and the names the rule gives it. A
        raw name beats another template's historical form, as in the drift guard, but never a
        renamed form: that overlap is ambiguous, so neither template claims the name. A parent name
        without ``{base}`` fits every template, so a template claims it only through a channel the
        rule gave that template's base.
        """
        claimants = [
            (template.pk, template.template_name, template.resolved, template.historical_pattern)
            for template in self.catalog.get()
            if template.channel_id is None
        ]
        if not claimants:
            return None, frozenset()
        raw_names = {raw for _claimant_id, _template_name, raw, _historical in claimants}
        renaming = _renaming_templates(self._rule)
        needs_channels = _parent_name_needs_channels(self._rule)
        claims = []
        raw_by_claimant = {}
        for claimant_id, template_name, raw, historical in claimants:
            renamed = [
                pattern
                for template in renaming
                if (pattern := _renamed_pattern(template, self._variables, raw, historical)) is not None
            ]
            channels = self._channel_patterns(raw, historical) if needs_channels else None
            labels = tuple(
                name
                for name in self._names
                if name == raw
                or (historical is not None and name not in raw_names and historical.fullmatch(name))
                or self._is_renamed(name, renamed, channels)
            )
            claims.append(TemplateClaim(claimant_id, template_name, labels))
            raw_by_claimant[claimant_id] = raw
        accepted, messages = resolve_template_claims(claims, module=self._module, label_kind="raw base")
        for message in messages:
            logger.warning("%s", message)
        by_name = {label: raw_by_claimant[claimant_id] for claimant_id, label in accepted}
        return by_name, frozenset(label for claim in claims for label in claim.labels) - by_name.keys()

    def _channel_patterns(self, raw, historical):
        """Return, by channel id, a matcher or None for the channel name the rule gives *raw*'s family."""
        rule = self._rule
        return {
            channel_id: _renamed_pattern(
                rule.name_template,
                {**self._variables, "channel": str(rule.channel_start + channel_id - 1)},
                raw,
                historical,
            )
            for channel_id in range(1, rule.channel_count + 1)
        }

    def _is_renamed(self, name, renamed, channels):
        """Return whether *name* is a renamed form and, where *channels* is given, one of its channels is too."""
        if not any(pattern.fullmatch(name) for pattern in renamed):
            return False
        if channels is None:
            return True
        return any(
            (pattern := channels.get(channel_id)) is not None and pattern.fullmatch(channel_name)
            for channel_name, channel_id in self._families[name]
        )


class GivenRawNames:
    """Bases for names a caller gives as raw template names, such as the names prediction receives.

    A given name is its own base, so a stale name is predicted from itself. A name the claim finds
    ambiguous has no base, because installation would not rename it either.
    """

    def __init__(self, bases):
        self._bases = bases

    def base_for(self, name):
        """Return *name*, or None when the claim over the given names finds it ambiguous."""
        return None if self._bases.is_ambiguous(name) else name
