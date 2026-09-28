# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Recover the raw template name a module rule reads as ``{base}``.

NetBox keeps no link from an interface to the template that created it, so a rule that reads
``{base}`` claims each live name for the template whose raw name it is, or whose raw name the rule
renames to it. A name that no single template claims has no base, and nothing renames it.
"""

import logging
import re
from typing import NamedTuple

from ..name_template import evaluate_name_template, references_variable
from .claims import TemplateClaim, resolve_group_claims, resolve_template_claims
from .targets import used_parent_template
from .template_names import VC_POSITION_DIGITS

logger = logging.getLogger(__name__)

# PostgreSQL text cannot hold NUL, so no stored name, template or value can spell these markers.
BASE_MARKER = "\x00base\x00"
_MARKERS = {"base": BASE_MARKER, "vc_position": "\x00vc_position\x00"}


def rule_reads_base(rule) -> bool:
    """Return whether any of *rule*'s name templates substitutes ``{base}``."""
    return any(references_variable(template, "base") for template in (rule.name_template, used_parent_template(rule)))


def _renaming_template(rule):
    """Return the template that gives a family's base interface its name under *rule*, or None."""
    if rule.channel_count <= 0:
        return rule.name_template
    return used_parent_template(rule) or None


def _parent_name_needs_channels(rule):
    """Return whether *rule* gives every parent one name, so only the family's channels carry its base."""
    parent_template = used_parent_template(rule)
    return bool(parent_template) and not references_variable(parent_template, "base")


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


def _channel_patterns(rule, variables, raw, historical):
    """Return, by channel id, a matcher or None for the channel name *rule* gives *raw*'s family."""
    return {
        channel_id: _renamed_pattern(
            rule.name_template,
            {**variables, "channel": str(rule.channel_start + channel_id - 1)},
            raw,
            historical,
        )
        for channel_id in range(1, rule.channel_count + 1)
    }


class TemplateForms(NamedTuple):
    """The names one template claims under one rule: its raw name and matchers for what the rule gives it.

    *renamed* is None when the rule gives the template no name of its own. *channels* is given when a
    renamed parent counts only through one of its channels.
    """

    raw: str
    renamed: re.Pattern | None
    channels: dict | None


def _template_forms(rule, variables, raw, historical):
    """Return the forms *rule* gives the template that resolves to *raw*."""
    renaming = _renaming_template(rule)
    renamed = None if renaming is None else _renamed_pattern(renaming, variables, raw, historical)
    channels = _channel_patterns(rule, variables, raw, historical) if _parent_name_needs_channels(rule) else None
    return TemplateForms(raw, renamed, channels)


