# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The claim and the plans after a move, checked on every small layout against a declarative rule.

The generator places one or two interface templates on a module that moved from virtual-chassis
position 1, bay 0 to position 2, bay 1. The previous and the current flat rule each have two or four
channels. The generator adds up to three flat families, complete or with the first channel only, at
each template's current, previous and historical bases, one plain interface at a template's raw
name, and a stray interface.

The oracle states the rule without stages. A template's evidence is every group of present names
that one of its forms spells: a raw name before or after the move, a historical name, or a complete
family from its current, previous or historical base. A group inside another group of the same
template is the same claim. A template is admitted only when its evidence is one group that no
other template's evidence touches. At plan level, no interface is in two plans; an admitted family
takes the current rule's names whole, or keeps every name when its channel count differs; a lone
admitted interface takes the first name of the family built on it, and two such interfaces that
intend one family build it once.
"""

import itertools
import re
from collections import Counter, defaultdict
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
MODULE = SimpleNamespace(pk=1, device_id=1)


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


KINDS = (_Kind("0:0"), _Kind("{module}"), _Kind("{vc_position}"), _Kind("{vc_position}/{module}"))
RULES = {"base": "{base}:{channel}", "bay": "{bay_position}:{channel}"}
CHANNEL_COUNTS = ((2, 2), (2, 4), (4, 2), (4, 4))
LAYOUT_COUNT = 27488


def _variables(vc, bay):
    return build_bay_chain_variables(bay, bay, "0", vc)


def _rule(name_template, channels):
    return InterfaceNameRule(
        name_template=name_template, breakout_mode=BreakoutModeChoices.FLAT, channel_count=channels, channel_start=0
    )


def _family_names(rule_key, base, bay, channels):
    """Return the names the rule gives the family on *base* in *bay*."""
    head = base if rule_key == "base" else bay
    return tuple(f"{head}:{channel}" for channel in range(channels))


class _Template:
    def __init__(self, pk, kind):
        self.pk = pk
        self.kind = kind
        self.current = kind.resolved(NEW_VC, NEW_BAY)
        self.previous = kind.resolved(OLD_VC, OLD_BAY)
        self.pattern = kind.historical(NEW_BAY)

    def family_bases(self, rule_key, channels):
        """Return ``(base, bay, channel count)`` for the current, previous and historical families of this template."""
        previous_count, current_count = channels
        bases = [(self.current, NEW_BAY, current_count), (self.previous, OLD_BAY, previous_count)]
        if self.pattern is not None and rule_key == "base":
            bases.extend((self.kind.resolved(vc, NEW_BAY), NEW_BAY, current_count) for vc in HISTORICAL_VCS)
        return bases

    def catalog_entry(self, vc, bay, pattern):
        return ResolvedTemplateName(
            self.pk, self.kind.template_name, self.kind.resolved(vc, bay), pattern, None, None, None
        )


@dataclass(frozen=True)
class _Layout:
    rule_key: str
    channels: tuple
    templates: tuple
    present: tuple


def _layouts():
    """Yield every layout the module docstring describes, each with a stray interface."""
    template_sets = [(kind,) for kind in KINDS] + list(itertools.combinations(KINDS, 2))
    for rule_key, channels, kinds in itertools.product(RULES, CHANNEL_COUNTS, template_sets):
        templates = tuple(_Template(pk, kind) for pk, kind in enumerate(kinds, start=1))
        families = list(
            dict.fromkeys(
                _family_names(rule_key, base, bay, count)
                for template in templates
                for base, bay, count in template.family_bases(rule_key, channels)
            )
        )
        raws = list(dict.fromkeys(name for template in templates for name in (template.current, template.previous)))
        for count in range(4):
            for chosen in itertools.combinations(families, count):
                for complete in itertools.product((True, False), repeat=count):
                    for plain in (None, *raws):
                        names = [
                            name
                            for names, whole in zip(chosen, complete, strict=True)
                            for name in names[: None if whole else 1]
                        ]
                        names += [name for name in (plain, "stray") if name is not None]
                        if len(set(names)) == len(names):
                            yield _Layout(rule_key, channels, templates, tuple(names))


def _evidence(layout):
    """Return each template's groups: every present name or complete family one of its forms spells."""
    previous_count, current_count = layout.channels
    present = set(layout.present)
    currents = {template.current for template in layout.templates}
    evidence = {}
    for template in layout.templates:
        groups = {
            (name,)
            for name in layout.present
            if name in (template.current, template.previous)
            or (template.pattern is not None and name not in currents and template.pattern.fullmatch(name))
        }
        bases = [(template.current, NEW_BAY, current_count), (template.previous, OLD_BAY, previous_count)]
        if template.pattern is not None and layout.rule_key == "base":
            bases += [
                (name[:-2], NEW_BAY, current_count)
                for name in layout.present
                if name.endswith(":0") and name[:-2] not in currents and template.pattern.fullmatch(name[:-2])
            ]
        groups |= {
            names
            for base, bay, count in bases
            if set(names := _family_names(layout.rule_key, base, bay, count)) <= present
        }
        evidence[template.pk] = groups
    return evidence


