# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for advanced engine functions: find_interfaces_for_rule, apply_rule_to_existing,
has_applicable_interfaces, _find_channel_base, _matching_moduletype_pks, build_variables edges."""

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    Site,
    VirtualChassis,
)
from django.test import TestCase

from netbox_interface_name_rules.engine import (
    _find_channel_base,
    _matching_moduletype_pks,
    apply_interface_name_rules,
    apply_rule_to_existing,
    build_variables,
    evaluate_name_template,
    find_interfaces_for_rule,
    has_applicable_interfaces,
)
from netbox_interface_name_rules.models import InterfaceNameRule


class EngineAdvancedFixtures(TestCase):
    """Shared fixtures for advanced engine tests."""

    @classmethod
    def setUpTestData(cls):
        """Create manufacturer, module types, device with bays, and basic modules."""
        manufacturer = Manufacturer.objects.create(name="AdvMfg", slug="advmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ADV-Switch", slug="adv-switch")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="ADV-SFP", part_number="ADV-SFP")
        cls.module_type2 = ModuleType.objects.create(
            manufacturer=manufacturer, model="ADV-QSFP", part_number="ADV-QSFP"
        )
        cls.module_type_regex = ModuleType.objects.create(
            manufacturer=manufacturer, model="QSFP-100G-LR4", part_number="QSFP-100G-LR4"
        )
        # Create bays before device
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Bay 0", position="0")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Bay 1", position="1")
        role = DeviceRole.objects.create(name="AdvRole", slug="advrole")
        site = Site.objects.create(name="AdvSite", slug="advsite")
        cls.device = Device.objects.create(name="adv-test-01", device_type=cls.device_type, role=role, site=site)
        cls.bay0 = ModuleBay.objects.get(device=cls.device, name="Bay 0")
        cls.bay1 = ModuleBay.objects.get(device=cls.device, name="Bay 1")

        # For VC tests
        vc = VirtualChassis.objects.create(name="adv-vc")
        cls.vc_device = Device.objects.create(
            name="adv-vc-sw1",
            device_type=cls.device_type,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=1,
        )
        cls.vc_bay = ModuleBay.objects.get(device=cls.vc_device, name="Bay 0")


class FindInterfacesForRuleTest(EngineAdvancedFixtures):
    """Test find_interfaces_for_rule with various rule and module configurations."""

    def test_exact_rule_finds_interface(self):
        """find_interfaces_for_rule returns entry for matching module interface."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["current_name"], "0")
        self.assertEqual(results[0]["new_names"], ["et-0/0/0"])
        self.assertEqual(total, 1)

    def test_already_correct_name_not_in_results(self):
        """Interface already named correctly is excluded from results."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="et-0/0/0", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 0)
        self.assertEqual(total, 1)

    def test_regex_rule_finds_interface(self):
        """Regex rule finds interfaces for matching module types."""
        rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-100G-.*",
            name_template="Hu0/0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type_regex)
        Interface.objects.create(device=self.device, module=module, name="0", type="100gbase-x-qsfp28")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["new_names"], ["Hu0/0/0/0"])

    def test_channel_rule_finds_module(self):
        """Channel rule (channel_count>0) finds base interface and lists expected channel names."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["new_names"], ["xe-0/0/0:0", "xe-0/0/0:1", "xe-0/0/0:2", "xe-0/0/0:3"])

    def test_limit_stops_early(self):
        """limit=1 returns at most 1 result even when multiple interfaces match."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module0 = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        module1 = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module0, name="0", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module1, name="1", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule, limit=1)
        self.assertEqual(len(results), 1)

    def test_no_modules_returns_empty(self):
        """Rule for module type that has no installed modules returns empty results."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type2,
            name_template="et-0/0/{bay_position}",
        )
        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 0)
        self.assertEqual(total, 0)


class ApplyRuleToExistingTest(EngineAdvancedFixtures):
    """Test apply_rule_to_existing retroactive bulk application."""

    def test_empty_interface_ids_returns_zero(self):
        """Explicitly empty interface_ids list returns 0 without DB access."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        result = apply_rule_to_existing(rule, interface_ids=[])
        self.assertEqual(result, 0)

    def test_disabled_rule_returns_zero(self):
        """Disabled rule returns 0 immediately."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
            enabled=False,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")
        result = apply_rule_to_existing(rule)
        self.assertEqual(result, 0)

    def test_renames_matching_interface(self):
        """apply_rule_to_existing renames interfaces matching the rule."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        result = apply_rule_to_existing(rule)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

    def test_interface_ids_filter(self):
        """Only interfaces matching interface_ids are renamed."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module0 = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        module1 = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        iface0 = Interface.objects.create(device=self.device, module=module0, name="0", type="10gbase-x-sfpp")
        iface1 = Interface.objects.create(device=self.device, module=module1, name="1", type="10gbase-x-sfpp")

        result = apply_rule_to_existing(rule, interface_ids=[iface0.pk])
        self.assertEqual(result, 1)
        iface0.refresh_from_db()
        iface1.refresh_from_db()
        self.assertEqual(iface0.name, "et-0/0/0")
        self.assertEqual(iface1.name, "1")  # untouched

    def test_channel_rule_applies_once_per_module(self):
        """Channel rule calls _apply_rule_to_interface once per module, not per interface."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        result = apply_rule_to_existing(rule)
        self.assertEqual(result, 4)
        names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(names, ["xe-0/0/0:0", "xe-0/0/0:1", "xe-0/0/0:2", "xe-0/0/0:3"])

    def test_limit_respected(self):
        """limit parameter stops processing after enough renames."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module0 = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        module1 = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module0, name="0", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module1, name="1", type="10gbase-x-sfpp")

        result = apply_rule_to_existing(rule, limit=1)
        self.assertLessEqual(result, 1)

    def test_regex_rule_applies_to_matching_module_types(self):
        """Regex rule's module_qs includes all matching module types."""
        rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-100G-.*",
            name_template="Hu0/0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type_regex)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="100gbase-x-qsfp28")

        result = apply_rule_to_existing(rule)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Hu0/0/0/0")