class RawBases:
    """The raw template name each live interface name stands for under one module rule.

    *families* maps each of the module's interface names outside any channel to its channels, as
    ``(name, channel_id)`` pairs. A rule that does not read ``{base}`` takes every name as its own
    base, so its claims are never computed. So does a module type without interface templates: it
    has no raw names to recover. *catalog* is shared with the family planners, so the module's
    templates load once.

    *previous_forms* holds what named each template before a move. Its forms are claim evidence too,
    and with it every rule computes its claims, so a name that no single template claims has no base.
    *flat_families* are then the complete flat families
    found for each template, with ``template_pk`` and ``members``: a template claims a family as one
    group of names, and one claim covers families and interfaces.
    """

    def __init__(self, module, rule, variables, families, catalog, previous_forms=None, flat_families=()):
        self._module = module
        self._rule = rule
        self._variables = variables
        self._families = dict(families)
        self._names = tuple(self._families)
        self.catalog = catalog
        self.previous_forms = previous_forms
        self.flat_families = tuple(flat_families)
        self._reads_base = previous_forms is not None or rule_reads_base(rule)
        self._claimed = False
        self._by_name = None
        self._claimant_by_name = {}
        self._ambiguous = frozenset()

    def base_for(self, name):
        """Return the raw template name *name* stands for, or None when no single template claims it."""
        if not self._reads_base:
            return name
        self._load()
        if self._by_name is None:
            return name
        return self._by_name.get(name)

    def claimant_for(self, name):
        """Return the primary key of the one template that claims *name*, or None."""
        self._load()
        return self._claimant_by_name.get(name)

    def is_ambiguous(self, name):
        """Return whether more than one template claims *name*, or its template claims another name too."""
        if not self._reads_base:
            return False
        self._load()
        return name in self._ambiguous

    def _load(self):
        """Compute the claims on first use."""
        if not self._claimed:
            self._by_name, self._claimant_by_name, self._ambiguous = self._claim()
            self._claimed = True

    def _claim(self):
        """Return ``(raw name by claimed name, template by claimed name, ambiguous names)``.

        Without templates the first is None, and a name is its own base.
        A template claims its raw name, its historical raw forms and the names the rule gives it. A
        raw name beats another template's historical form, as in the drift guard, but never a
        renamed form: that overlap is ambiguous, so neither template claims the name. A parent name
        without ``{base}`` fits every template, so a template claims it only through a channel the
        rule gave that template's base. A form from the previous state beats nothing.
        """
        claimants = [
            (template.pk, template.template_name, template.resolved, template.historical_pattern)
            for template in self.catalog.get()
            if template.channel_id is None
        ]
        if not claimants:
            return None, {}, frozenset()
        raw_names = {raw for _claimant_id, _template_name, raw, _historical in claimants}
        claims = []
        raw_by_claimant = {}
        for claimant_id, template_name, raw, historical in claimants:
            current = _template_forms(self._rule, self._variables, raw, historical)
            previous = self._previous_template_forms(claimant_id)
            labels = tuple(
                name
                for name in self._names
                if self._matches(name, current)
                or (historical is not None and name not in raw_names and historical.fullmatch(name))
                or (previous is not None and self._matches(name, previous))
            )
            claims.append(TemplateClaim(claimant_id, template_name, labels))
            raw_by_claimant[claimant_id] = raw
        accepted, messages = self._resolve(claims)
        for message in messages:
            logger.warning("%s", message)
        claimant_by_name = {name: claimant_id for claimant_id, group in accepted for name in group}
        by_name = {name: raw_by_claimant[claimant_id] for name, claimant_id in claimant_by_name.items()}
        claimed = {name for claim in claims for group in self._groups(claim) for name in group}
        return by_name, claimant_by_name, frozenset(claimed - by_name.keys())

    def _groups(self, claim):
        """Return the groups of names *claim* holds: one per interface, and after a move one per flat family."""
        groups = tuple((label,) for label in claim.labels)
        if self.previous_forms is None:
            return groups
        return groups + tuple(
            tuple(member.name for member in family.members)
            for family in self.flat_families
            if family.template_pk == claim.claimant_id
        )

    def _resolve(self, claims):
        """Return accepted ``(template, group of names)`` pairs and the messages of the claim over *claims*."""
        if self.previous_forms is None:
            accepted, messages = resolve_template_claims(claims, module=self._module, label_kind="raw base")
            return tuple((claimant_id, (label,)) for claimant_id, label in accepted), messages
        grouped = tuple(TemplateClaim(claim.claimant_id, claim.template_name, self._groups(claim)) for claim in claims)
        return resolve_group_claims(grouped, module=self._module)

    def _previous_template_forms(self, claimant_id):
        """Return the forms the template had in the previous state, or None without a previous state."""
        previous_forms = self.previous_forms
        template = None if previous_forms is None else previous_forms.templates.get(claimant_id)
        if template is None:
            return None
        if previous_forms.rule is None:
            return TemplateForms(template.resolved, None, None)
        return _template_forms(
            previous_forms.rule, previous_forms.variables, template.resolved, template.historical_pattern
        )

    def _matches(self, name, forms):
        """Return whether *name* is the raw name in *forms* or a name its rule gives."""
        return name == forms.raw or self._is_renamed(name, forms.renamed, forms.channels)

    def _is_renamed(self, name, renamed, channels):
        """Return whether *name* is a renamed form and, where *channels* is given, one of its channels is too."""
        if renamed is None or not renamed.fullmatch(name):
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
