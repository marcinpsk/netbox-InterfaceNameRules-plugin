# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The one claim over every name form, checked on every small layout against a declarative statement.

The generator places one or two interface templates on a module at virtual-chassis position 2, bay 1,
which a move brought from position 1, bay 0. The rule is a plain rule that reads ``{base}``, one that
does not, or a flat breakout rule with two channels that reads ``{base}`` or does not. The rule of the previous
state is the same rule, another plain rule, or none. The generator adds up to three interfaces from
the names the templates' forms spell, and a stray interface.

The oracle states the rule without stages. A template claims every present name one of its forms
spells: its raw name now, a historical ``{vc_position}`` name that is no template's raw name now, a
name the rule gives it at any position, and after a move its raw name before the move and a name the
previous rule gives it. A flat breakout rule gives a template a flat family, which the template claims
whole when the module carries its first name; a move recognises no flat family. An interface is
admitted only when one template claims it and that template claims nothing outside one family, and
its base is that template's raw name now.

The same claim decides every path. After a move it decides alone. Without a move, the plans of an
install, of a forced reapply and of Apply Rules read it: an install touches a name that still carries a
raw name, and a forced reapply and Apply Rules touch every name. A rule that reads ``{base}`` keeps a
name that no single template claims, and a breakout rule builds only on a name that one template alone
claims. A rule without channels that does not read ``{base}`` needs no template, so it renames every
name it touches (ADR 0013).
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
MODULE = SimpleNamespace(pk=1, device_id=1)
RULES = {"base": ("x", "{base}"), "bay": ("y", "{bay_position}")}
FLAT_PREFIX = "z"
FLAT_RULES = {"flat": "{base}", "flat without base": "{bay_position}"}
FLAT_NAME = re.compile(rf"{FLAT_PREFIX}(?P<base>.+):0")
UNCLAIMED = "kept as unclaimed"
MISSING = ("blocked", "the flat family is missing 1 of its 2 interfaces")
LAYOUT_COUNT = 7808
FLAT_LAYOUT_COUNT = 2846


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
TEMPLATE_SETS = [(kind,) for kind in KINDS] + list(itertools.combinations(KINDS, 2))


def _variables(vc, bay):
    return build_bay_chain_variables(bay, bay, "0", vc)


def _output(rule_key, base, bay):
    """Return the name the rule *rule_key* gives the template that resolves to *base* in *bay*."""
    prefix, variable = RULES[rule_key]
    return prefix + (base if variable == "{base}" else bay)


def _family(base):
    """Return the names the flat rule gives the family of *base*."""
    return tuple(f"{FLAT_PREFIX}{base}:{channel}" for channel in range(2))


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
    """Yield every plain-rule layout the module docstring describes."""
    for rule_key, kinds in itertools.product(RULES, TEMPLATE_SETS):
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
            yield from _with_present(rule_key, previous_key, templates, pool)


def _flat_targets(rule_key, base):
    """Return the names the flat rule *rule_key* gives the family of *base*."""
    return _family(base if rule_key == "flat" else NEW_BAY)


def _flat_layouts():
    """Yield every flat-rule layout the module docstring describes; a move recognises no flat family."""
    for rule_key, kinds in itertools.product(FLAT_RULES, TEMPLATE_SETS):
        templates = tuple(_Template(pk, kind) for pk, kind in enumerate(kinds, start=1))
        pool = dict.fromkeys(
            name
            for template in templates
            for base in (template.current, *template.examples(NEW_BAY))
            for name in (base, *_flat_targets(rule_key, base))
        )
        yield from _with_present(rule_key, None, templates, pool)


def _with_present(rule_key, previous_key, templates, pool):
    for count in range(4):
        for chosen in itertools.combinations(pool, count):
            yield _Layout(rule_key, previous_key, templates, (*chosen, "stray"))


def _is_raw(template, name, currents):
    """Return whether *name* is *template*'s raw name now, or a historical one no template holds now."""
    return name == template.current or (
        template.now is not None and name not in currents and template.now.fullmatch(name) is not None
    )


def _gives(rule_key, name, bay, raw, historical):
    """Return whether the rule *rule_key* gives *name* to a template whose raw name is *raw*, at any position."""
    if rule_key not in RULES:
        return False
    prefix, variable = RULES[rule_key]
    if not name.startswith(prefix):
        return False
    rest = name[len(prefix) :]
    if variable != "{base}":
        return rest == bay
    return rest == raw or (historical is not None and historical.fullmatch(rest) is not None)


def _units(template, layout, currents, moved):
    """Return ``(names, families)``: the units of one name and of one flat family *template* claims."""
    units, families = set(), set()
    for name in layout.present:
        if (
            _is_raw(template, name, currents)
            or _gives(layout.rule_key, name, NEW_BAY, template.current, template.now)
            or (moved and name == template.previous)
            or (
                moved
                and layout.previous_key is not None
                and _gives(layout.previous_key, name, OLD_BAY, template.previous, template.before)
            )
        ):
            units.add((name,))
        match = FLAT_NAME.fullmatch(name)
        if layout.rule_key in FLAT_RULES and not moved and match is not None:
            base = match["base"]
            if layout.rule_key != "flat":
                spelled = base == NEW_BAY
            else:
                spelled = base == template.current or (template.now is not None and template.now.fullmatch(base))
            if spelled:
                families.add(tuple(member for member in _family(base) if member in layout.present))
    return units, families


