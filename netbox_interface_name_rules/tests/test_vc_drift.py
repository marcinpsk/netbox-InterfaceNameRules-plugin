# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for raw-name drift caused by NetBox's native ``{vc_position}`` template token.

NetBox 4.6 resolves ``{vc_position}`` / ``{vc_position:X}`` in component *template* names once, at
instantiation: the member's position when the device is in a virtual chassis, else the explicit
fallback, else ``0``.  It never re-resolves them.  The engine, in contrast, recomputes template
names at apply time, so on a module type that uses the token the raw-name set is time-dependent and
an interface stops matching its own raw name after the device joins a VC, changes position inside
one, or leaves it.

The three drift directions are exercised here through real ``Device.save()`` calls inside
``captureOnCommitCallbacks``, so the plugin's own signals schedule and run the re-apply, and against
real ``VirtualChassis`` rows and real NetBox module instantiation.  The only simulation is NetBox
≤ 4.5 (see ``VcPositionLegacyNetboxTest``), where the constant that carries the token has to be
hidden from the engine's feature check.

A fixture whose interface names come out of the token needs a release that resolves it, so every
such class is gated on ``supports_vc_position_token()``; the two control classes are not, and their
assertions are written to hold on a release that never resolves the token as well.

Two engine names are pinned here as the fix's public surface:

* ``engine.supports_vc_position_token()`` — the feature check, probed lazily from
  ``dcim.constants.VC_POSITION_RE`` the way ``supports_channelization()`` probes the Interface model.
* ``engine._raw_name_patterns(module)`` — the structural matchers, one per interface template whose
  name carries the token, empty for every other template and on every release without the constant.

Everything else is pinned through behaviour.
"""

import sys
import types
from unittest import mock, skipUnless

from dcim.models import (
    Device,
    Interface,
    InterfaceTemplate,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    VirtualChassis,
)

from netbox_interface_name_rules import engine
from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    apply_rule_to_existing,
    find_convertible_families,
    predict_rule_output,
    supports_channelization,
    supports_vc_position_token,
)
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.signals import _apply_rules_deferred
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_channelization import (
    CHANNEL_TYPE,
    PARENT_TYPE,
    PLAIN_TYPE,
    PLUGIN_LOGGER,
    REQUIRES_CHANNELIZATION,
    ChannelizationTestCase,
    _build_device,
)

FLAT = BreakoutModeChoices.FLAT
CHANNELIZED = BreakoutModeChoices.CHANNELIZED

# Every fixture spelling a name NetBox resolved from the token needs the release that resolves it:
# on 4.5 and older the token stays literal in the interface name and the drift cannot even occur.
REQUIRES_VC_POSITION_TOKEN = "requires a NetBox that resolves {vc_position} in template names (4.6+)"


def _token_module_type(manufacturer, model, *template_names, iface_type=PLAIN_TYPE):
    """Create a ModuleType whose interface templates are named *template_names*, in order."""
    module_type = ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=model)
    for name in template_names:
        InterfaceTemplate.objects.create(module_type=module_type, name=name, type=iface_type)
    return module_type


def _raw_name_patterns(module):
    """Return the engine's structural raw-name matchers for *module* (see the module docstring)."""
    return list(engine._raw_name_patterns(module))


class _ConstantsWithoutVcToken(types.ModuleType):
    """``dcim.constants`` as NetBox ≤ 4.5 shipped it — everything except ``VC_POSITION_RE``."""

    def __init__(self, real):
        super().__init__(real.__name__)
        self._real = real

    def __getattr__(self, name):
        if name == "VC_POSITION_RE":
            raise AttributeError(name)
        return getattr(self._real, name)


def _without_vc_position_re():
    """Hide ``VC_POSITION_RE`` from the engine's lazy feature check, leaving NetBox itself intact."""
    import dcim.constants

    return mock.patch.dict(sys.modules, {"dcim.constants": _ConstantsWithoutVcToken(dcim.constants)})


