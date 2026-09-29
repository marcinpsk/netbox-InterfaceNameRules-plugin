# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Claim each live interface name for the one interface template it stands for.

NetBox keeps no link from an interface to the template that created it, so every path claims each
live name for the templates whose forms spell it, in one pass over every form of every template. A
name that no single template claims has no base, and nothing renames it.
"""

import logging
import re
from typing import NamedTuple

from ..name_template import evaluate_name_template, references_variable
from .claims import TemplateClaim, resolve_template_claims
from .targets import breaks_out, flat_family_names, used_parent_template
from .template_names import VC_POSITION_DIGITS, ResolvedTemplateName

logger = logging.getLogger(__name__)

# PostgreSQL text cannot hold NUL, so no stored name, template or value can spell these markers.
_MARKERS = {"base": "\x00base\x00", "vc_position": "\x00vc_position\x00"}
# A module type without interface templates claims as one template whose raw name is the bay position.
_STAND_IN = "{bay_position}"


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
    The position marker stands for any position, also on a device that has no position now.
    """
    vc_values = (_MARKERS["vc_position"], *((variables["vc_position"],) if "vc_position" in variables else ()))
    for vc_position in vc_values:
        for base in (_MARKERS["base"], raw):
            try:
                return evaluate_name_template(template, {**variables, "base": base, "vc_position": vc_position})
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


class FlatFamily(NamedTuple):
    """A flat breakout family that one template claims.

    *base_name* is the template's raw name now, and *names* are the names the family's rows carry,
    in channel order.
    """

    base_name: str
    names: tuple[str, ...]


class NameClaim(NamedTuple):
    """Whether a template claims one name, one template alone does, and one does as a raw name now or earlier."""

    claimed: bool
    accepted: bool
    raw: bool


_UNCLAIMED = NameClaim(claimed=False, accepted=False, raw=False)


class _Resolution(NamedTuple):
    """What one claim pass decided: the raw base of each name one template alone claims, families and claims."""

    bases: dict
    families: tuple[FlatFamily, ...]
    claims: dict


class RawBases:
    """The one claim over every name form of a module's interfaces under one rule (ADR 0013).

    *families* maps each name outside a channel to its channels; *plain* names the flat-family candidates.
    *earlier_positions* are the virtual-chassis positions at which the templates also gave raw names.
    """

    def __init__(self, module, rule, variables, families, plain, catalog, previous_forms=None, earlier_positions=()):
        self._module = module
        self._rule = rule
        self._variables = variables
        self._families = dict(families)
        self._names = tuple(self._families)
        self._plain = frozenset(plain)
        self.catalog = catalog
        self.previous_forms = previous_forms
        self._earlier_positions = tuple(earlier_positions)
        self._reads_base = previous_forms is not None or rule_reads_base(rule)
        self._breaks_out = breaks_out(rule)
        self._claims_flat = previous_forms is None and self._breaks_out
        self._resolution = None

    def base_for(self, name):
        """Return ``{base}`` for *name*: its own name for a rule that needs no claim, else ``builds_on``."""
        return self.builds_on(name) if self._reads_base else name

    def builds_on(self, name):
        """Return the raw template name *name* stands for, or None when no single template claims it."""
        return self._resolved().bases.get(name)

    def is_ambiguous(self, name):
        """Return whether a rule that reads the claim finds *name* claimed, but not by one template alone."""
        if not (self._reads_base or self._breaks_out):
            return False
        claim = self.claim(name)
        return claim.claimed and not claim.accepted

    def claim(self, name):
        """Return how the claim treats *name*."""
        return self._resolved().claims.get(name, _UNCLAIMED)

    def flat_families(self):
        """Return every flat family that one template alone claims."""
        if not self._claims_flat:
            return ()
        return self._resolved().families

    def _resolved(self):
        """Resolve the claim on first use."""
        if self._resolution is None:
            self._resolution = self._claim()
        return self._resolution

    def _claim(self):
        """Resolve every form of every template in one pass, in the order ADR 0013 lists."""
        templates = [template for template in self.catalog.get() if template.channel_id is None]
        if not templates and self.previous_forms is None:
            templates = [ResolvedTemplateName(None, _STAND_IN, self._variables["bay_position"], None, None, None, None)]
        raw_names = {name for template in templates for name in self._raw_forms(template)}
        raw_by_claimant = {template.pk: template.resolved for template in templates}
        claims, flat, raw_claimed = [], {}, set()
        for template in templates:
            single = self._single_names(template, raw_names)
            raw_claimed.update(name for name in single if self._is_raw_name(name, template, raw_names))
            families = self._flat_families(template)
            flat.update(((template.pk, unit), family) for unit, family in families.items())
            claims.append(
                TemplateClaim(template.pk, template.template_name, [(name,) for name in single] + [*families])
            )
        accepted, messages = resolve_template_claims(claims, module=self._module)
        for message in messages:
            logger.warning("%s", message)
        bases = {name: raw_by_claimant[claimant_id] for claimant_id, unit in accepted for name in unit}
        claimed = dict.fromkeys(name for claim in claims for unit in claim.units for name in unit)
        return _Resolution(
            bases=bases,
            families=tuple(flat[pair] for pair in accepted if pair in flat),
            claims={name: NameClaim(True, name in bases, name in raw_claimed) for name in claimed},
        )

    def _single_names(self, template, raw_names):
        """Return each name that one of *template*'s forms spells, other than a flat family."""
        current = _template_forms(self._rule, self._variables, template.resolved, template.historical_pattern)
        previous = self._previous_template_forms(template.pk)
        return tuple(
            name
            for name in self._names
            if self._matches(name, current)
            or self._is_raw_name(name, template, raw_names)
            or (previous is not None and self._matches(name, previous))
        )

    def _raw_forms(self, template):
        """Return *template*'s raw name now and at each of the earlier positions."""
        return {template.resolved, *(template.at_chassis_position(p).resolved for p in self._earlier_positions)}

    def _is_raw_name(self, name, template, raw_names):
        """Return whether *name* is *template*'s raw name now, earlier, or at a position no other raw name holds."""
        historical = template.historical_pattern
        return name in self._raw_forms(template) or (
            historical is not None and name not in raw_names and historical.fullmatch(name) is not None
        )

    def _flat_families(self, template):
        """Return, by the plain interfaces the module carries, each flat family whose first one it carries."""
        if not self._claims_flat:
            return {}
        rule = self._rule
        first = _renamed_pattern(
            rule.name_template,
            {**self._variables, "channel": str(rule.channel_start)},
            template.resolved,
            template.historical_pattern,
        )
        if first is None:
            return {}
        families = {}
        for name in self._names:
            match = None if name not in self._plain else first.fullmatch(name)
            family = None if match is None else self._flat_family(match.groupdict(), template.resolved)
            if family is not None:
                families[tuple(member for member in family.names if member in self._plain)] = family
        return families

    def _flat_family(self, groups, raw):
        """Return the flat family whose first name matched with *groups*, or None when the rule cannot name it."""
        variables = self._variables if "vc" not in groups else {**self._variables, "vc_position": groups["vc"]}
        try:
            names = flat_family_names(self._rule, variables, groups.get("base", raw))
        except (TypeError, ValueError):
            return None
        return FlatFamily(raw, names) if len(set(names)) == len(names) else None

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

    builds_on = base_for