@dataclass(frozen=True)
class _Verdict:
    """What the oracle decides: the base of each admitted name, the claimed names, and those claimed as raw."""

    bases: dict
    families: frozenset
    claimed: frozenset
    raw: frozenset


def _expected(layout, moved):
    """Return the verdict of the rule stated in the module docstring."""
    currents = {template.current for template in layout.templates}
    units = {template.pk: _units(template, layout, currents, moved) for template in layout.templates}
    names = {pk: {name for unit in (*singles, *flat) for name in unit} for pk, (singles, flat) in units.items()}
    bases, families = {}, set()
    for template in layout.templates:
        singles, flat = units[template.pk]
        others = set().union(*(claimed for pk, claimed in names.items() if pk != template.pk))
        covering = [unit for unit in singles | flat if set(unit) == names[template.pk]]
        if len(covering) == 1 and others.isdisjoint(names[template.pk]):
            bases.update(dict.fromkeys(names[template.pk], template.current))
            families.update(unit for unit in covering if unit in flat)
    raw = {name for template in layout.templates for name in names[template.pk] if _is_raw(template, name, currents)}
    return _Verdict(bases, frozenset(families), frozenset().union(*names.values()), frozenset(raw))


def _rule(rule_key):
    if rule_key in FLAT_RULES:
        return InterfaceNameRule(
            name_template=f"{FLAT_PREFIX}{FLAT_RULES[rule_key]}:{{channel}}",
            breakout_mode=BreakoutModeChoices.FLAT,
            channel_count=2,
            channel_start=0,
        )
    return InterfaceNameRule(name_template="".join(RULES[rule_key]))


def _interfaces(layout):
    return [
        SimpleNamespace(pk=pk, name=name, device_id=1, module_id=1, parent_id=None, channel_id=None, channels=None)
        for pk, name in enumerate(layout.present, start=1)
    ]


def _claim(layout, rule, interfaces, moved):
    """Return the plugin's one claim over *layout*, after a move or without one."""
    catalog = SimpleNamespace(
        get=lambda: tuple(template.catalog_entry(NEW_VC, NEW_BAY, template.now) for template in layout.templates)
    )
    previous_forms = None
    if moved:
        previous_rule = None if layout.previous_key is None else _rule(layout.previous_key)
        previous_forms = family.PreviousForms(
            previous_rule,
            _variables(OLD_VC, OLD_BAY),
            {template.pk: template.catalog_entry(OLD_VC, OLD_BAY, template.before) for template in layout.templates},
        )
    return family.module_raw_bases(MODULE, rule, _variables(NEW_VC, NEW_BAY), interfaces, previous_forms, catalog)


def _admitted(bases, layout):
    """Return the base of each interface *bases* admits."""
    return {name: base for name in layout.present if (base := bases.base_for(name)) is not None}


def _plan_view(plans):
    """Return, for each interface the plans touch, what it becomes: a name, a family it builds, or why it is kept."""
    view = {}
    for plan in plans:
        if isinstance(plan, family.FlatCreationPlan):
            rows = ((plan.base.name, plan.target_names),)
        else:
            rows = ((member.snapshot.name, member.target_name) for member in plan.members)
        for name, target in rows:
            if plan.precondition_status is None:
                view[name] = target
            elif plan.precondition_reason == family.UNCLAIMED_BASE_REASON:
                view[name] = UNCLAIMED
            else:
                view[name] = (str(plan.precondition_status), plan.precondition_reason)
    return view


def _paths(layout):
    """Return the plan view of an install, a forced reapply and Apply Rules, all read from one claim."""
    rule = _rule(layout.rule_key)
    variables = _variables(NEW_VC, NEW_BAY)
    interfaces = _interfaces(layout)
    bases = _claim(layout, rule, interfaces, moved=False)
    return {
        path: _plan_view(family.plan_module_families(MODULE, rule, variables, interfaces, bases, scope).plans)
        for path, scope in (
            ("install", family.RunScope.INSTALL),
            ("forced reapply", family.RunScope.FORCED),
            ("Apply Rules", None),
        )
    }