class VcDriftTestCase(ChannelizationTestCase):
    """VC transitions go through a real ``Device.save()`` so the plugin's signals do the scheduling."""

    def _save_vc_state(self, device, virtual_chassis, position):
        with self.captureOnCommitCallbacks(execute=True):
            device.virtual_chassis = virtual_chassis
            device.vc_position = position
            device.save()

    def _join(self, vc, position, device=None):
        """Add *device* to *vc* at *position* — the join direction (fallback → position)."""
        self._save_vc_state(device or self.device, vc, position)

    def _renumber(self, position, device=None):
        """Move *device* to another position inside its VC — the renumber direction (P → Q)."""
        device = device or self.device
        self._save_vc_state(device, device.virtual_chassis, position)

    def _leave(self, device=None):
        """Remove *device* from its VC — the leave direction (position → fallback)."""
        self._save_vc_state(device or self.device, None, None)

    def _install_on(self, device, module_type, position):
        """Install a module of *module_type* into *device*'s bay at *position*, rules and all."""
        bay = ModuleBay.objects.get(device=device, name=f"Bay {position}")
        with self.captureOnCommitCallbacks(execute=True):
            module = Module.objects.create(device=device, module_bay=bay, module_type=module_type)
        return module, bay


# ---------------------------------------------------------------------------
# Join: the device was standalone when NetBox named the interfaces
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionJoinDriftTest(VcDriftTestCase):
    """Interfaces instantiated outside a VC keep the fallback name after the device joins one."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("VcJoin", ["3", "4"])
        cls.module_type = _token_module_type(manufacturer, "VcJoin-QSFP", "xe-{vc_position:0}/0/{module}")

    def test_joining_a_vc_renames_a_flat_channel_family_named_with_the_fallback(self):
        """The worst case from the issue: under force, a breakout rule matches its family by raw name."""
        module, _ = self._install_on(self.device, self.module_type, "3")
        self.assertEqual(self._names(module), ["xe-0/0/3"])  # instantiated with the '0' fallback
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-{vc_position}/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )

        self._join(VirtualChassis.objects.create(name="vcjoin-vc"), 2)

        self.assertEqual(self._names(module), ["et-2/0/3:0", "et-2/0/3:1", "et-2/0/3:2", "et-2/0/3:3"])

    def test_a_drifted_interface_is_still_raw_on_the_non_force_path(self):
        """The idempotency guard asks 'is this name still the template's'; drift makes it answer wrongly.

        The module install callback is the non-force consumer, so it is the one run here: the device
        joined the VC while no rule existed, and the rule that arrives afterwards must still see the
        interface as unrenamed.
        """
        module, bay = self._install_on(self.device, self.module_type, "4")
        self._join(VirtualChassis.objects.create(name="vcjoin-vc2"), 2)
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        _apply_rules_deferred(module.pk, bay.pk)

        self.assertEqual(self._names(module), ["et-0/0/4"])


# ---------------------------------------------------------------------------
# Renumber: the position the interfaces were named with is gone
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionRenumberDriftTest(VcDriftTestCase):
    """A historical position is neither the current one nor the fallback — enumerating values misses it."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcRenum", ["3", "5"], virtual_chassis=VirtualChassis.objects.create(name="vcrenum-vc"), vc_position=1
        )
        cls.module_type = _token_module_type(manufacturer, "VcRenum-QSFP", "xe-{vc_position:0}/0/{module}")
        cls.simple_type = _token_module_type(manufacturer, "VcRenum-SFP", "xe-{vc_position:0}/0/{module}")

    def test_renumbering_renames_a_family_named_at_an_earlier_position(self):
        """The token sits in a middle path segment, so the drifted name is structural, not a suffix."""
        module, _ = self._install_on(self.device, self.module_type, "3")
        self.assertEqual(self._names(module), ["xe-1/0/3"])
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-{vc_position}/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )

        self._renumber(2)

        self.assertEqual(self._names(module), ["et-2/0/3:0", "et-2/0/3:1", "et-2/0/3:2", "et-2/0/3:3"])

    def test_one_matcher_covers_every_resolution_the_template_ever_had(self):
        """Enumerating the current position and the fallback misses position 1; a matcher covers all three."""
        module, _ = self._install_on(self.device, self.module_type, "5")
        self._renumber(2)
        template = InterfaceTemplate.objects.get(module_type=self.module_type)

        self.assertEqual(self._names(module), ["xe-1/0/5"])
        self.assertEqual(template.resolve_name(module), "xe-2/0/5")  # all NetBox resolves it to now

        patterns = _raw_name_patterns(module)
        self.assertEqual(len(patterns), 1)
        for name in ("xe-1/0/5", "xe-2/0/5", "xe-0/0/5"):  # historical, current, off-VC fallback
            self.assertTrue(patterns[0].fullmatch(name), f"{patterns[0].pattern} does not cover {name}")


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionForceBaseMatchingTest(VcDriftTestCase):
    """Force-mode channel bases are compared in two forms; drift-awareness applies to both, and no more."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcForm", ["5", "7"], virtual_chassis=VirtualChassis.objects.create(name="vcform-vc"), vc_position=1
        )
        # The raw name has no '/', so an already-renamed base only matches on its last path segment.
        cls.module_type = _token_module_type(manufacturer, "VcForm-QSFP", "{vc_position}-{module}")

    def _breakout_rule(self, name_template):
        return InterfaceNameRule.objects.create(
            module_type=self.module_type, name_template=name_template, channel_count=1, channel_start=0
        )

    def test_an_already_renamed_base_matches_on_its_last_path_segment(self):
        """``xe-.../{raw}`` keeps the raw name as the last segment — the second form the force path compares."""
        self._breakout_rule("et-0/0/{vc_position}-{bay_position}:{channel}")
        module, _ = self._install_on(self.device, self.module_type, "5")
        self.assertEqual(self._names(module), ["et-0/0/1-5:0"])

        self._renumber(2)

        self.assertEqual(self._names(module), ["et-0/0/2-5:0"])

    def test_a_rule_output_that_buries_the_raw_name_stays_out_of_reach(self):
        """The honest boundary: neither comparison form ever saw such a name, and none is invented."""
        self._breakout_rule("et-0/0/{vc_position}-{bay_position}-x:{channel}")
        module, _ = self._install_on(self.device, self.module_type, "7")
        self.assertEqual(self._names(module), ["et-0/0/1-7-x:0"])

        self._renumber(2)

        self.assertEqual(self._names(module), ["et-0/0/1-7-x:0"])


# ---------------------------------------------------------------------------
# Leave: no signal fires, and the manual paths have to cope
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionLeaveDriftTest(VcDriftTestCase):
    """Leaving a VC is an operator decision — nothing is scheduled, but a re-apply must still match."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcLeave", ["3", "4", "5"], virtual_chassis=VirtualChassis.objects.create(name="vcleave-vc"), vc_position=2
        )
        cls.module_type = _token_module_type(manufacturer, "VcLeave-SFP", "xe-{vc_position:0}/0/{module}")

    def test_leaving_a_vc_schedules_no_rename(self):
        """Deliberate: a rule's ``{vc_position}`` cannot even evaluate off a VC, so un-renaming is manual."""
        module, _ = self._install_on(self.device, self.module_type, "3")
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        self._leave()

        self.assertEqual(self._names(module), ["xe-2/0/3"])

    def test_a_re_apply_after_leaving_a_vc_matches_the_drifted_interface(self):
        """Off the VC the templates resolve to the fallback, so the installed name drifts the other way."""
        module, bay = self._install_on(self.device, self.module_type, "4")
        self._leave()
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        self.assertEqual(apply_interface_name_rules(module, bay), 1)
        self.assertEqual(self._names(module), ["et-0/0/4"])

    def test_the_apply_page_path_never_consulted_raw_names(self):
        """``apply_rule_to_existing`` evaluates the names it finds, so it is drift-immune by construction."""
        module, _ = self._install_on(self.device, self.module_type, "5")
        self._leave()
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        self.assertEqual(apply_rule_to_existing(rule).changed_count, 1)
        self.assertEqual(self._names(module), ["et-0/0/5"])


