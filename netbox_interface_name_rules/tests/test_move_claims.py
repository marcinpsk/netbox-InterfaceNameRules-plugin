# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The claim after a move, checked on every small layout against a declarative statement of the rule.

The generator places one or two interface templates on a module that moved from virtual-chassis
position 1, bay 0 to position 2, bay 1. The rule now is a plain rule that reads ``{base}`` or one that
does not, and the rule of the previous state is the same rule, the other one, or none. The generator
adds up to three interfaces from the names the templates' forms spell, and a stray interface.

The oracle states the rule without stages. A template claims every present name one of its forms
spells: its raw name before or after the move, a historical ``{vc_position}`` name that is no
template's raw name now, or a name the rule now or the previous rule gives it. An interface is
admitted only when one template claims it and that template claims nothing else, and its base is
that template's raw name now.
"""

import itertools
import re
from dataclasses import dataclass
from types import SimpleNamespace

from django.test import SimpleTestCase

from netbox_interface_name_rules import family
from netbox_interface_name_rules.family.template_names import ResolvedTemplateName
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.naming import build_bay_chain_variables

OLD_VC, OLD_BAY = 1, "0"
NEW_VC, NEW_BAY = 2, "1"
HISTORICAL_VCS = (3, 4)
MODULE = SimpleNamespace(pk=1, device_id=1)
RULES = {"base": ("x", "{base}"), "bay": ("y", "{bay_position}")}
LAYOUT_COUNT = 7808


@dataclass(frozen=True)
class _Kind:
    """One interface template, as it resolves at a virtual-chassis position and a bay."""

    template_name: str

    def resolved(self, vc, bay):
        return self.template_name.replace("{vc_position}", str(vc)).replace("{module}", bay)

    def historical(self, bay):
        """Return the pattern of every name the template gave at another position, or None without the token."""
        if "{vc_position}" not in self.template_name:
            return None
        return re.compile(
            re.escape(self.template_name.replace("{module}", bay)).replace(r"\{vc_position\}", r"\d{1,10}")
        )


KINDS = (_Kind("mgmt"), _Kind("{module}"), _Kind("{vc_position}"), _Kind("{vc_position}/{module}"))


def _variables(vc, bay):
    return build_bay_chain_variables(bay, bay, "0", vc)


def _output(rule_key, base, bay):
    """Return the name the rule *rule_key* gives the template that resolves to *base* in *bay*."""
    prefix, variable = RULES[rule_key]
    return prefix + (base if variable == "{base}" else bay)


class _Template:
    def __init__(self, pk, kind):
        self.pk = pk
        self.kind = kind
        self.current = kind.resolved(NEW_VC, NEW_BAY)
        self.previous = kind.resolved(OLD_VC, OLD_BAY)
        self.now = kind.historical(NEW_BAY)
        self.before = kind.historical(OLD_BAY)

    def examples(self, bay):
        """Return the raw names this template gave at the historical positions in *bay*."""
        return tuple(self.kind.resolved(vc, bay) for vc in HISTORICAL_VCS) if self.now is not None else ()

    def catalog_entry(self, vc, bay, pattern):
        return ResolvedTemplateName(
            self.pk, self.kind.template_name, self.kind.resolved(vc, bay), pattern, None, None, None
        )


@dataclass(frozen=True)
class _Layout:
    rule_key: str
    previous_key: str | None
    templates: tuple
    present: tuple


def _layouts():
    """Yield every layout the module docstring describes."""
    template_sets = [(kind,) for kind in KINDS] + list(itertools.combinations(KINDS, 2))
    for rule_key, kinds in itertools.product(RULES, template_sets):
        for previous_key in (rule_key, *(key for key in RULES if key != rule_key), None):
            templates = tuple(_Template(pk, kind) for pk, kind in enumerate(kinds, start=1))
            pool = dict.fromkeys(
                name
                for template in templates
                for name in (
                    template.current,
                    template.previous,
                    *template.examples(NEW_BAY),
                    *(_output(rule_key, base, NEW_BAY) for base in (template.current, *template.examples(NEW_BAY))),
                    *(
                        _output(previous_key, base, OLD_BAY)
                        for base in (template.previous, *template.examples(OLD_BAY))
                        if previous_key is not None
                    ),
                )
            )
            for count in range(4):
                for chosen in itertools.combinations(pool, count):
                    yield _Layout(rule_key, previous_key, templates, (*chosen, "stray"))


def _spells(template, name, rule_key, previous_key, currents):
    """Return whether one of *template*'s forms spells *name*."""
    now = template.now
    if name in (template.current, template.previous):
        return True
    if now is not None and name not in currents and now.fullmatch(name):
        return True
    if _gives(rule_key, name, NEW_BAY, template.current, now):
        return True
    return previous_key is not None and _gives(previous_key, name, OLD_BAY, template.previous, template.before)


