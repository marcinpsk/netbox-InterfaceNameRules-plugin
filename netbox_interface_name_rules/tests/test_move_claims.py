# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The claim after a move, checked on every small layout against a declarative statement of the rule.

The generator places one or two interface templates on a module that moved from virtual-chassis
position 1, bay 0 to position 2, bay 1, under a two-channel flat rule. It adds up to three flat
families, complete or with the first channel only, at each template's current, previous and
historical bases, one plain interface at a template's raw name, and a stray interface. The oracle
states the rule without stages: a template's evidence is every group of present names that one of
its forms spells, a group inside another group of the same template is the same claim, and a
template is admitted only when its evidence is one group that no other template's evidence touches.
"""

import itertools
import re
from dataclasses import dataclass
from types import SimpleNamespace

from django.test import SimpleTestCase

from netbox_interface_name_rules import family
from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.family.template_names import ResolvedTemplateName
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.naming import build_bay_chain_variables

OLD_VC, OLD_BAY = 1, "0"
NEW_VC, NEW_BAY = 2, "1"
HISTORICAL_VCS = (3, 4)


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


KINDS = (
    _Kind("0:0"),
    _Kind("{module}"),
    _Kind("{vc_position}/{module}"),
)
RULES = {
    "base": "{base}:{channel}",
    "bay": "{bay_position}:{channel}",
}


def _variables(vc, bay):
    return build_bay_chain_variables(bay, bay, "0", vc)


def _rule(name_template):
    return InterfaceNameRule(
        name_template=name_template, breakout_mode=BreakoutModeChoices.FLAT, channel_count=2, channel_start=0
    )


def _family_names(rule_key, base, bay):
    """Return the two names the rule gives the family on *base* in *bay*."""
    head = base if rule_key == "base" else bay
    return (f"{head}:0", f"{head}:1")


class _Template:
    def __init__(self, pk, kind):
        self.pk = pk
        self.kind = kind
        self.current = kind.resolved(NEW_VC, NEW_BAY)
        self.previous = kind.resolved(OLD_VC, OLD_BAY)
        self.pattern = kind.historical(NEW_BAY)

    def family_bases(self, rule_key):
        """Return ``(base, bay)`` for the current, previous and historical families of this template."""
        bases = [(self.current, NEW_BAY), (self.previous, OLD_BAY)]
        if self.pattern is not None and rule_key == "base":
            bases.extend((self.kind.resolved(vc, NEW_BAY), NEW_BAY) for vc in HISTORICAL_VCS)
        return bases

    def catalog_entry(self, vc, bay, pattern):
        return ResolvedTemplateName(
            self.pk, self.kind.template_name, self.kind.resolved(vc, bay), pattern, None, None, None
        )


def _layouts():
    """Yield ``(rule key, templates, interface names)`` for every layout the module docstring describes."""
    template_sets = [(kind,) for kind in KINDS] + list(itertools.combinations(KINDS, 2))
    for rule_key, kinds in itertools.product(RULES, template_sets):
        templates = tuple(_Template(pk, kind) for pk, kind in enumerate(kinds, start=1))
        families = list(
            dict.fromkeys(
                _family_names(rule_key, base, bay)
                for template in templates
                for base, bay in template.family_bases(rule_key)
            )
        )
        raws = list(dict.fromkeys(name for template in templates for name in (template.current, template.previous)))
        for count in range(4):
            for chosen in itertools.combinations(families, count):
                for complete in itertools.product((True, False), repeat=count):
                    for plain in (None, *raws):
                        for stray in (False, True):
                            names = [
                                name
                                for names, whole in zip(chosen, complete, strict=True)
                                for name in names[: 2 if whole else 1]
                            ]
                            names += [name for name in (plain, "stray" if stray else None) if name is not None]
                            if len(set(names)) == len(names):
                                yield rule_key, templates, tuple(names)


def _evidence(rule_key, templates, present):
    """Return each template's groups: every present name or complete family one of its forms spells."""
    currents = {template.current for template in templates}
    evidence = {}
    for template in templates:
        groups = {
            (name,)
            for name in present
            if name in (template.current, template.previous)
            or (template.pattern is not None and name not in currents and template.pattern.fullmatch(name))
        }
        bases = [(template.current, NEW_BAY), (template.previous, OLD_BAY)]
        if template.pattern is not None and rule_key == "base":
            bases += [
                (name[:-2], NEW_BAY)
                for name in present
                if name.endswith(":0") and name[:-2] not in currents and template.pattern.fullmatch(name[:-2])
            ]
        groups |= {names for base, bay in bases if set(names := _family_names(rule_key, base, bay)) <= set(present)}
        evidence[template.pk] = groups
    return evidence


def _expected(evidence):
    """Return ``(admitted names, admitted families)`` under the rule stated in the module docstring."""
    names, families = set(), set()
    for pk, groups in evidence.items():
        widest = [group for group in groups if not any(set(group) < set(other) for other in groups)]
        others = {
            name for other, other_groups in evidence.items() if other != pk for group in other_groups for name in group
        }
        if len(widest) == 1 and others.isdisjoint(widest[0]):
            names |= set(widest[0])
            if len(widest[0]) > 1:
                families.add(widest[0])
    return names, families


def _claim(rule_key, templates, present):
    """Return ``(admitted names, admitted families)`` from the plugin's claim after a move."""
    rule = _rule(RULES[rule_key])
    catalog = SimpleNamespace(
        get=lambda: tuple(template.catalog_entry(NEW_VC, NEW_BAY, template.pattern) for template in templates)
    )
    previous_forms = family.PreviousForms(
        rule,
        _variables(OLD_VC, OLD_BAY),
        {
            template.pk: template.catalog_entry(OLD_VC, OLD_BAY, template.kind.historical(OLD_BAY))
            for template in templates
        },
    )
    interfaces = [
        SimpleNamespace(pk=pk, name=name, parent_id=None, channel_id=None, channels=None)
        for pk, name in enumerate(present, start=1)
    ]
    bases = family.move_raw_bases("module", rule, _variables(NEW_VC, NEW_BAY), interfaces, catalog, previous_forms)
    admitted = {name for name in present if bases.base_for(name) is not None}
    return admitted, {tuple(member.name for member in found.members) for found in bases.admitted_flat_families()}


class MoveClaimInvariantTest(SimpleTestCase):
    """Every layout resolves as the declarative rule says, in both directions."""

    def test_every_layout_admits_exactly_what_one_template_alone_claims(self):
        layouts = list(_layouts())
        failures = []
        single_template_admissions = 0
        with self.assertNoLogs("netbox_interface_name_rules", "ERROR"):
            for rule_key, templates, present in layouts:
                evidence = _evidence(rule_key, templates, present)
                expected = _expected(evidence)
                if len(templates) == 1 and expected[0]:
                    single_template_admissions += 1
                got = _claim(rule_key, templates, present)
                if got != expected:
                    failures.append((rule_key, [t.kind.template_name for t in templates], present, got, expected))

        self.assertEqual(len(layouts), 4356)
        self.assertGreater(single_template_admissions, 0)
        self.assertEqual(failures[:5], [], f"{len(failures)} of {len(layouts)} layouts differ")

    def test_a_raw_name_inside_its_own_templates_family_is_admitted_with_the_family(self):
        template = _Template(1, KINDS[0])
        present = ("0:0", "0:1")

        self.assertEqual(_claim("bay", (template,), present), ({"0:0", "0:1"}, {("0:0", "0:1")}))
