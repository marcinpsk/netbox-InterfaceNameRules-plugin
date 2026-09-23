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

    *names* are the module's interface names outside any channel. A rule that does not read
    ``{base}`` takes every name as its own base, so its claims are never computed. So does a module
    type without interface templates: it has no raw names to recover. *catalog* is shared with the
    family planners, so the module's templates load once.
    """

    def __init__(self, module, rule, variables, names, catalog):
        self._module = module
        self._rule = rule
        self._variables = variables
        self._names = tuple(names)
        self.catalog = catalog
        self._reads_base = rule_reads_base(rule)
        self._claimed = False
        self._by_name = None

    def base_for(self, name):
        """Return the raw template name *name* stands for, or None when no single template claims it."""
        if not self._reads_base:
            return name
        if not self._claimed:
            self._by_name = self._claim()
            self._claimed = True
        if self._by_name is None:
            return name
        return self._by_name.get(name)

    def _claim(self):
        """Map every name exactly one template claims to its raw name, or return None without templates.

        A template claims its raw name, its historical raw forms and the names the rule gives it. A
        raw name beats another template's historical form, as in the drift guard, but never a
        renamed form: that overlap is ambiguous, so neither template claims the name.
        """
        claimants = [
            (template.pk, template.template_name, template.resolved, template.historical_pattern)
            for template in self.catalog.get()
            if template.channel_id is None
        ]
        if not claimants:
            return None
        raw_names = {raw for _claimant_id, _template_name, raw, _historical in claimants}
        renaming = _renaming_templates(self._rule)
        claims = []
        raw_by_claimant = {}
        for claimant_id, template_name, raw, historical in claimants:
            renamed = [
                pattern
                for template in renaming
                if (pattern := _renamed_pattern(template, self._variables, raw, historical)) is not None
            ]
            labels = tuple(
                name
                for name in self._names
                if name == raw
                or (historical is not None and name not in raw_names and historical.fullmatch(name))
                or any(pattern.fullmatch(name) for pattern in renamed)
            )
            claims.append(TemplateClaim(claimant_id, template_name, labels))
            raw_by_claimant[claimant_id] = raw
        accepted, messages = resolve_template_claims(claims, module=self._module, label_kind="raw base")
        for message in messages:
            logger.warning("%s", message)
        return {label: raw_by_claimant[claimant_id] for claimant_id, label in accepted}


class GivenRawNames:
    """Bases for names that are already raw template names, such as the names prediction receives."""

    @staticmethod
    def base_for(name):
        """Return *name*: it is its own raw template name."""
        return name


GIVEN_RAW_NAMES = GivenRawNames()
