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

Two names are pinned here as the fix's public surface:

* ``engine.supports_vc_position_token()`` — the feature check, probed lazily from
  ``dcim.constants.VC_POSITION_RE`` the way ``supports_channelization()`` probes the Interface model.
* the ``historical_pattern`` of each ``family.resolved_template_names(module)`` entry: the structural
  matchers, one per interface template whose name carries the token, None for every other template
  and on every release without the constant.

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
from django.contrib.contenttypes.models import ContentType
from extras.models import JournalEntry

from netbox_interface_name_rules import engine
from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    apply_rule_to_existing,
    find_convertible_families,
    find_interfaces_for_rule,
    predict_rule_output,
    supports_channelization,
    supports_vc_position_token,
)
from netbox_interface_name_rules.family import UNCLAIMED_BASE_REASON, plan_installed_families, resolved_template_names
from netbox_interface_name_rules.family.template_names import BAY_CHAIN_RELATIONS, pinned_template_cache
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.naming import build_variables
from netbox_interface_name_rules.rename_triggers import ModuleTrigger, reapply
from netbox_interface_name_rules.tests.helpers import (
    CHANNEL_TYPE,
    CHANNELIZED,
    FLAT,
    PARENT_TYPE,
    PLAIN_TYPE,
    PLUGIN_LOGGER,
    REQUIRES_CHANNELIZATION,
    REQUIRES_VC_POSITION_TOKEN,
    VcDriftTestCase,
    build_device,
    token_module_type,
)
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band

CLAIM_LOGGER = "netbox_interface_name_rules.family.raw_bases"


def _raw_name_patterns(module):
    """Return the structural raw-name matchers of *module*'s templates (see the module docstring)."""
    return [
        template.historical_pattern
        for template in resolved_template_names(module)
        if template.historical_pattern is not None
    ]


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