# ---------------------------------------------------------------------------
# Ambiguity: a structural matcher may claim more than it should
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionAmbiguityTest(VcDriftTestCase):
    """A claim that is not globally unique renames nothing and says why — it is never guessed."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcAmb", ["3", "4"], virtual_chassis=VirtualChassis.objects.create(name="vcamb-vc"), vc_position=1
        )
        # One token template plus a plain one whose interface an earlier rename moved onto the
        # token template's fallback variant.
        cls.decoy_type = _token_module_type(
            manufacturer, "VcAmb-QSFP", "xe-{vc_position:0}/0/{module}", "mgmt-{module}"
        )
        # Two token templates whose matchers overlap on 'xe-1/0/4'.
        cls.overlap_type = _token_module_type(
            manufacturer, "VcAmb-SFP", "xe-{vc_position}/0/{module}", "xe-1/{vc_position}/{module}"
        )

    def test_a_matcher_that_claims_two_interfaces_renames_neither(self):
        """The mis-selection the review found: the false candidate is present *and* so is the real one."""
        module, bay = self._install_on(self.device, self.decoy_type, "3")
        self.assertEqual(self._names(module), ["mgmt-3", "xe-1/0/3"])
        # An earlier rename left the plain template's interface sitting on the token template's fallback name.
        rename_out_of_band(Interface.objects.get(module=module, name="mgmt-3"), "xe-0/0/3")
        self._renumber(2)
        InterfaceNameRule.objects.create(module_type=self.decoy_type, name_template="et-{base}")

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["xe-0/0/3", "xe-1/0/3"])
        output = "\n".join(logs.output)
        self.assertIn("xe-{vc_position:0}/0/{module}", output)  # which template made the ambiguous claim
        self.assertIn(str(module), output)
        for candidate in ("xe-0/0/3", "xe-1/0/3"):
            self.assertIn(candidate, output)

    def test_a_forced_re_apply_does_not_break_out_an_ambiguous_pair(self):
        """The same claim reached through the force path, where distinct targets hide the collision.

        A breakout rule that carries ``{base}`` gives each claimed base a name of its own, so nothing
        downstream refuses the second one: the guard has to be the thing that stops it.
        """
        module, _ = self._install_on(self.device, self.decoy_type, "3")
        rename_out_of_band(Interface.objects.get(module=module, name="mgmt-3"), "xe-0/0/3")
        InterfaceNameRule.objects.create(
            module_type=self.decoy_type, name_template="et-{base}:{channel}", channel_count=2, channel_start=0
        )

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            self._renumber(2)

        self.assertEqual(self._names(module), ["xe-0/0/3", "xe-1/0/3"])
        output = "\n".join(logs.output)
        self.assertIn("xe-{vc_position:0}/0/{module}", output)
        self.assertIn(str(module), output)
        for candidate in ("xe-0/0/3", "xe-1/0/3"):
            self.assertIn(candidate, output)

    def test_an_interface_claimed_by_two_templates_renames_nothing(self):
        """Two matchers over one name is the same failure seen from the other side; both claims are dropped."""
        module, bay = self._install_on(self.device, self.overlap_type, "4")
        self.assertEqual(self._names(module), ["xe-1/0/4", "xe-1/1/4"])
        self._renumber(6)
        InterfaceNameRule.objects.create(module_type=self.overlap_type, name_template="et-{base}")

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["xe-1/0/4", "xe-1/1/4"])
        self.assertIn("xe-1/0/4", "\n".join(logs.output))

    def test_two_token_templates_that_do_not_overlap_both_rename(self):
        """The guard is scoped to real ambiguity: distinct claims still each match their own interface."""
        self._renumber(5)  # instantiate away from position 1, where the two matchers would collide
        module, bay = self._install_on(self.device, self.overlap_type, "4")
        self.assertEqual(self._names(module), ["xe-1/5/4", "xe-5/0/4"])
        self._renumber(6)
        InterfaceNameRule.objects.create(module_type=self.overlap_type, name_template="et-{base}")

        self.assertEqual(apply_interface_name_rules(module, bay), 2)
        self.assertEqual(self._names(module), ["et-xe-1/5/4", "et-xe-5/0/4"])


class VcPositionAdjacentTokenTest(VcDriftTestCase):
    """Tokens with nothing between them cannot be told apart, so the template builds no matcher.

    Back-to-back tokens expand to back-to-back numeric alternatives, which backtrack for an
    unbounded time on a name that does not match. Every remaining token matches only as many
    digits as ``vc_position`` can hold.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcAdj", ["3", "4"], virtual_chassis=VirtualChassis.objects.create(name="vcadj-vc"), vc_position=1
        )
        cls.adjacent_type = _token_module_type(manufacturer, "VcAdj-QSFP", "xe-{vc_position}{vc_position}/0/{module}")
        cls.separated_type = _token_module_type(manufacturer, "VcAdj-SFP", "xe-{vc_position}/{vc_position}/{module}")
        # A template name may legally spell the marker the matcher builder inserts for itself.
        cls.sentinel_type = _token_module_type(
            manufacturer, "VcAdj-QSFP28", "xe-InrVcPositionSentinel1End-{vc_position}/{module}"
        )

    @skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
    def test_adjacent_tokens_build_no_matcher_at_all(self):
        module, _ = self._install_on(self.device, self.adjacent_type, "3")

        self.assertEqual(self._names(module), ["xe-11/0/3"])
        self.assertEqual(_raw_name_patterns(module), [])

    @skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
    def test_a_separated_token_matches_only_a_storable_position(self):
        module, _ = self._install_on(self.device, self.separated_type, "4")
        self.assertEqual(self._names(module), ["xe-1/1/4"])

        patterns = _raw_name_patterns(module)

        self.assertEqual(len(patterns), 1)
        self.assertNotIn(r"\d+", patterns[0].pattern)
        self.assertTrue(patterns[0].fullmatch("xe-1/1/4"))
        self.assertTrue(patterns[0].fullmatch("xe-2147483647/0/4"))  # the largest position NetBox stores
        self.assertIsNone(patterns[0].fullmatch("xe-12345678901/0/4"))

    @skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
    def test_a_sentinel_shaped_literal_builds_no_matcher_instead_of_raising(self):
        module, bay = self._install_on(self.device, self.sentinel_type, "3")

        self.assertEqual(self._names(module), ["xe-InrVcPositionSentinel1End-1/3"])
        self.assertEqual(_raw_name_patterns(module), [])
        self.assertEqual(apply_interface_name_rules(module, bay), 0)