class HasApplicableInterfacesTest(EngineAdvancedFixtures):
    """Test has_applicable_interfaces helper."""

    def test_returns_true_when_rename_possible(self):
        """Returns True when at least one interface would be renamed."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        self.assertTrue(has_applicable_interfaces(rule))

    def test_returns_false_when_no_modules(self):
        """Returns False when no modules exist for this rule's module type."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type2,  # no modules installed with this type
            name_template="et-0/0/{bay_position}",
        )
        self.assertFalse(has_applicable_interfaces(rule))

    def test_returns_false_when_already_correct(self):
        """Returns False when all matching interfaces already have correct names."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="et-0/0/0", type="10gbase-x-sfpp")
        self.assertFalse(has_applicable_interfaces(rule))


class MatchingModuleTypePksTest(TestCase):
    """Test _matching_moduletype_pks regex lookup."""

    @classmethod
    def setUpTestData(cls):
        """Create some module types for matching tests."""
        manufacturer = Manufacturer.objects.create(name="PksMfg", slug="pksmfg")
        cls.mt_lr4 = ModuleType.objects.create(manufacturer=manufacturer, model="QSFP-100G-LR4", part_number="PK-LR4")
        cls.mt_lr8 = ModuleType.objects.create(manufacturer=manufacturer, model="QSFP-100G-LR8", part_number="PK-LR8")
        cls.mt_other = ModuleType.objects.create(manufacturer=manufacturer, model="SFP-10G-LR", part_number="PK-10G")

    def test_returns_matching_pks(self):
        """Returns PKs for module types matching the pattern."""
        pks = _matching_moduletype_pks("QSFP-100G-.*")
        self.assertIn(self.mt_lr4.pk, pks)
        self.assertIn(self.mt_lr8.pk, pks)
        self.assertNotIn(self.mt_other.pk, pks)

    def test_returns_empty_list_for_no_match(self):
        """Returns empty list when no module types match."""
        pks = _matching_moduletype_pks("NOMATCH-.*")
        self.assertEqual(pks, [])

    def test_invalid_regex_raises_value_error(self):
        """Invalid regex raises ValueError."""
        with self.assertRaises(ValueError):
            _matching_moduletype_pks("[invalid(")


class FindChannelBaseTest(EngineAdvancedFixtures):
    """Test _find_channel_base chooses the right interface for channel rules."""

    def test_prefers_already_renamed_ch0_interface(self):
        """_find_channel_base prefers an interface already named as channel 0."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # One interface is already the ch=0 name, another is still raw
        iface_ch0 = Interface.objects.create(
            device=self.device, module=module, name="xe-0/0/0:0", type="10gbase-x-sfpp"
        )
        iface_raw = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        variables = build_variables(self.bay0)
        ifaces = sorted([iface_ch0, iface_raw], key=lambda i: i.name)
        result = _find_channel_base(rule, ifaces, variables)
        self.assertEqual(result, iface_ch0)

    def test_falls_back_to_first_interface_alphabetically(self):
        """When no interface matches ch=0, returns first alphabetically (via caller-sorted list)."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        iface_a = Interface.objects.create(device=self.device, module=module, name="1", type="10gbase-x-sfpp")
        iface_b = Interface.objects.create(device=self.device, module=module, name="2", type="10gbase-x-sfpp")

        variables = build_variables(self.bay1)
        ifaces = sorted([iface_a, iface_b], key=lambda i: i.name)
        result = _find_channel_base(rule, ifaces, variables)
        self.assertEqual(result, iface_a)


class BuildVariablesEdgesTest(TestCase):
    """Test build_variables edge cases: parent bays, VC injection."""

    @classmethod
    def setUpTestData(cls):
        """Create nested device with parent bay relationship."""
        manufacturer = Manufacturer.objects.create(name="VarEdgeMfg", slug="varedgemfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="VE-Dev", slug="ve-dev")
        ModuleBayTemplate.objects.create(device_type=device_type, name="Slot 2", position="2")
        role = DeviceRole.objects.create(name="VERole", slug="verole")
        site = Site.objects.create(name="VESite", slug="vesite")
        cls.device = Device.objects.create(name="ve-test-01", device_type=device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="Slot 2")

        # VC device for vc_position injection test
        vc = VirtualChassis.objects.create(name="ve-vc")
        cls.vc_device = Device.objects.create(
            name="ve-vc-sw",
            device_type=device_type,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=3,
        )
        cls.vc_bay = ModuleBay.objects.get(device=cls.vc_device, name="Slot 2")

    def test_vc_position_injected_for_vc_member(self):
        """vc_position is present in variables when device is a VC member."""
        variables = build_variables(self.vc_bay, device=self.vc_device)
        self.assertIn("vc_position", variables)
        self.assertEqual(variables["vc_position"], "3")

    def test_vc_position_absent_for_non_vc_device(self):
        """vc_position is NOT present in variables for a non-VC device."""
        variables = build_variables(self.bay, device=self.device)
        self.assertNotIn("vc_position", variables)

    def test_vc_position_absent_when_vc_position_none(self):
        """vc_position is NOT injected when device.vc_position is None."""
        manufacturer = Manufacturer.objects.create(name="VEMfg2", slug="vemfg2")
        dt = DeviceType.objects.create(manufacturer=manufacturer, model="VE2-Dev", slug="ve2-dev")
        ModuleBayTemplate.objects.create(device_type=dt, name="Slot 0", position="0")
        role = DeviceRole.objects.create(name="VE2Role", slug="ve2role")
        site = Site.objects.create(name="VE2Site", slug="ve2site")
        vc = VirtualChassis.objects.create(name="ve2-vc")
        device_no_pos = Device.objects.create(
            name="ve2-nopos",
            device_type=dt,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=None,
        )
        bay = ModuleBay.objects.get(device=device_no_pos, name="Slot 0")
        variables = build_variables(bay, device=device_no_pos)
        self.assertNotIn("vc_position", variables)


class EvaluateNameTemplateEdgesTest(TestCase):
    """Test evaluate_name_template error branches."""

    def test_syntax_error_in_expression_raises_value_error(self):
        """Badly formed arithmetic that passes char check but fails AST → ValueError."""
        with self.assertRaises(ValueError):
            evaluate_name_template("{1 + }", {})

    def test_unsafe_ast_node_rejected(self):
        """AST node not in allowlist (e.g., function call) raises ValueError."""
        with self.assertRaises(ValueError):
            evaluate_name_template("{__import__}", {})


class ForceReapplyTest(EngineAdvancedFixtures):
    """Test force_reapply=True paths in apply_interface_name_rules."""

    def test_force_reapply_non_channel_renames_already_renamed(self):
        """force_reapply=True renames interfaces even if name doesn't match raw template."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # Interface already has the correct final name — normally would be skipped
        iface = Interface.objects.create(device=self.device, module=module, name="old-name", type="10gbase-x-sfpp")

        # Without force_reapply: skipped because "old-name" not in raw_names
        result = apply_interface_name_rules(module, self.bay0, force_reapply=False)
        self.assertEqual(result, 0)

        # With force_reapply: should apply and rename
        result = apply_interface_name_rules(module, self.bay0, force_reapply=True)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

    def test_force_reapply_channel_matches_subinterfaces_by_base_name(self):
        """force_reapply with channel rule matches sub-interfaces by base name (before ':')."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # Already-renamed channel interfaces — base "0" matches raw_name "0"
        iface0 = Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:0", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:1", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:2", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:3", type="10gbase-x-sfpp")

        # force_reapply=True: base "xe-0/0/0" does NOT match raw "0", so unrenamed=[]
        # (correctly avoiding re-creating channels when names are already correct)
        result = apply_interface_name_rules(module, self.bay0, force_reapply=True)
        # Names are already correct, so no renames needed
        self.assertEqual(result, 0)
        self.assertIsNotNone(iface0)  # Interface still exists
        iface0.refresh_from_db()
        self.assertEqual(iface0.name, "xe-0/0/0:0")  # Name unchanged


class FlagPotentiallyDeprecatedTest(EngineAdvancedFixtures):
    """Test that _flag_rule_potentially_deprecated is called on no-op renames."""

    def test_no_op_rename_adds_deprecated_tag(self):
        """When rule matches but all interfaces already have correct names, tag is added."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # Create with the final correct name (so _apply_rule_to_interface returns 0 for it)
        # But the name "et-0/0/0" is NOT in raw_names (raw = "0"), so this won't trigger deprecated.
        # Instead, set force_reapply so it's in unrenamed but produces 0 renames.
        iface = Interface.objects.create(device=self.device, module=module, name="et-0/0/0", type="10gbase-x-sfpp")

        # force_reapply=True: unrenamed=[iface], but new_name=="et-0/0/0"==iface.name → renamed=0
        apply_interface_name_rules(module, self.bay0, force_reapply=True)

        # The tag should have been added
        iface.refresh_from_db()
        tags = list(rule.tags.filter(slug="potentially-deprecated"))
        self.assertEqual(len(tags), 1)