def _expected_flat_paths(layout):
    """Return the plan view each path gives a flat layout under the rule stated in the module docstring.

    A complete family takes its names now. A family that lost a member keeps its names, unless the rule
    gives it those names now: its interfaces then build it again, once. On every path a breakout rule
    builds only on a name one template alone claims; an install touches only raw names.
    """
    verdict = _expected(layout, moved=False)
    installed = {}
    for unit in verdict.families:
        base, targets = FLAT_NAME.fullmatch(unit[0])["base"], _flat_targets(layout.rule_key, verdict.bases[unit[0]])
        if len(unit) == len(targets):
            installed.update(zip(unit, targets, strict=True))
        elif _family(base) != targets:
            installed.update(dict.fromkeys(unit, MISSING))
    leftover = [name for name in layout.present if name not in installed]

    def built(names):
        names, kept = list(names), {}
        for name in names:
            if name in verdict.bases:
                targets = _flat_targets(layout.rule_key, verdict.bases[name])
                if targets not in kept or (name == targets[0] and kept[targets] != targets[0]):
                    kept[targets] = name
        taken = {member for targets in kept for member in targets}
        refused = {name: UNCLAIMED for name in names if name not in verdict.bases and name not in taken}
        return {**refused, **{name: targets for targets, name in kept.items()}}

    return {
        "install": built(name for name in leftover if name in verdict.raw),
        "forced reapply": {**installed, **built(leftover)},
        "Apply Rules": {**installed, **built(leftover)},
    }


def _expected_paths(layout):
    """Return the plan view each path gives under the rule stated in the module docstring."""
    verdict = _expected(layout, moved=False)
    prefix, variable = RULES[layout.rule_key]
    reads_base = variable == "{base}"

    def target(name):
        return prefix + (verdict.bases[name] if reads_base else NEW_BAY)

    def planned(name):
        return UNCLAIMED if reads_base and name not in verdict.bases else target(name)

    return {
        "install": {name: planned(name) for name in layout.present if name in verdict.raw},
        "forced reapply": {name: planned(name) for name in layout.present},
        "Apply Rules": {name: planned(name) for name in layout.present},
    }


def _rebuilds(layout):
    """Return whether an incomplete family the rule names now is built again in *layout*."""
    verdict = _expected(layout, moved=False)
    return any(
        len(unit) < 2
        and _family(FLAT_NAME.fullmatch(unit[0])["base"]) == _flat_targets(layout.rule_key, verdict.bases[unit[0]])
        for unit in verdict.families
    )


class ClaimInvariantTest(SimpleTestCase):
    """Every layout resolves as the declarative rule says, in both directions, on every path."""

    def test_every_layout_admits_exactly_what_one_template_alone_claims_after_a_move(self):
        layouts = list(_layouts())
        failures = []
        admitted_alone = 0
        for layout in layouts:
            expected = _expected(layout, moved=True).bases
            if len(layout.templates) == 1 and expected:
                admitted_alone += 1
            got = _admitted(_claim(layout, _rule(layout.rule_key), _interfaces(layout), moved=True), layout)
            if got != expected:
                failures.append((layout.rule_key, layout.previous_key, layout.present, got, expected))

        self.assertEqual(len(layouts), LAYOUT_COUNT)
        self.assertGreater(admitted_alone, 0)
        self.assertEqual(failures[:3], [], f"{len(failures)} of {len(layouts)} layouts differ")

    def test_every_path_without_a_move_reads_the_same_claim(self):
        layouts = [layout for layout in _layouts() if layout.previous_key == layout.rule_key]
        failures = []
        kept = 0
        for layout in layouts:
            expected = _expected_paths(layout)
            kept += UNCLAIMED in expected["install"].values()
            got = _paths(layout)
            if got != expected:
                failures.append((layout.rule_key, layout.present, got, expected))

        self.assertGreater(kept, 0)
        self.assertEqual(failures[:3], [], f"{len(failures)} of {len(layouts)} layouts differ")

    def test_every_flat_layout_claims_each_family_whole(self):
        layouts = list(_flat_layouts())
        failures = []
        families = 0
        for layout in layouts:
            expected = _expected(layout, moved=False)
            families += bool(expected.families)
            bases = _claim(layout, _rule(layout.rule_key), _interfaces(layout), moved=False)
            own_bases = {name: name for name in layout.present}
            want = (
                frozenset(expected.bases),
                expected.bases if layout.rule_key == "flat" else own_bases,
                expected.families,
            )
            got = (
                frozenset(name for name in layout.present if bases.claim(name).accepted),
                _admitted(bases, layout),
                frozenset(
                    tuple(name for name in flat.names if name in layout.present) for flat in bases.flat_families()
                ),
            )
            if got != want:
                failures.append((layout.rule_key, layout.present, got, want))

        self.assertEqual(len(layouts), FLAT_LAYOUT_COUNT)
        self.assertGreater(families, 0)
        self.assertEqual(failures[:3], [], f"{len(failures)} of {len(layouts)} layouts differ")

    def test_every_path_plans_what_the_flat_claim_accepts(self):
        layouts = list(_flat_layouts())
        failures = []
        missing = rebuilt = 0
        for layout in layouts:
            expected = _expected_flat_paths(layout)
            missing += MISSING in expected["Apply Rules"].values()
            rebuilt += _rebuilds(layout)
            got = _paths(layout)
            if got != expected:
                failures.append((layout.rule_key, layout.present, got, expected))

        self.assertEqual(len(layouts), FLAT_LAYOUT_COUNT)
        self.assertGreater(missing, 0)
        self.assertGreater(rebuilt, 0)
        self.assertEqual(failures[:3], [], f"{len(failures)} of {len(layouts)} layouts differ")