# ---------------------------------------------------------------------------
# Controls: no token, and no token support
# ---------------------------------------------------------------------------


class VcPositionNoTokenControlTest(VcDriftTestCase):
    """A module type that never mentions the token must behave exactly as it did before the fix."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("VcNoTok", ["3", "4"])
        cls.module_type = _token_module_type(manufacturer, "VcNoTok-QSFP", "{module}")

    @skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
    def test_a_tokenless_module_type_builds_no_matchers(self):
        """The pattern machinery is not engaged at all — the check's positive half lives here."""
        module, _ = self._install_on(self.device, self.module_type, "3")

        self.assertTrue(engine.supports_vc_position_token())
        self.assertEqual(_raw_name_patterns(module), [])

    def test_a_tokenless_flat_family_survives_a_join_unchanged(self):
        """Exact matching still selects the family through its last path segment, and renames nothing new."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module, _ = self._install_on(self.device, self.module_type, "4")
        self.assertEqual(self._names(module), ["et-0/0/4:0", "et-0/0/4:1"])

        self._join(VirtualChassis.objects.create(name="vcnotok-vc"), 2)

        self.assertEqual(self._names(module), ["et-0/0/4:0", "et-0/0/4:1"])


class VcPositionLegacyNetboxTest(VcDriftTestCase):
    """NetBox ≤ 4.5 has no native token, so the engine must take the original exact-only code path.

    Deliberately ungated: on a release that never resolves the token the simulated state and the real
    one coincide, so every assertion here has to hold as written on 4.5 and older too.  Nothing below
    therefore spells a name NetBox resolved from the token, or asserts that support is present.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcLegacy", ["3"], virtual_chassis=VirtualChassis.objects.create(name="vclegacy-vc"), vc_position=1
        )
        cls.module_type = _token_module_type(manufacturer, "VcLegacy-SFP", "xe-{vc_position:0}/0/{module}")

    def test_the_feature_check_is_false_without_the_constant(self):
        """Probed from ``dcim.constants``, lazily — an upstream removal must flip the check, not crash."""
        with _without_vc_position_re():
            self.assertFalse(engine.supports_vc_position_token())

    def test_no_matchers_are_built_without_the_constant(self):
        module, _ = self._install_on(self.device, self.module_type, "3")

        with _without_vc_position_re():
            self.assertEqual(_raw_name_patterns(module), [])

    def test_matching_takes_the_exact_only_path_without_the_constant(self):
        """Byte-identical pre-4.6 behaviour: a name only a matcher could claim is not a candidate."""
        module, bay = self._install_on(self.device, self.module_type, "3")
        # Named explicitly, so the interface sits on a position variant whatever the release resolved.
        rename_out_of_band(Interface.objects.get(module=module), "xe-1/0/3")
        self._renumber(2)
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        with _without_vc_position_re():
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["xe-1/0/3"])