def _admitted(evidence):
    """Return the one group each admitted template claims."""
    admitted = {}
    for pk, groups in evidence.items():
        widest = [group for group in groups if not any(set(group) < set(other) for other in groups)]
        others = {
            name for other, other_groups in evidence.items() if other != pk for group in other_groups for name in group
        }
        if len(widest) == 1 and others.isdisjoint(widest[0]):
            admitted[pk] = widest[0]
    return admitted


def _plan_problems(layout, admitted, plans):
    """Return what the plans do that the rule does not allow, or an empty list."""
    members = [member for plan in plans for member in plan.live_members]
    shared = sorted(name for name, count in Counter(member.snapshot.name for member in members).items() if count > 1)
    if shared:
        return [f"in two plans: {shared}"]
    planned = {
        member.snapshot.name: member.target_name
        for plan in plans
        if plan.precondition_status is None
        for member in plan.live_members
        if member.target_name != member.snapshot.name
    }
    _previous_count, current_count = layout.channels
    expected, lone = {}, defaultdict(list)
    for template in layout.templates:
        group = admitted.get(template.pk)
        if group is None:
            continue
        names = _family_names(layout.rule_key, template.current, NEW_BAY, current_count)
        if len(group) == 1:
            lone[names].append(group[0])
        elif len(group) == current_count:
            expected.update(zip(group, names, strict=True))
    problems = []
    for names, bases in lone.items():
        built = [base for base in bases if base in planned] if len(bases) > 1 else bases
        if len(built) != 1:
            problems.append(f"{len(built)} of {bases} build {names}")
        expected.update((base, names[0]) for base in built)
    expected = {name: target for name, target in expected.items() if name != target}
    if planned != expected:
        problems.append(f"planned {planned}, expected {expected}")
    return problems


def _claim_and_plans(layout):
    """Return ``(admitted names, plans)`` from the plugin's claim and planner after a move."""
    previous_count, current_count = layout.channels
    rule = _rule(RULES[layout.rule_key], current_count)
    catalog = SimpleNamespace(
        get=lambda: tuple(template.catalog_entry(NEW_VC, NEW_BAY, template.pattern) for template in layout.templates)
    )
    previous_forms = family.PreviousForms(
        _rule(RULES[layout.rule_key], previous_count),
        _variables(OLD_VC, OLD_BAY),
        {
            template.pk: template.catalog_entry(OLD_VC, OLD_BAY, template.kind.historical(OLD_BAY))
            for template in layout.templates
        },
    )
    interfaces = [
        SimpleNamespace(pk=pk, name=name, device_id=1, module_id=1, parent_id=None, channel_id=None, channels=None)
        for pk, name in enumerate(layout.present, start=1)
    ]
    variables = _variables(NEW_VC, NEW_BAY)
    bases = family.move_raw_bases(MODULE, rule, variables, interfaces, catalog, previous_forms)
    admitted = {name for name in layout.present if bases.base_for(name) is not None}
    return admitted, family.plan_module_families_from(MODULE, rule, variables, interfaces, bases).plans


class MoveClaimInvariantTest(SimpleTestCase):
    """Every layout resolves and plans as the declarative rule says, in both directions."""

    def test_every_layout_admits_and_plans_exactly_what_one_template_alone_claims(self):
        layouts = list(_layouts())
        failures = []
        admitted_alone = 0
        for layout in layouts:
            admitted = _admitted(_evidence(layout))
            if len(layout.templates) == 1 and admitted:
                admitted_alone += 1
            try:
                names, plans = _claim_and_plans(layout)
            except ValueError as error:
                failures.append((layout, str(error)))
                continue
            problems = _plan_problems(layout, admitted, plans)
            if names != {name for group in admitted.values() for name in group}:
                problems.append(f"admitted {sorted(names)}")
            if problems:
                failures.append((layout, problems))

        self.assertEqual(len(layouts), LAYOUT_COUNT)
        self.assertGreater(admitted_alone, 0)
        self.assertEqual(failures[:3], [], f"{len(failures)} of {len(layouts)} layouts differ")

    def test_a_raw_name_inside_its_own_templates_family_is_admitted_with_the_family(self):
        layout = _Layout("bay", (2, 2), (_Template(1, KINDS[0]),), ("0:0", "0:1"))

        names, plans = _claim_and_plans(layout)

        self.assertEqual(names, {"0:0", "0:1"})
        self.assertEqual(_plan_problems(layout, {1: ("0:0", "0:1")}, plans), [])

    def test_two_plans_that_share_an_interface_are_refused_before_any_runs(self):
        rule = _rule(RULES["base"], 2)
        interfaces = [
            SimpleNamespace(pk=pk, name=name, device_id=1, module_id=1, parent_id=None, channel_id=None, channels=None)
            for pk, name in enumerate(("0:0", "0:1", "1:0"), start=1)
        ]
        overlapping = (
            family.FlatFamily(1, "1", ("1:0", "1:1"), (interfaces[0], interfaces[1])),
            family.FlatFamily(2, "2", ("2:0", "2:1"), (interfaces[1], interfaces[2])),
        )
        claim = SimpleNamespace(
            previous_forms=object(),
            catalog=None,
            base_for=lambda _name: None,
            admitted_flat_families=lambda: overlapping,
        )

        with self.assertRaisesMessage(ValueError, "interface '0:1' is in more than one planned family"):
            family.plan_module_families_from(MODULE, rule, _variables(NEW_VC, NEW_BAY), interfaces, claim)