def _gives(rule_key, name, bay, raw, historical):
    """Return whether the rule *rule_key* gives *name* to a template whose raw name is *raw*, at any position."""
    prefix, variable = RULES[rule_key]
    if not name.startswith(prefix):
        return False
    rest = name[len(prefix) :]
    if variable != "{base}":
        return rest == bay
    return rest == raw or (historical is not None and historical.fullmatch(rest) is not None)


def _expected(layout):
    """Return the base of each admitted interface under the rule stated in the module docstring."""
    currents = {template.current for template in layout.templates}
    claims = {
        template.pk: {
            name for name in layout.present if _spells(template, name, layout.rule_key, layout.previous_key, currents)
        }
        for template in layout.templates
    }
    expected = {}
    for template in layout.templates:
        names = claims[template.pk]
        others = set().union(*(claimed for pk, claimed in claims.items() if pk != template.pk))
        if len(names) == 1 and others.isdisjoint(names):
            expected[next(iter(names))] = template.current
    return expected


def _claim(layout):
    """Return the base of each interface the plugin's claim after a move admits."""
    prefix, variable = RULES[layout.rule_key]
    rule = InterfaceNameRule(name_template=prefix + variable)
    previous_rule = None
    if layout.previous_key is not None:
        previous_rule = InterfaceNameRule(name_template="".join(RULES[layout.previous_key]))
    catalog = SimpleNamespace(
        get=lambda: tuple(template.catalog_entry(NEW_VC, NEW_BAY, template.now) for template in layout.templates)
    )
    previous_forms = family.PreviousForms(
        previous_rule,
        _variables(OLD_VC, OLD_BAY),
        {template.pk: template.catalog_entry(OLD_VC, OLD_BAY, template.before) for template in layout.templates},
    )
    interfaces = [
        SimpleNamespace(pk=pk, name=name, parent_id=None, channel_id=None, channels=None)
        for pk, name in enumerate(layout.present, start=1)
    ]
    bases = family.move_raw_bases(MODULE, rule, _variables(NEW_VC, NEW_BAY), interfaces, catalog, previous_forms)
    return {name: base for name in layout.present if (base := bases.base_for(name)) is not None}


class MoveClaimInvariantTest(SimpleTestCase):
    """Every layout resolves as the declarative rule says, in both directions."""

    def test_every_layout_admits_exactly_what_one_template_alone_claims(self):
        layouts = list(_layouts())
        failures = []
        admitted_alone = 0
        for layout in layouts:
            expected = _expected(layout)
            if len(layout.templates) == 1 and expected:
                admitted_alone += 1
            got = _claim(layout)
            if got != expected:
                failures.append((layout.rule_key, layout.previous_key, layout.present, got, expected))

        self.assertEqual(len(layouts), LAYOUT_COUNT)
        self.assertGreater(admitted_alone, 0)
        self.assertEqual(failures[:3], [], f"{len(failures)} of {len(layouts)} layouts differ")