# ---------------------------------------------------------------------------
# The {module} copy trick, on a nested bay
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionNestedBayTest(VcDriftTestCase):
    """A matcher is built by resolving ``{module}`` through NetBox's own code on a copy of the template."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcNest", ["2"], virtual_chassis=VirtualChassis.objects.create(name="vcnest-vc"), vc_position=1
        )
        cls.chassis_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="VcNest-Chassis", part_number="VcNest-Chassis"
        )
        ModuleBayTemplate.objects.create(module_type=cls.chassis_type, name="LC Bay", position="1")
        cls.leaf_type = _token_module_type(manufacturer, "VcNest-LEAF", "xe-{vc_position:0}/{module}")

    def _install_leaf(self):
        """Install the chassis in the device bay and a leaf module in the chassis' own bay."""
        outer_bay = ModuleBay.objects.get(device=self.device, name="Bay 2")
        chassis = Module.objects.create(device=self.device, module_bay=outer_bay, module_type=self.chassis_type)
        inner_bay = ModuleBay.objects.get(device=self.device, module=chassis, name="LC Bay")
        with self.captureOnCommitCallbacks(execute=True):
            leaf = Module.objects.create(device=self.device, module_bay=inner_bay, module_type=self.leaf_type)
        return leaf, inner_bay

    def test_the_matcher_resolves_the_module_placeholder_of_a_nested_bay(self):
        """``{vc_position}`` and ``{module}`` in one template: only the position is left open."""
        leaf, _ = self._install_leaf()
        self._renumber(2)

        patterns = _raw_name_patterns(leaf)

        self.assertEqual(len(patterns), 1)
        self.assertTrue(patterns[0].fullmatch("xe-1/1"), patterns[0].pattern)  # the name it was instantiated with
        self.assertTrue(patterns[0].fullmatch("xe-2/1"), patterns[0].pattern)  # the name it resolves to now

    def test_a_nested_module_is_renamed_after_a_renumber(self):
        """The same thing asserted end to end, so a matcher that leaves ``{module}`` literal cannot pass."""
        leaf, _ = self._install_leaf()
        self.assertEqual(self._names(leaf), ["xe-1/1"])
        InterfaceNameRule.objects.create(
            module_type=self.leaf_type,
            name_template="et-{vc_position}/{bay_position}:{channel}",
            channel_count=1,
            channel_start=0,
        )

        self._renumber(2)

        self.assertEqual(self._names(leaf), ["et-2/1:0"])


