# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests that a module rule reads ``{base}`` as the raw template name on every apply.

NetBox keeps no link from an interface to the template that created it, so a reapply recovers the
raw name by a claim: the raw name itself, or the name the rule gives it. Everything runs through
real module installs, real ``Device.save()`` calls and the real Apply Rules entry points.
"""

from unittest import skipUnless

from dcim.models import Interface, InterfaceTemplate, ModuleType, VirtualChassis

from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    apply_rule_to_existing,
    find_interfaces_for_rule,
    supports_channelization,
    supports_vc_position_token,
)
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_breakout_mode import CHANNELIZED, FLAT, _plain_module_type
from netbox_interface_name_rules.tests.test_channelization import (
    PLAIN_TYPE,
    PLUGIN_LOGGER,
    REQUIRES_CHANNELIZATION,
    _build_device,
    _channelized_module_type,
)
from netbox_interface_name_rules.tests.test_vc_drift import (
    REQUIRES_VC_POSITION_TOKEN,
    VcDriftTestCase,
    _token_module_type,
)


class RawBasePlainRenameTest(VcDriftTestCase):
    """A plain module rename reads the raw template name, so no reapply grows it."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "RawBase",
            ["3", "4", "5", "6", "7"],
            virtual_chassis=VirtualChassis.objects.create(name="rawbase-vc"),
            vc_position=1,
        )
        cls.module_type = _plain_module_type(manufacturer, "RawBase-SFP", PLAIN_TYPE)
        cls.rule = InterfaceNameRule.objects.create(module_type=cls.module_type, name_template="{base}-x")
        cls.vc_type = _plain_module_type(manufacturer, "RawBase-VC", PLAIN_TYPE)
        InterfaceNameRule.objects.create(module_type=cls.vc_type, name_template="{base}.{vc_position}")
        cls.fixed_type = _plain_module_type(manufacturer, "RawBase-FIXED", PLAIN_TYPE)
        cls.fixed_rule = InterfaceNameRule.objects.create(
            module_type=cls.fixed_type, name_template="et-0/0/{bay_position}"
        )
        cls.twin_type = ModuleType.objects.create(manufacturer=manufacturer, model="RawBase-TWIN")
        InterfaceTemplate.objects.create(module_type=cls.twin_type, name="{module}", type=PLAIN_TYPE)
        InterfaceTemplate.objects.create(module_type=cls.twin_type, name="{module}1", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(module_type=cls.twin_type, name_template="p{base}{vc_position}")
        cls.flat_type = _plain_module_type(manufacturer, "RawBase-FLAT", PLAIN_TYPE)
        cls.flat_rule = InterfaceNameRule.objects.create(
            module_type=cls.flat_type,
            name_template="{base}:{channel}",
            breakout_mode=FLAT,
            channel_count=2,
            channel_start=0,
        )
        cls.arithmetic_type = _plain_module_type(manufacturer, "RawBase-ARITH", PLAIN_TYPE)
        InterfaceNameRule.objects.create(module_type=cls.arithmetic_type, name_template="{{base} + 100}")
        cls.vc_arithmetic_type = _plain_module_type(manufacturer, "RawBase-VCARITH", PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            module_type=cls.vc_arithmetic_type, name_template="{{base} + 100}.{vc_position}"
        )
        cls.literal_marker_type = _plain_module_type(manufacturer, "RawBase-LITERAL", PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            module_type=cls.literal_marker_type, name_template="{base}-InrRawBaseMark0.{vc_position}"
        )
        cls.assembled_marker_type = _plain_module_type(manufacturer, "RawBase-ASSEMBLED", PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            module_type=cls.assembled_marker_type, name_template="{base}-InrRawBaseMark{0}.{vc_position}"
        )
        cls.marker_type = ModuleType.objects.create(manufacturer=manufacturer, model="RawBase-MARK")
        InterfaceTemplate.objects.create(module_type=cls.marker_type, name="InrRawBaseMark", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(module_type=cls.marker_type, name_template="{base}-x")
        cls.unevaluable_type = ModuleType.objects.create(manufacturer=manufacturer, model="RawBase-UNEVAL")
        InterfaceTemplate.objects.create(module_type=cls.unevaluable_type, name="xe-{module}", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(module_type=cls.unevaluable_type, name_template="{{base} + 1}")

    def test_a_forced_reapply_renames_nothing(self):
        module, bay = self._install_on(self.device, self.module_type, "3")
        self.assertEqual(self._names(module), ["3-x"])

        self.assertEqual(apply_interface_name_rules(module, bay, force_reapply=True), 0)
        self.assertEqual(self._names(module), ["3-x"])
        self.assertFalse(self.rule.tags.filter(slug="potentially-deprecated").exists())

    def test_a_forced_reapply_of_an_applied_rule_does_not_flag_it(self):
        module, bay = self._install_on(self.device, self.fixed_type, "7")

        self.assertEqual(apply_interface_name_rules(module, bay, force_reapply=True), 0)
        self.assertFalse(self.fixed_rule.tags.filter(slug="potentially-deprecated").exists())

    def test_apply_rules_renames_nothing_and_previews_nothing(self):
        module, _ = self._install_on(self.device, self.module_type, "3")

        self.assertEqual(find_interfaces_for_rule(self.rule), ([], 1))
        self.assertEqual(apply_rule_to_existing(self.rule).changed_count, 0)
        self.assertEqual(self._names(module), ["3-x"])

    def test_a_renumber_moves_the_name_to_the_new_position(self):
        module, _ = self._install_on(self.device, self.vc_type, "4")
        self.assertEqual(self._names(module), ["4.1"])

        self._renumber(2)

        self.assertEqual(self._names(module), ["4.2"])

    def test_an_interface_no_template_claims_keeps_its_name(self):
        module, bay = self._install_on(self.device, self.module_type, "5")
        rename_out_of_band(Interface.objects.get(module=module), "custom")

        self.assertEqual(find_interfaces_for_rule(self.rule)[0], [])
        with self.assertLogs(PLUGIN_LOGGER, "WARNING") as logs:
            apply_interface_name_rules(module, bay, force_reapply=True)

        self.assertEqual(self._names(module), ["custom"])
        self.assertIn("'custom'", "\n".join(logs.output))

    def test_an_interface_two_templates_claim_keeps_its_name(self):
        module, bay = self._install_on(self.device, self.twin_type, "6")
        self.assertEqual(self._names(module), ["p61", "p611"])

        with self.assertLogs(PLUGIN_LOGGER, "WARNING") as logs:
            apply_interface_name_rules(module, bay, force_reapply=True)

        # 'p611' is raw '61' at position 1, and also raw '6' at position 11.
        self.assertEqual(self._names(module), ["p61", "p611"])
        self.assertIn("'p611'", "\n".join(logs.output))

    def test_a_flat_family_is_not_built_on_an_unclaimed_member(self):
        module, _ = self._install_on(self.device, self.flat_type, "5")
        self.assertEqual(self._names(module), ["5:0", "5:1"])
        rename_out_of_band(Interface.objects.get(module=module, name="5:0"), "custom")

        self.assertEqual(find_interfaces_for_rule(self.flat_rule)[0], [])
        self.assertEqual(apply_rule_to_existing(self.flat_rule).changed_count, 0)
        self.assertEqual(self._names(module), ["5:1", "custom"])

    def test_arithmetic_over_base_claims_the_name_it_gave(self):
        module, bay = self._install_on(self.device, self.arithmetic_type, "4")
        self.assertEqual(self._names(module), ["104"])

        self.assertEqual(apply_interface_name_rules(module, bay, force_reapply=True), 0)
        self.assertEqual(self._names(module), ["104"])

    def test_a_renumber_moves_an_arithmetic_name_to_the_new_position(self):
        module, _ = self._install_on(self.device, self.vc_arithmetic_type, "4")
        self.assertEqual(self._names(module), ["104.1"])

        self._renumber(2)

        self.assertEqual(self._names(module), ["104.2"])

    def test_a_raw_name_that_spells_the_claim_marker_is_claimed(self):
        module, bay = self._install_on(self.device, self.marker_type, "3")
        self.assertEqual(self._names(module), ["InrRawBaseMark-x"])

        self.assertEqual(apply_interface_name_rules(module, bay, force_reapply=True), 0)
        self.assertEqual(self._names(module), ["InrRawBaseMark-x"])

    def test_a_rule_that_spells_the_claim_marker_still_follows_a_renumber(self):
        module, _ = self._install_on(self.device, self.literal_marker_type, "4")
        self.assertEqual(self._names(module), ["4-InrRawBaseMark0.1"])

        self._renumber(2)

        self.assertEqual(self._names(module), ["4-InrRawBaseMark0.2"])

    def test_a_marker_the_rule_assembles_does_not_break_a_renumber(self):
        module, _ = self._install_on(self.device, self.assembled_marker_type, "4")
        self.assertEqual(self._names(module), ["4-InrRawBaseMark0.1"])

        self._renumber(2)

        self.assertEqual(self._names(module), ["4-InrRawBaseMark0.2"])

    def test_a_raw_name_the_rule_cannot_evaluate_claims_no_renamed_form(self):
        module, bay = self._install_on(self.device, self.unevaluable_type, "4")
        rename_out_of_band(Interface.objects.get(module=module), "custom")

        with self.assertLogs(PLUGIN_LOGGER, "WARNING") as logs:
            apply_interface_name_rules(module, bay, force_reapply=True)

        self.assertEqual(self._names(module), ["custom"])
        self.assertIn("no single interface template claims", "\n".join(logs.output))

    def test_a_rule_without_base_still_renames_a_hand_renamed_interface(self):
        module, _ = self._install_on(self.device, self.fixed_type, "7")
        rename_out_of_band(Interface.objects.get(module=module), "custom")

        apply_rule_to_existing(self.fixed_rule)

        self.assertEqual(self._names(module), ["et-0/0/7"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class RawBaseChannelizedFamilyTest(VcDriftTestCase):
    """An installed channelized family reads the raw template name, so no reapply renames it."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("RawBaseChan", ["3", "7", "8"])
        cls.parent_type = _plain_module_type(manufacturer, "RawBaseChan-PARENT")
        cls.parent_rule = InterfaceNameRule.objects.create(
            module_type=cls.parent_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="{base}-parent",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )
        cls.channel_type = _plain_module_type(manufacturer, "RawBaseChan-CHANNEL")
        cls.channel_rule = InterfaceNameRule.objects.create(
            module_type=cls.channel_type,
            name_template="{base}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=1,
        )
        cls.lockstep_type = _channelized_module_type(manufacturer, "RawBaseChan-LOCKSTEP")
        cls.lockstep_rule = InterfaceNameRule.objects.create(module_type=cls.lockstep_type, name_template="{base}-l")

    def _assert_reapplies_rename_nothing(self, rule, module, bay, names):
        self.assertEqual(self._names(module), names)
        self.assertEqual(apply_interface_name_rules(module, bay), 0)
        self.assertEqual(apply_interface_name_rules(module, bay, force_reapply=True), 0)
        self.assertEqual(find_interfaces_for_rule(rule)[0], [])
        self.assertEqual(apply_rule_to_existing(rule).changed_count, 0)
        self.assertEqual(self._names(module), names)

    def test_a_base_parent_template_does_not_grow(self):
        module, bay = self._install_on(self.device, self.parent_type, "7")

        self._assert_reapplies_rename_nothing(
            self.parent_rule, module, bay, ["7-parent", "xe-0/0/7:0", "xe-0/0/7:1", "xe-0/0/7:2", "xe-0/0/7:3"]
        )

    def test_a_base_channel_template_keeps_its_install_names(self):
        module, bay = self._install_on(self.device, self.channel_type, "8")

        self._assert_reapplies_rename_nothing(self.channel_rule, module, bay, ["8:1", "8:2", "8:3", "8:4", "et-0/0/8"])

    def test_a_family_whose_parent_no_template_claims_keeps_its_names(self):
        module, bay = self._install_on(self.device, self.parent_type, "7")
        rename_out_of_band(self._parent(module), "custom")

        self.assertEqual(find_interfaces_for_rule(self.parent_rule)[0], [])
        with self.assertLogs(PLUGIN_LOGGER, "WARNING") as logs:
            self.assertEqual(apply_interface_name_rules(module, bay, force_reapply=True), 0)

        self.assertEqual(self._names(module), ["custom", "xe-0/0/7:0", "xe-0/0/7:1", "xe-0/0/7:2", "xe-0/0/7:3"])
        self.assertIn("'custom'", "\n".join(logs.output))

    def test_no_family_is_built_on_an_unclaimed_plain_interface(self):
        plain_type = _plain_module_type(ModuleType.objects.get(pk=self.parent_type.pk).manufacturer, "RawBaseChan-BARE")
        module, _ = self._install_on(self.device, plain_type, "3")
        rename_out_of_band(Interface.objects.get(module=module), "custom")
        rule = InterfaceNameRule.objects.create(
            module_type=plain_type,
            name_template="{base}:{channel}",
            parent_name_template="{base}",
            breakout_mode=CHANNELIZED,
            channel_count=2,
            channel_start=0,
        )

        self.assertEqual(find_interfaces_for_rule(rule)[0], [])
        self.assertEqual(apply_rule_to_existing(rule).changed_count, 0)
        self.assertEqual(self._names(module), ["custom"])

    def test_a_lockstep_base_rename_does_not_grow(self):
        module, bay = self._install_on(self.device, self.lockstep_type, "3")

        self._assert_reapplies_rename_nothing(
            self.lockstep_rule, module, bay, ["3-l", "3-l:1", "3-l:2", "3-l:3", "3-l:4"]
        )


@skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
class RawBaseFlatFamilyPreviewTest(VcDriftTestCase):
    """The Apply Rules preview offers an installed flat family the rename that Apply Rules performs."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "RawBaseFlat", ["3"], virtual_chassis=VirtualChassis.objects.create(name="rawbaseflat-vc"), vc_position=1
        )
        cls.module_type = _token_module_type(manufacturer, "RawBaseFlat-QSFP", "xe-{vc_position:0}/0/{module}")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="brk-{base}:{channel}",
            breakout_mode=FLAT,
            channel_count=2,
            channel_start=0,
        )

    def test_an_installed_flat_family_previews_nothing_after_install(self):
        self._install_on(self.device, self.module_type, "3")

        self.assertEqual(find_interfaces_for_rule(self.rule)[0], [])

    def test_a_drifted_flat_family_previews_the_rename_apply_performs(self):
        module, _ = self._install_on(self.device, self.module_type, "3")
        self._leave()

        results, _total = find_interfaces_for_rule(self.rule)

        self.assertEqual(
            [(entry["current_name"], entry["new_names"]) for entry in results],
            [("brk-xe-1/0/3:0", ["brk-xe-0/0/3:0", "brk-xe-0/0/3:1"])],
        )
        apply_rule_to_existing(self.rule, interface_ids=[results[0]["interface"].pk])
        self.assertEqual(self._names(module), ["brk-xe-0/0/3:0", "brk-xe-0/0/3:1"])


@skipUnless(supports_channelization() and supports_vc_position_token(), REQUIRES_CHANNELIZATION)
class RawBaseDriftedCreationTest(VcDriftTestCase):
    """A family built on a drifted interface reads the raw name, but a blank parent keeps its own."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "RawBaseDrift", ["3"], virtual_chassis=VirtualChassis.objects.create(name="rawbasedrift-vc"), vc_position=1
        )
        cls.module_type = _token_module_type(manufacturer, "RawBaseDrift-QSFP", "xe-{vc_position:0}/0/{module}")

    def test_a_blank_parent_template_keeps_the_drifted_name(self):
        module, _ = self._install_on(self.device, self.module_type, "3")
        self._renumber(2)
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            breakout_mode=CHANNELIZED,
            channel_count=2,
            channel_start=0,
        )

        preview = find_interfaces_for_rule(rule)[0]
        apply_rule_to_existing(rule)

        self.assertEqual([entry["new_names"] for entry in preview], [["xe-1/0/3", "xe-2/0/3:0", "xe-2/0/3:1"]])
        self.assertEqual(self._names(module), ["xe-1/0/3", "xe-2/0/3:0", "xe-2/0/3:1"])