# ---------------------------------------------------------------------------
# Join: the device was standalone when NetBox named the interfaces
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionJoinDriftTest(VcDriftTestCase):
    """Interfaces instantiated outside a VC keep the fallback name after the device joins one."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device("VcJoin", ["3", "4"])
        cls.module_type = token_module_type(manufacturer, "VcJoin-QSFP", "xe-{vc_position:0}/0/{module}")

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
        module, _ = self._install_on(self.device, self.module_type, "4")
        self._join(VirtualChassis.objects.create(name="vcjoin-vc2"), 2)
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        reapply([ModuleTrigger.after_install(module)])

        self.assertEqual(self._names(module), ["et-0/0/4"])


# ---------------------------------------------------------------------------
# Renumber: the position the interfaces were named with is gone
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionRenumberDriftTest(VcDriftTestCase):
    """A historical position is neither the current one nor the fallback — enumerating values misses it."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device(
            "VcRenum", ["3", "5"], virtual_chassis=VirtualChassis.objects.create(name="vcrenum-vc"), vc_position=1
        )
        cls.module_type = token_module_type(manufacturer, "VcRenum-QSFP", "xe-{vc_position:0}/0/{module}")
        cls.simple_type = token_module_type(manufacturer, "VcRenum-SFP", "xe-{vc_position:0}/0/{module}")

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
    """A forced reapply finds a breakout family by the names the rule gives it at any position."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device(
            "VcForm", ["5", "7"], virtual_chassis=VirtualChassis.objects.create(name="vcform-vc"), vc_position=1
        )
        cls.module_type = token_module_type(manufacturer, "VcForm-QSFP", "{vc_position}-{module}")

    def _breakout_rule(self, name_template):
        return InterfaceNameRule.objects.create(
            module_type=self.module_type, name_template=name_template, channel_count=1, channel_start=0
        )

    def test_a_family_whose_name_ends_in_the_raw_name_is_renamed(self):
        self._breakout_rule("et-0/0/{vc_position}-{bay_position}:{channel}")
        module, _ = self._install_on(self.device, self.module_type, "5")
        self.assertEqual(self._names(module), ["et-0/0/1-5:0"])

        self._renumber(2)

        self.assertEqual(self._names(module), ["et-0/0/2-5:0"])

    def test_a_family_whose_name_buries_the_raw_name_is_renamed_too(self):
        """The rule gives this name at position 1, so the claim finds it wherever the raw name sits."""
        self._breakout_rule("et-0/0/{vc_position}-{bay_position}-x:{channel}")
        module, _ = self._install_on(self.device, self.module_type, "7")
        self.assertEqual(self._names(module), ["et-0/0/1-7-x:0"])

        self._renumber(2)

        self.assertEqual(self._names(module), ["et-0/0/2-7-x:0"])


# ---------------------------------------------------------------------------
# Leave: no signal fires, and the manual paths have to cope
# ---------------------------------------------------------------------------


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionLeaveDriftTest(VcDriftTestCase):
    """Leaving a VC renames nothing, and a later re-apply must still match the drifted name."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device(
            "VcLeave", ["3", "4", "5"], virtual_chassis=VirtualChassis.objects.create(name="vcleave-vc"), vc_position=2
        )
        cls.module_type = token_module_type(manufacturer, "VcLeave-SFP", "xe-{vc_position:0}/0/{module}")

    def test_leaving_a_vc_renames_nothing(self):
        """Deliberate: what the interfaces are called off a VC is an operator decision, even for a rule
        that needs no ``{vc_position}``."""
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
        manufacturer, cls.device = build_device(
            "VcAmb", ["3", "4"], virtual_chassis=VirtualChassis.objects.create(name="vcamb-vc"), vc_position=1
        )
        # One token template plus a plain one whose interface an earlier rename moved onto the
        # token template's fallback variant.
        cls.decoy_type = token_module_type(manufacturer, "VcAmb-QSFP", "xe-{vc_position:0}/0/{module}", "mgmt-{module}")
        # Two token templates whose matchers overlap on 'xe-1/0/4'.
        cls.overlap_type = token_module_type(
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

    def test_the_claim_warns_once_and_names_the_template_and_both_candidates(self):
        module, bay = self._install_on(self.device, self.decoy_type, "3")
        rename_out_of_band(Interface.objects.get(module=module, name="mgmt-3"), "xe-0/0/3")
        self._renumber(2)
        InterfaceNameRule.objects.create(module_type=self.decoy_type, name_template="et-{base}")

        with self.assertLogs(CLAIM_LOGGER, level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["xe-0/0/3", "xe-1/0/3"])
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [
                (
                    f"Interface template 'xe-{{vc_position:0}}/0/{{module}}' of {module} could name any of "
                    "['xe-0/0/3', 'xe-1/0/3'] as its raw name or its renamed form; "
                    "skipping them all rather than renaming a guess."
                )
            ],
        )

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

    def test_a_forced_re_apply_does_not_break_out_an_ambiguous_pair_with_one_target(self):
        """The same claim where both bases intend one family, so nothing downstream tells them apart.

        A breakout rule without ``{base}`` gives every base on the module the same names, so the two
        claimed rows collapse into one family before anything is written.  The guard has to refuse
        the pair while it can still see both of them.
        """
        module, _ = self._install_on(self.device, self.decoy_type, "3")
        rename_out_of_band(Interface.objects.get(module=module, name="mgmt-3"), "xe-0/0/3")
        InterfaceNameRule.objects.create(
            module_type=self.decoy_type,
            name_template="et-0/0/{bay_position}:{channel}",
            channel_count=2,
            channel_start=0,
        )

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            self._renumber(2)

        self.assertEqual(self._names(module), ["xe-0/0/3", "xe-1/0/3"])
        output = "\n".join(logs.output)
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

    def test_a_rule_without_base_renames_a_refused_raw_name_on_install_as_apply_rules_does(self):
        """A rule that does not read ``{base}`` needs no template, so the claim refuses nothing it would use."""
        modules = []
        for position in ("3", "4"):
            module, _ = self._install_on(self.device, self.decoy_type, position)
            rename_out_of_band(Interface.objects.get(module=module, name=f"mgmt-{position}"), f"xe-0/0/{position}")
            modules.append(module)
        self._renumber(2)
        rule = InterfaceNameRule.objects.create(module_type=self.decoy_type, name_template="et-0/0/{bay_position}")
        installed, applied = modules

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING"):
            reapply([ModuleTrigger.after_install(installed)])
            outcome = apply_rule_to_existing(
                rule, interface_ids=Interface.objects.filter(module=applied).values_list("pk", flat=True)
            )

        (entry,) = JournalEntry.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(installed), assigned_object_id=installed.pk
        )
        self.assertIn("to `et-0/0/3`: target name is already in use", entry.comments)
        self.assertEqual(outcome.changed_count, 1)
        self.assertEqual([member.reason for member in outcome.skipped_members], ["target name is already in use"])
        for module, position in ((installed, "3"), (applied, "4")):
            with self.subTest(position=position):
                names = self._names(module)
                self.assertIn(f"et-0/0/{position}", names)
                self.assertEqual(len(names), 2)

    def test_two_token_templates_that_do_not_overlap_both_rename(self):
        """The guard is scoped to real ambiguity: distinct claims still each match their own interface.

        ``{base}`` is the raw name each template resolves to now, at position 6.
        """
        self._renumber(5)  # instantiate away from position 1, where the two matchers would collide
        module, bay = self._install_on(self.device, self.overlap_type, "4")
        self.assertEqual(self._names(module), ["xe-1/5/4", "xe-5/0/4"])
        self._renumber(6)
        InterfaceNameRule.objects.create(module_type=self.overlap_type, name_template="et-{base}")

        self.assertEqual(apply_interface_name_rules(module, bay), 2)
        self.assertEqual(self._names(module), ["et-xe-1/6/4", "et-xe-6/0/4"])


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class VcPositionOneClaimPassTest(VcDriftTestCase):
    """One claim over every form refuses a template that claims its raw name and its earlier family.

    The flat rule ``{base}:{channel}`` named the family ``1/0:0``, ``1/0:1`` at position 1. After a renumber
    to 3, a plain interface carries the template's new raw name ``3/0``. The template claims both, so every
    path keeps all three names and reports them.
    """

    CANDIDATES = ("1/0:0", "1/0:1", "3/0")

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device(
            "VcPass", ["0"], virtual_chassis=VirtualChassis.objects.create(name="vcpass-vc"), vc_position=1
        )
        cls.module_type = token_module_type(manufacturer, "VcPass-QSFP", "{vc_position}/{module}")

    def _flat_rule(self):
        return InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            breakout_mode=FLAT,
            channel_count=2,
            channel_start=0,
        )

    def _add_plain_interface(self, module, name):
        Interface.objects.create(device=self.device, module=module, name=name, type=PLAIN_TYPE)

    def _candidates_without_a_rule(self):
        """Leave the family named at position 1 and the raw name of position 3, with no rule yet."""
        module, bay = self._install_on(self.device, self.module_type, "0")
        rename_out_of_band(Interface.objects.get(module=module, name="1/0"), "1/0:0")
        self._add_plain_interface(module, "1/0:1")
        self._renumber(3)
        self._add_plain_interface(module, "3/0")
        return module, bay

    def _assert_the_claim_names_every_candidate(self, logs, module):
        output = "\n".join(logs.output)
        self.assertIn("{vc_position}/{module}", output)
        self.assertIn(str(module), output)
        for candidate in self.CANDIDATES:
            self.assertIn(candidate, output)

    def _assert_every_candidate_is_reported(self, target):
        (entry,) = JournalEntry.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(target), assigned_object_id=target.pk
        )
        for candidate in self.CANDIDATES:
            self.assertIn(f"`{candidate}`: {UNCLAIMED_BASE_REASON}", entry.comments)

    def test_the_virtual_chassis_reapply_keeps_both_candidates_and_reports_them(self):
        self._flat_rule()
        module, _ = self._install_on(self.device, self.module_type, "0")
        self.assertEqual(self._names(module), ["1/0:0", "1/0:1"])
        self._add_plain_interface(module, "3/0")

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            self._renumber(3)

        self.assertEqual(self._names(module), list(self.CANDIDATES))
        self._assert_the_claim_names_every_candidate(logs, module)
        self._assert_every_candidate_is_reported(self.device)

    def test_an_install_reapply_keeps_both_candidates_and_reports_the_raw_name(self):
        module, _ = self._candidates_without_a_rule()
        self._flat_rule()

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            reapply([ModuleTrigger.after_install(module)])

        self.assertEqual(self._names(module), list(self.CANDIDATES))
        self._assert_the_claim_names_every_candidate(logs, module)
        (entry,) = JournalEntry.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(module), assigned_object_id=module.pk
        )
        # An install touches only interfaces that still carry a raw name.
        self.assertIn(f"`3/0`: {UNCLAIMED_BASE_REASON}", entry.comments)
        self.assertNotIn("1/0:", entry.comments)

    def test_apply_rules_keeps_both_candidates_and_reports_them(self):
        module, _ = self._candidates_without_a_rule()
        rule = self._flat_rule()

        self.assertEqual(find_interfaces_for_rule(rule), ([], 3))
        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            outcome = apply_rule_to_existing(rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(self._names(module), list(self.CANDIDATES))
        self._assert_the_claim_names_every_candidate(logs, module)
        self.assertEqual(
            sorted((member.current_name, member.reason) for member in outcome.skipped_members),
            [(candidate, UNCLAIMED_BASE_REASON) for candidate in self.CANDIDATES],
        )


class VcPositionAdjacentTokenTest(VcDriftTestCase):
    """Tokens with nothing between them cannot be told apart, so the template builds no matcher.

    Back-to-back tokens expand to back-to-back numeric alternatives, which backtrack for an
    unbounded time on a name that does not match. Every remaining token matches only as many
    digits as ``vc_position`` can hold.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device(
            "VcAdj", ["3", "4"], virtual_chassis=VirtualChassis.objects.create(name="vcadj-vc"), vc_position=1
        )
        cls.adjacent_type = token_module_type(manufacturer, "VcAdj-QSFP", "xe-{vc_position}{vc_position}/0/{module}")
        cls.separated_type = token_module_type(manufacturer, "VcAdj-SFP", "xe-{vc_position}/{vc_position}/{module}")

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
class VcPositionResolutionTest(VcDriftTestCase):
    """A resolved template gives the name that NetBox resolves at another chassis position or off a chassis."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device(
            "VcRes", ["3"], virtual_chassis=VirtualChassis.objects.create(name="vcres-vc"), vc_position=1
        )
        cls.module_type = token_module_type(
            manufacturer,
            "VcRes-QSFP",
            "xe-{vc_position}/{module}",
            "ge-{vc_position:9}/{module}",
            "et-{vc_position}{vc_position:7}/0/{module}",
            "mgmt{module}",
        )
        # NetBox resolves {module} first, so this bay brings one more token into each name.
        ModuleBay.objects.create(device=cls.device, name="Bay T", position="{vc_position}")
        cls.card_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="VcRes-Card", part_number="VcRes-Card"
        )
        ModuleBayTemplate.objects.create(module_type=cls.card_type, name="LC Bay", position="1")

    def test_a_template_at_another_position_reads_nothing_and_leaves_the_module_and_its_device_alone(self):
        module, _ = self._install_on(self.device, self.module_type, "T")
        read = Module.objects.select_related(*BAY_CHAIN_RELATIONS).get(pk=module.pk)
        with pinned_template_cache([read]):
            entries = resolved_template_names(read)
        device = read.device

        with self.assertNumQueries(0):
            names = {
                position: [entry.at_chassis_position(position).resolved for entry in entries]
                for position in (1, 4, None)
            }

        self.assertEqual(names[1], [entry.resolved for entry in entries])
        self.assertEqual(names[4], ["xe-4/4", "ge-4/4", "et-44/0/4", "mgmt{vc_position}"])
        self.assertEqual(names[None], ["xe-0/0", "ge-9/0", "et-07/0/0", "mgmt{vc_position}"])
        self.assertIs(read.device, device)
        self.assertEqual((device.vc_position, device.virtual_chassis_id), (1, self.device.virtual_chassis_id))

    def test_a_nested_template_at_another_position_reads_nothing(self):
        card, _ = self._install_on(self.device, self.card_type, "3")
        inner_bay = ModuleBay.objects.get(device=self.device, module=card, name="LC Bay")
        with self.captureOnCommitCallbacks(execute=True):
            leaf = Module.objects.create(device=self.device, module_bay=inner_bay, module_type=self.module_type)
        read = Module.objects.select_related(*BAY_CHAIN_RELATIONS).get(pk=leaf.pk)
        with pinned_template_cache([read]):
            entries = resolved_template_names(read)

        with self.assertNumQueries(0):
            names = [entry.at_chassis_position(4).resolved for entry in entries]

        self.assertEqual(names, ["xe-4/1", "ge-4/1", "et-44/0/1", "mgmt1"])
        self.assertEqual(read.device.vc_position, 1)