# ---------------------------------------------------------------------------
# Prediction keeps its contract
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionPredictionTest(VcDriftTestCase):
    """``predict_rule_output`` is handed names by its caller; nothing about the fix changes that."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcPred", ["3"], virtual_chassis=VirtualChassis.objects.create(name="vcpred-vc"), vc_position=2
        )
        cls.module_type = _token_module_type(manufacturer, "VcPred-SFP", "xe-{vc_position:0}/0/{module}")

    def test_names_resolved_at_call_time_still_predict_correctly(self):
        """The documented precondition: the caller resolves the names, so same-instant input is exact."""
        module, bay = self._install_on(self.device, self.module_type, "3")
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-{base}")

        self.assertEqual(predict_rule_output(module, bay, ["xe-2/0/3"]), ["et-xe-2/0/3"])

    def test_prediction_maps_whatever_name_it_is_given(self):
        """No variant-awareness is added: a stale name predicts from itself, it is not silently corrected."""
        module, bay = self._install_on(self.device, self.module_type, "3")
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-{base}")

        self.assertEqual(predict_rule_output(module, bay, ["xe-1/0/3"]), ["et-xe-1/0/3"])


# ---------------------------------------------------------------------------
# Channelized families (NetBox 4.7+)
# ---------------------------------------------------------------------------


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionAsymmetricFamilyTest(VcDriftTestCase):
    """Only symmetric token use keeps a family in step; an asymmetric one degrades, it never guesses."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcAsym", ["3"], virtual_chassis=VirtualChassis.objects.create(name="vcasym-vc"), vc_position=1
        )
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="VcAsym-QSFP", part_number="VcAsym-QSFP"
        )
        # The token is on the parent template only: the children never drift with it.
        parent = InterfaceTemplate.objects.create(
            module_type=cls.module_type, name="xe-{vc_position:0}/0/{module}", type=PARENT_TYPE, channels=2
        )
        for channel_id in (1, 2):
            InterfaceTemplate.objects.create(
                module_type=cls.module_type,
                name=f"xe-0/0/{{module}}:{channel_id}",
                type=CHANNEL_TYPE,
                parent=parent,
                channel_id=channel_id,
            )

    def test_the_drifted_parent_is_matched_and_its_children_are_left_alone(self):
        """The parent is found structurally; the children have no derivable suffix, so they are reported."""
        module, bay = self._install_on(self.device, self.module_type, "3")
        self.assertEqual(self._names(module), ["xe-0/0/3:1", "xe-0/0/3:2", "xe-1/0/3"])
        self._renumber(2)
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 1)
        self.assertEqual(self._names(module), ["et-0/0/3", "xe-0/0/3:1", "xe-0/0/3:2"])
        output = "\n".join(logs.output)
        self.assertIn("Cannot derive a name for channel interface", output)
        self.assertIn("xe-0/0/3:1", output)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionConversionRecoveryTest(VcDriftTestCase):
    """Flat→channelized identification reads rule-output names, so it recovers the historical base."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "VcConv",
            ["3", "4", "5", "6", "7"],
            virtual_chassis=VirtualChassis.objects.create(name="vcconv-vc"),
            vc_position=1,
        )
        cls.manufacturer = manufacturer
        cls.wrap_type = _token_module_type(manufacturer, "VcConv-WRAP", "xe-{vc_position:0}/0/{module}")
        cls.twice_type = _token_module_type(manufacturer, "VcConv-TWICE", "xe-{vc_position:0}/0/{module}")
        cls.arith_type = _token_module_type(manufacturer, "VcConv-ARITH", "{vc_position}{module}")
        cls.free_type = _token_module_type(manufacturer, "VcConv-FREE", "xe-{vc_position:0}/0/{module}")
        cls.two_base_type = _token_module_type(
            manufacturer, "VcConv-TWOBASE", "xe-{vc_position:0}/0/{module}", "xe-{vc_position:9}/0/{module}"
        )

    @staticmethod
    def _flat_rule(module_type, name_template):
        return InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template=name_template,
            breakout_mode=FLAT,
            channel_count=4,
            channel_start=0,
        )

    @staticmethod
    def _switch_to_channelized(rule, parent_name_template="et-0/0/{bay_position}"):
        rule.breakout_mode = CHANNELIZED
        rule.parent_name_template = parent_name_template
        rule.save()
        return rule

    def test_a_renumbered_family_is_identified_through_its_wrapped_base(self):
        """``brk-{base}:{channel}`` buries the raw name, so identification has to recover it by capture."""
        rule = self._flat_rule(self.wrap_type, "brk-{base}:{channel}")
        module, _ = self._install_on(self.device, self.wrap_type, "3")
        self.assertEqual(self._names(module), [f"brk-xe-1/0/3:{channel}" for channel in range(4)])
        self._renumber(2)
        self.assertEqual(self._names(module), [f"brk-xe-1/0/3:{channel}" for channel in range(4)])
        self._switch_to_channelized(rule)

        candidates = find_convertible_families(rule).candidates

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].convertible, candidates[0].reason)
        self.assertEqual(list(candidates[0].current_names), [f"brk-xe-1/0/3:{channel}" for channel in range(4)])
        self.assertEqual(candidates[0].new_names[0], "et-0/0/3")

    def test_a_template_that_repeats_the_base_is_recovered_through_a_backreference(self):
        """One capture group and a backreference — a repeated ``{base}`` must not become a second group."""
        rule = self._flat_rule(self.twice_type, "brk-{base}-{base}:{channel}")
        module, _ = self._install_on(self.device, self.twice_type, "4")
        self.assertEqual(self._names(module), [f"brk-xe-1/0/4-xe-1/0/4:{channel}" for channel in range(4)])
        self._renumber(2)
        self._switch_to_channelized(rule)

        candidates = find_convertible_families(rule).candidates

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].convertible, candidates[0].reason)
        self.assertEqual(candidates[0].new_names[0], "et-0/0/4")

    def test_a_base_inside_an_arithmetic_expression_is_skipped_cleanly(self):
        """A non-numeric sentinel cannot go through the arithmetic validator; the family is simply not offered."""
        rule = self._flat_rule(self.arith_type, "p{1 + {base}}:{channel}")
        module, _ = self._install_on(self.device, self.arith_type, "5")
        self.assertEqual(self._names(module), [f"p16:{channel}" for channel in range(4)])
        self._renumber(2)
        self._switch_to_channelized(rule)

        preview = find_convertible_families(rule)

        self.assertEqual(preview.candidates, ())
        self.assertFalse(preview.has_more)
        self.assertEqual(self._names(module), [f"p16:{channel}" for channel in range(4)])

    def test_a_family_matcher_that_captures_two_bases_is_not_offered(self):
        """Conversion rewrites rows an operator owns, so an ambiguous recovery offers nothing at all."""
        rule = self._flat_rule(self.two_base_type, "brk-{base}:{channel}")
        standalone = Device.objects.create(
            name="vcconv-sw2",
            device_type=self.device.device_type,
            role=self.device.role,
            site=self.device.site,
        )
        module, _ = self._install_on(standalone, self.two_base_type, "6")
        self.assertEqual(
            self._names(module),
            sorted(f"brk-xe-{fallback}/0/6:{channel}" for fallback in ("0", "9") for channel in range(4)),
        )
        self._join(VirtualChassis.objects.create(name="vcconv-vc2"), 5, device=standalone)
        self._switch_to_channelized(rule)

        self.assertEqual(find_convertible_families(rule).candidates, ())

    def test_a_rule_without_a_base_is_identified_after_a_renumber(self):
        """Drift-immune by construction — asserted, not assumed, so the fix cannot regress it."""
        rule = self._flat_rule(self.free_type, "et-0/0/{bay_position}:{channel}")
        module, _ = self._install_on(self.device, self.free_type, "7")
        self.assertEqual(self._names(module), [f"et-0/0/7:{channel}" for channel in range(4)])
        self._renumber(2)
        self._switch_to_channelized(rule, parent_name_template="pe-0/0/{bay_position}")

        candidates = find_convertible_families(rule).candidates

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].convertible, candidates[0].reason)
        self.assertEqual(candidates[0].new_names[0], "pe-0/0/7")