# ---------------------------------------------------------------------------
# Controls: no token, and no token support
# ---------------------------------------------------------------------------


class VcPositionNoTokenControlTest(VcDriftTestCase):
    """A module type that never mentions the token must behave exactly as it did before the fix."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device("VcNoTok", ["3", "4"])
        cls.module_type = token_module_type(manufacturer, "VcNoTok-QSFP", "{module}")

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
        manufacturer, cls.device = build_device(
            "VcLegacy", ["3"], virtual_chassis=VirtualChassis.objects.create(name="vclegacy-vc"), vc_position=1
        )
        cls.module_type = token_module_type(manufacturer, "VcLegacy-SFP", "xe-{vc_position:0}/0/{module}")

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
        manufacturer, cls.device = build_device(
            "VcNest", ["2"], virtual_chassis=VirtualChassis.objects.create(name="vcnest-vc"), vc_position=1
        )
        cls.chassis_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="VcNest-Chassis", part_number="VcNest-Chassis"
        )
        ModuleBayTemplate.objects.create(module_type=cls.chassis_type, name="LC Bay", position="1")
        cls.leaf_type = token_module_type(manufacturer, "VcNest-LEAF", "xe-{vc_position:0}/{module}")

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
        manufacturer, cls.device = build_device(
            "VcPred", ["3"], virtual_chassis=VirtualChassis.objects.create(name="vcpred-vc"), vc_position=2
        )
        cls.module_type = token_module_type(manufacturer, "VcPred-SFP", "xe-{vc_position:0}/0/{module}")

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
        manufacturer, cls.device = build_device(
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
        manufacturer, cls.device = build_device(
            "VcConv",
            ["3", "4", "5", "6", "7"],
            virtual_chassis=VirtualChassis.objects.create(name="vcconv-vc"),
            vc_position=1,
        )
        cls.manufacturer = manufacturer
        cls.wrap_type = token_module_type(manufacturer, "VcConv-WRAP", "xe-{vc_position:0}/0/{module}")
        cls.twice_type = token_module_type(manufacturer, "VcConv-TWICE", "xe-{vc_position:0}/0/{module}")
        cls.arith_type = token_module_type(manufacturer, "VcConv-ARITH", "{vc_position}{module}")
        cls.free_type = token_module_type(manufacturer, "VcConv-FREE", "xe-{vc_position:0}/0/{module}")
        cls.two_base_type = token_module_type(
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
        self._switch_to_channelized(rule)
        self._renumber(2)
        self.assertEqual(self._names(module), [f"brk-xe-1/0/3:{channel}" for channel in range(4)])

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
        self._switch_to_channelized(rule)
        self._renumber(2)

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

        with self.assertLogs(CLAIM_LOGGER, level="WARNING") as logs:
            self.assertEqual(find_convertible_families(rule).candidates, ())

        self.assertIn(
            f"Interface 'brk-xe-0/0/6:0' on {module} could be the raw or renamed name of any of the templates "
            "['xe-{vc_position:0}/0/{module}', 'xe-{vc_position:9}/0/{module}']",
            " ".join(logs.output),
        )

    def test_an_unrelated_family_survives_overlapping_historical_claims(self):
        """A multi-base claim rejects its shared base but leaves an unrelated family available."""
        module_type = token_module_type(
            self.manufacturer,
            "VcConv-OVERLAP",
            "xe-{vc_position}/0/{module}",
            "xe-1/{vc_position}/{module}",
            "et-{vc_position}/0/{module}",
        )
        rule = self._flat_rule(module_type, "brk-{base}:{channel}")
        module, _ = self._install_on(self.device, module_type, "3")
        for channel in range(4):
            rename_out_of_band(
                Interface.objects.get(module=module, name=f"brk-xe-1/1/3:{channel}"),
                f"brk-xe-2/0/3:{channel}",
            )
        self._switch_to_channelized(rule)
        self._renumber(5)

        with self.assertLogs(CLAIM_LOGGER, level="WARNING") as logs:
            candidates = find_convertible_families(rule).candidates

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].convertible, candidates[0].reason)
        self.assertEqual(list(candidates[0].current_names), [f"brk-et-1/0/3:{channel}" for channel in range(4)])
        self.assertEqual(candidates[0].new_names[0], "et-0/0/3")
        self.assertIn(
            f"Interface 'brk-xe-1/0/3:0' on {module} could be the raw or renamed name of any of the templates "
            "['xe-1/{vc_position}/{module}', 'xe-{vc_position}/0/{module}']",
            " ".join(logs.output),
        )

    def test_a_family_a_current_and_a_historical_base_both_spell_is_not_offered(self):
        """The rule gives the family to one template now and to the other at position 1: neither converts it."""
        module_type = token_module_type(
            self.manufacturer,
            "VcConv-CURRENT-FIRST",
            "xe-{vc_position:0}/0/{module}",
            "xe-1/0/{module}",
        )
        rule = self._flat_rule(module_type, "brk-{base}:{channel}")
        self._renumber(2)
        module, _ = self._install_on(self.device, module_type, "3")
        Interface.objects.filter(module=module, name__contains="xe-2/0/3").delete()
        self._switch_to_channelized(rule, parent_name_template="parent-{base}")

        with self.assertLogs(CLAIM_LOGGER, level="WARNING") as logs:
            self.assertEqual(find_convertible_families(rule).candidates, ())

        self.assertIn(
            f"Interface 'brk-xe-1/0/3:0' on {module} could be the raw or renamed name of any of the templates "
            "['xe-1/0/{module}', 'xe-{vc_position:0}/0/{module}']",
            " ".join(logs.output),
        )

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

    def test_a_family_a_current_and_a_historical_base_both_spell_is_not_planned(self):
        """One claim over every form: a name the rule gives two templates is neither template's."""
        module_type = token_module_type(
            self.manufacturer,
            "VcConv-CURRENT-OVERLAP",
            "xe-{vc_position:0}/0/{module}",
            "xe-1/0/{module}",
        )
        rule = self._flat_rule(module_type, "brk-{base}:{channel}")
        self._renumber(2)
        module, _ = self._install_on(self.device, module_type, "3")
        # Leaves the tokenized template one historical base: the other template's current base.
        Interface.objects.filter(module=module, name__contains="xe-2/0/3").delete()
        variables = build_variables(module.module_bay, device=module.device)

        with self.assertLogs(CLAIM_LOGGER, level="WARNING"):
            plans = plan_installed_families(module, rule, variables).plans

        self.assertEqual(plans, ())
