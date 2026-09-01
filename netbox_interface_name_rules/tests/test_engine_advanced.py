# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for advanced engine functions: find_interfaces_for_rule, apply_rule_to_existing,
has_applicable_interfaces, _matching_moduletype_pks, build_variables edges."""

from types import SimpleNamespace
from unittest.mock import patch

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    InterfaceTemplate,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    Platform,
    Site,
    VirtualChassis,
)
from django.test import TestCase

from netbox_interface_name_rules.engine import (
    _extract_trailing_digits,
    _matching_moduletype_pks,
    apply_interface_name_rules,
    apply_rule_to_existing,
    build_variables,
    evaluate_name_template,
    find_interfaces_for_rule,
    has_applicable_interfaces,
)
from netbox_interface_name_rules.family import FamilyStatus
from netbox_interface_name_rules.family.names import INTERFACE_NAME_CONSTRAINT
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
        cls.platform = Platform.objects.create(name="AdvOS", slug="advos")


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

        results, _ = find_interfaces_for_rule(rule)
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

        results, _ = find_interfaces_for_rule(rule)
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

        results, _ = find_interfaces_for_rule(rule, limit=1)
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

    def test_device_type_filter_scopes_modules(self):
        """Rule with device_type only finds modules installed on matching device."""
        # A second device type with its own device
        mfg = Manufacturer.objects.create(name="DtFilterMfg", slug="dtfiltermfg")
        other_dt = DeviceType.objects.create(manufacturer=mfg, model="OTHER-DEV", slug="other-dev")
        ModuleBayTemplate.objects.create(device_type=other_dt, name="Bay 0", position="0")
        site = Site.objects.get(name="AdvSite")
        role = DeviceRole.objects.get(name="AdvRole")
        other_device = Device.objects.create(name="other-dev-01", device_type=other_dt, role=role, site=site)
        other_bay = ModuleBay.objects.get(device=other_device, name="Bay 0")

        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="et-0/0/{bay_position}",
        )
        # Module on matching device — should appear
        m_match = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=m_match, name="0", type="10gbase-x-sfpp")
        # Module on non-matching device — should NOT appear
        m_other = Module.objects.create(device=other_device, module_bay=other_bay, module_type=self.module_type)
        Interface.objects.create(device=other_device, module=m_other, name="0", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["module"], m_match)
        self.assertEqual(total, 1)

    def test_platform_filter_scopes_modules(self):
        """Rule with platform only finds modules installed on matching-platform devices."""
        site = Site.objects.get(name="AdvSite")
        role = DeviceRole.objects.get(name="AdvRole")
        platform_device = Device.objects.create(
            name="platform-dev-01", device_type=self.device_type, role=role, site=site, platform=self.platform
        )
        platform_bay = ModuleBay.objects.get(device=platform_device, name="Bay 0")

        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            platform=self.platform,
            name_template="et-0/0/{bay_position}",
        )
        # Module on platform-matching device
        m_match = Module.objects.create(device=platform_device, module_bay=platform_bay, module_type=self.module_type)
        Interface.objects.create(device=platform_device, module=m_match, name="0", type="10gbase-x-sfpp")
        # Module on non-platform device (no platform set)
        m_other = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=m_other, name="1", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["module"], m_match)
        self.assertEqual(total, 1)

    def test_template_error_shown_in_new_names(self):
        """A template that raises ValueError produces '<error: ...>' in new_names."""
        # {vc_position} is undefined for a non-VC device, so evaluation raises ValueError.
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-{vc_position}/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["new_names"][0].startswith("<error:"))
        self.assertEqual(total, 1)

    def test_channel_rule_all_correct_not_in_results(self):
        """Channel rule excludes module when all expected channels already exist."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # Create all expected channel interfaces with already-correct names
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:0", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:1", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(len(results), 0)
        self.assertEqual(total, 2)


class ApplyRuleToExistingTest(EngineAdvancedFixtures):
    """Test apply_rule_to_existing retroactive bulk application."""

    def test_empty_interface_ids_returns_zero(self):
        """Explicitly empty interface_ids list returns 0 without DB access."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        result = apply_rule_to_existing(rule, interface_ids=[])
        self.assertEqual(result.changed_count, 0)

    def test_disabled_rule_returns_zero(self):
        """Disabled rule returns 0 immediately."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
            enabled=False,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")
        result = apply_rule_to_existing(rule)
        self.assertEqual(result.changed_count, 0)
        # Verify interface name is unchanged
        iface.refresh_from_db()
        self.assertEqual(iface.name, "0")

    def test_renames_matching_interface(self):
        """apply_rule_to_existing renames interfaces matching the rule."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        result = apply_rule_to_existing(rule)
        self.assertEqual(result.changed_count, 1)
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
        self.assertEqual(result.changed_count, 1)
        iface0.refresh_from_db()
        iface1.refresh_from_db()
        self.assertEqual(iface0.name, "et-0/0/0")
        self.assertEqual(iface1.name, "1")  # untouched

    def test_channel_rule_applies_once_per_module(self):
        """A channel rule plans one family per module, not one per interface."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        result = apply_rule_to_existing(rule)
        self.assertEqual(result.changed_count, 4)
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
        self.assertEqual(result.changed_count, 1)

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
        self.assertEqual(result.changed_count, 1)
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


class ChannelFamilyBaseTest(EngineAdvancedFixtures):
    """Two bases that intend one family's names build it once, through the base that already names it."""

    def _breakout_rule(self):
        """Return a four-channel flat breakout rule whose names ignore the base."""
        return InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )

    def test_a_half_built_family_is_completed_through_its_own_first_member(self):
        """The interface already named channel 0 owns the family; the raw port is left where it is."""
        rule = self._breakout_rule()
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:0", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        outcome = apply_rule_to_existing(rule)

        self.assertEqual(outcome.changed_count, 3)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["0", "xe-0/0/0:0", "xe-0/0/0:1", "xe-0/0/0:2", "xe-0/0/0:3"],
        )

    def test_with_no_member_named_yet_the_first_port_builds_the_family(self):
        """Nothing names the family, so its first candidate in module order builds it."""
        rule = self._breakout_rule()
        module = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="1", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="2", type="10gbase-x-sfpp")

        outcome = apply_rule_to_existing(rule)

        self.assertEqual(outcome.changed_count, 4)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["2", "xe-0/0/1:0", "xe-0/0/1:1", "xe-0/0/1:2", "xe-0/0/1:3"],
        )


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

    def test_template_expression_position_extracts_from_name(self):
        """Bay with position='{module}' (template expression) extracts number from bay name."""
        # Force the position to a template expression via low-level update to bypass form validation
        ModuleBay.objects.filter(pk=self.bay.pk).update(position="{module}")
        self.bay.refresh_from_db()
        # bay.name = "Slot 2", so trailing digit = "2"
        variables = build_variables(self.bay)
        self.assertEqual(variables["bay_position"], "2")
        self.assertEqual(variables["bay_position_num"], "2")

    def test_template_expression_position_no_digits_falls_back(self):
        """Bay with template position and name without digits → bay_position='0'."""
        manufacturer = Manufacturer.objects.create(name="VEMfg3", slug="vemfg3")
        dt = DeviceType.objects.create(manufacturer=manufacturer, model="VE3-Dev", slug="ve3-dev")
        ModuleBayTemplate.objects.create(device_type=dt, name="SlotABC", position="0")
        role = DeviceRole.objects.create(name="VE3Role", slug="ve3role")
        site = Site.objects.create(name="VE3Site", slug="ve3site")
        device = Device.objects.create(name="ve3-dev-01", device_type=dt, role=role, site=site)
        bay = ModuleBay.objects.get(device=device, name="SlotABC")
        # Force template-expression position with no digit in name
        ModuleBay.objects.filter(pk=bay.pk).update(position="{module}")
        bay.refresh_from_db()
        variables = build_variables(bay)
        self.assertEqual(variables["bay_position"], "0")


class EvaluateNameTemplateEdgesTest(TestCase):
    """Test evaluate_name_template error branches."""

    def test_syntax_error_in_expression_raises_value_error(self):
        """Badly formed arithmetic that passes char check but fails AST → ValueError."""
        with self.assertRaises(ValueError):
            evaluate_name_template("{1 + }", {})

    def test_unsafe_ast_node_rejected(self):
        """The arithmetic evaluator rejects a name lookup before it can execute."""
        with self.assertRaises(ValueError):
            evaluate_name_template("{__import__}", {})


class ExtractTrailingDigitsTest(TestCase):
    """Test _extract_trailing_digits: the ReDoS-safe trailing-digit extractor."""

    def test_pure_digits(self):
        """All-digit string returns itself."""
        self.assertEqual(_extract_trailing_digits("123"), "123")

    def test_alpha_suffix_none(self):
        """String ending in non-digit returns empty string."""
        self.assertEqual(_extract_trailing_digits("abc"), "")

    def test_mixed_trailing_digits(self):
        """Typical interface position like 'swp1' → '1'."""
        self.assertEqual(_extract_trailing_digits("swp1"), "1")

    def test_path_style(self):
        """Juniper-style position 'xe-0/0/0' → trailing '0'."""
        self.assertEqual(_extract_trailing_digits("xe-0/0/0"), "0")

    def test_empty_string(self):
        """Empty string returns empty string."""
        self.assertEqual(_extract_trailing_digits(""), "")

    def test_single_digit(self):
        """Single digit string returns that digit."""
        self.assertEqual(_extract_trailing_digits("5"), "5")

    def test_multi_digit_trailing(self):
        """Multiple trailing digits captured: 'port42' → '42'."""
        self.assertEqual(_extract_trailing_digits("port42"), "42")

    def test_no_backtracking_on_long_non_digit_suffix(self):
        """Long string ending with a non-digit runs in O(n) — should return empty quickly."""
        long_str = "1" * 1000 + "x"
        self.assertEqual(_extract_trailing_digits(long_str), "")


class ForceReapplyTest(EngineAdvancedFixtures):
    """Test force_reapply=True paths in apply_interface_name_rules."""

    def test_force_reapply_non_channel_renames_already_renamed(self):
        """force_reapply=True renames interfaces even if name doesn't match raw template."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # Interface currently has an old name — would normally be skipped (not in raw_names)
        iface = Interface.objects.create(device=self.device, module=module, name="old-name", type="10gbase-x-sfpp")

        # Without force_reapply: skipped because "old-name" not in raw_names
        result = apply_interface_name_rules(module, self.bay0, force_reapply=False)
        self.assertEqual(result, 0)

        # With force_reapply: should apply and rename
        result = apply_interface_name_rules(module, self.bay0, force_reapply=True)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

    def test_force_reapply_channel_no_renames_when_already_correct(self):
        """force_reapply with channel rule produces no renames when all interfaces already have correct names."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # Already-renamed channel interfaces — last segment of base "xe-0/0/0" (i.e. "0") matches raw_name "0"
        iface0 = Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:0", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:1", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:2", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="xe-0/0/0:3", type="10gbase-x-sfpp")

        # force_reapply=True: base "xe-0/0/0" is matched (via last segment "0" in raw_names),
        # but template evaluates to the same names, so 0 renames occur.
        result = apply_interface_name_rules(module, self.bay0, force_reapply=True)
        # Names are already correct, so no renames needed
        self.assertEqual(result, 0)
        self.assertIsNotNone(iface0)  # Interface still exists
        iface0.refresh_from_db()
        self.assertEqual(iface0.name, "xe-0/0/0:0")  # Name unchanged

    def test_force_reapply_channel_renames_on_vc_position_change(self):
        """force_reapply with a channel+vc_position rule renames already-named channel interfaces."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-{vc_position}/0/{bay_position}:{channel}",
            channel_count=1,
            channel_start=0,
        )
        module = Module.objects.create(device=self.vc_device, module_bay=self.vc_bay, module_type=self.module_type)
        # Interface was previously named with old vc_position=2; last segment "0" matches raw_name "0"
        iface = Interface.objects.create(device=self.vc_device, module=module, name="xe-2/0/0:0", type="10gbase-x-sfpp")

        # force_reapply=True: base "xe-2/0/0" is matched (last segment "0" in raw_names),
        # and vc_device.vc_position=1 → new name "xe-1/0/0:0" differs → rename occurs.
        result = apply_interface_name_rules(module, self.vc_bay, force_reapply=True)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "xe-1/0/0:0")

    def test_no_rule_for_module_returns_zero(self):
        """apply_interface_name_rules returns 0 immediately when no rule matches."""
        # No InterfaceNameRule created → find_matching_rule returns None
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")
        result = apply_interface_name_rules(module, self.bay0)
        self.assertEqual(result, 0)


class FlagPotentiallyDeprecatedTest(EngineAdvancedFixtures):
    """Test that _flag_rule_potentially_deprecated is called on no-op renames."""

    def test_no_op_rename_adds_deprecated_tag(self):
        """When rule matches but all interfaces already have correct names, tag is added."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # Create with the final correct name (so the rule renames nothing for it)
        # But the name "et-0/0/0" is NOT in raw_names (raw = "0"), so this won't trigger deprecated.
        # Instead, set force_reapply so it's in unrenamed but produces 0 renames.
        iface = Interface.objects.create(device=self.device, module=module, name="et-0/0/0", type="10gbase-x-sfpp")

        # force_reapply=True: unrenamed=[iface], but new_name=="et-0/0/0"==iface.name → renamed=0
        apply_interface_name_rules(module, self.bay0, force_reapply=True)

        # The tag should have been added
        iface.refresh_from_db()
        tags = list(rule.tags.filter(slug="potentially-deprecated"))
        self.assertEqual(len(tags), 1)


# ---------------------------------------------------------------------------
# engine.py — evaluate_name_template unsafe AST node (line 829)
# ---------------------------------------------------------------------------


class EngineEvaluateTemplateUnsafeASTTest(TestCase):
    """Test evaluate_name_template raises for unsafe AST node types (defense-in-depth)."""

    def test_unsafe_ast_node_raises_valueerror(self):
        """An exponent expression passes the character guard but fails the AST evaluator.

        ``{2**3}`` is built only from characters the guard regex permits
        (digits and ``*``), so it slips past the cheap character check. The
        parser yields a ``Pow`` node, which the recursive arithmetic
        evaluator rejects with ValueError.
        """
        from netbox_interface_name_rules.engine import evaluate_name_template

        with self.assertRaises(ValueError):
            evaluate_name_template("{2**3}", {})


# ---------------------------------------------------------------------------
# rule_selection.py, invalid regex path
# ---------------------------------------------------------------------------


class EngineFindRegexMatchErrorTest(TestCase):
    """Test that rule selection silently skips stored patterns RE2 cannot compile."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RegXMfg", slug="regxmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="RegX-Dev", slug="regx-dev")
        cls.module_type_good = ModuleType.objects.create(
            manufacturer=manufacturer, model="RegX-GOOD", part_number="RegX-GOOD"
        )
        # Create a rule with an invalid regex pattern (bypassing model validation)
        cls.bad_regex_rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="[unclosed(",  # syntactically invalid
            name_template="port{bay_position}",
            enabled=True,
        )
        # Save without calling clean() to bypass validation
        cls.bad_regex_rule.save()

    def test_invalid_regex_pattern_is_skipped_not_raised(self):
        """_find_regex_match silently skips rules whose module_type_pattern is not valid RE2.

        The bad-pattern rule is inserted directly via save() to bypass clean()
        validation. Rule loading compiles through the RE2 seam, so an invalid
        pattern moves on to the next candidate instead of propagating.
        """
        from netbox_interface_name_rules.rule_selection import _find_regex_match

        candidates = [(None, None, None)]
        result = _find_regex_match("RegX-GOOD", candidates)
        # The bad regex rule is skipped; result is None (no valid rule found)
        self.assertIsNone(result)

    @classmethod
    def tearDownClass(cls):
        if cls.bad_regex_rule.pk:
            InterfaceNameRule.objects.filter(pk=cls.bad_regex_rule.pk).delete()
        super().tearDownClass()


# ---------------------------------------------------------------------------
# engine.py — has_applicable_interfaces exception path (lines 571-572)
# ---------------------------------------------------------------------------


class EngineHasApplicableExceptionTest(TestCase):
    """Test has_applicable_interfaces() catches exceptions and returns False."""

    def test_invalid_regex_rule_returns_false(self):
        """has_applicable_interfaces() returns False when the scan raises ValueError for real.

        A rule with an invalid ``module_type_pattern`` (inserted via save() to
        bypass clean() validation) makes ``_matching_moduletype_pks`` raise a
        real ValueError inside ``find_interfaces_for_rule``; has_applicable_interfaces
        catches ValueError and returns False. No mock is required.
        """
        from netbox_interface_name_rules.engine import has_applicable_interfaces

        bad_rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="[invalid(",  # syntactically invalid regex
            name_template="et-0/0/{bay_position}",
            enabled=True,
        )
        bad_rule.save()  # bypass clean() so the invalid pattern is persisted
        try:
            self.assertFalse(has_applicable_interfaces(bad_rule))
        finally:
            InterfaceNameRule.objects.filter(pk=bad_rule.pk).delete()


# ---------------------------------------------------------------------------
# engine.py: a breakout rule whose family names cannot be evaluated
# ---------------------------------------------------------------------------


class BreakoutTemplateValueErrorTest(TestCase):
    """A breakout template that cannot be evaluated leaves every candidate base where it is."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ChanXMfg", slug="chanxmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ChanX-Dev", slug="chanx-dev")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="ChanX-SFP", part_number="ChanX-SFP"
        )
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="CXBay 0", position="0")
        role = DeviceRole.objects.create(name="ChanXRole", slug="chanxrole")
        site = Site.objects.create(name="ChanXSite", slug="chanxsite")
        cls.device = Device.objects.create(name="chanx-dev-01", device_type=cls.device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="CXBay 0")

    def test_an_unevaluable_template_builds_nothing_and_renames_nothing(self):
        """``{undefined_var}`` is not a naming variable, so every family reports the failure."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{undefined_var}:{channel}",  # The undefined variable raises ValueError.
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="Eth0", type="100gbase-x-qsfp28")
        Interface.objects.create(device=self.device, module=module, name="Eth1", type="100gbase-x-qsfp28")

        outcome = apply_rule_to_existing(rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(
            {member.status for member in outcome.skipped_members},
            {FamilyStatus.FAILED},
        )
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["Eth0", "Eth1"],
        )


# ---------------------------------------------------------------------------
# engine.py — _build_module_qs platform filter (line 588)
# ---------------------------------------------------------------------------


class EngineBuildModuleQsPlatformTest(TestCase):
    """Test _build_module_qs applies platform filter correctly (line 588)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="PlatXMfg", slug="platxmfg")
        cls.platform = Platform.objects.create(name="PLATX-IOS", slug="platx-ios")
        other_platform = Platform.objects.create(name="PLATX-NXOS", slug="platx-nxos")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="PLATX-SFP", part_number="PLATX-SFP"
        )
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            platform=cls.platform,
            name_template="et-0/0/{bay_position}",
        )
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="PLATX-Dev", slug="platx-dev")
        ModuleBayTemplate.objects.create(device_type=device_type, name="PLBay 0", position="0")
        role = DeviceRole.objects.create(name="PlatXRole", slug="platxrole")
        site = Site.objects.create(name="PlatXSite", slug="platxsite")
        device_match = Device.objects.create(
            name="platx-dev-match", device_type=device_type, role=role, site=site, platform=cls.platform
        )
        device_other = Device.objects.create(
            name="platx-dev-other", device_type=device_type, role=role, site=site, platform=other_platform
        )
        bay_match = ModuleBay.objects.get(device=device_match)
        bay_other = ModuleBay.objects.get(device=device_other)
        cls.module_match = Module.objects.create(device=device_match, module_bay=bay_match, module_type=cls.module_type)
        cls.module_other = Module.objects.create(device=device_other, module_bay=bay_other, module_type=cls.module_type)

    def test_platform_filter_applied(self):
        """_build_module_qs applies rule.platform filter — matching device is included, other is excluded."""
        from netbox_interface_name_rules.engine import _build_module_qs

        qs = _build_module_qs(self.rule)
        pks = list(qs.values_list("pk", flat=True))
        self.assertIn(self.module_match.pk, pks)
        self.assertNotIn(self.module_other.pk, pks)


# ---------------------------------------------------------------------------
# engine.py — apply_rule_to_existing no-ifaces and id_set paths (lines 750, 753)
# ---------------------------------------------------------------------------


class EngineApplyRuleToExistingEdgeCasesTest(TestCase):
    """Test apply_rule_to_existing edge cases: no interfaces, id_set filter."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ARXMfg", slug="arxmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ARX-Dev", slug="arx-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="ARX-SFP", part_number="ARX-SFP")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="ARXBay 0", position="0")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="ARXBay 1", position="1")
        role = DeviceRole.objects.create(name="ARXRole", slug="arxrole")
        site = Site.objects.create(name="ARXSite", slug="arxsite")
        cls.device = Device.objects.create(name="arx-dev-01", device_type=cls.device_type, role=role, site=site)
        cls.bay0 = ModuleBay.objects.get(device=cls.device, name="ARXBay 0")
        cls.bay1 = ModuleBay.objects.get(device=cls.device, name="ARXBay 1")

    def test_channel_rule_no_interfaces_skips(self):
        """Channel rule skips module with no interfaces (line 750: continue)."""
        from netbox_interface_name_rules.engine import apply_rule_to_existing

        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        # Module with NO interfaces
        Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        count = apply_rule_to_existing(rule)
        self.assertEqual(count.changed_count, 0)

    def test_channel_rule_id_set_filters_base(self):
        """Channel rule with id_set skips when base_iface.pk not in id_set (line 753)."""
        from netbox_interface_name_rules.engine import apply_rule_to_existing

        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="Eth99", type="100gbase-x-qsfp28")
        # Pass an id_set that does NOT include iface.pk
        count = apply_rule_to_existing(rule, interface_ids=[iface.pk + 9999])
        self.assertEqual(count.changed_count, 0)
        # Interface should be unchanged
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Eth99")


# ---------------------------------------------------------------------------
# engine.py — _rename_device_interface template/full_clean exception paths (lines 131-156)
# ---------------------------------------------------------------------------


class EngineRenameDeviceInterfaceExceptionTest(TestCase):
    """Test _rename_device_interface exception paths for template and validation errors."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RDIXMfg", slug="rdixmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="RDIX-Dev", slug="rdix-dev")
        cls.platform = Platform.objects.create(name="RDIX-IOS", slug="rdix-ios")
        role = DeviceRole.objects.create(name="RDIXRole", slug="rdixrole")
        site = Site.objects.create(name="RDIXSite", slug="rdixsite")
        vc = VirtualChassis.objects.create(name="rdix-vc")
        cls.device = Device.objects.create(
            name="rdix-sw1",
            device_type=cls.device_type,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=1,
            platform=cls.platform,
        )

    def test_template_exception_skips_interface(self):
        """_rename_device_interface skips when template evaluation raises (lines 131-138)."""
        from netbox_interface_name_rules.engine import apply_device_interface_rules

        # Create a device-interface rule with an unsafe template that triggers ValueError
        rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="{1/0}",  # division not allowed — ValueError from evaluate_name_template
        )
        Interface.objects.create(device=self.device, name="Gi0/1", type="1000base-t")
        apply_device_interface_rules(self.device)
        # Interface should NOT be renamed (exception was caught)
        iface = Interface.objects.get(device=self.device, name="Gi0/1")
        self.assertIsNotNone(iface)
        rule.delete()

    def test_full_clean_exception_skips_interface(self):
        """A device-interface rename onto a name already on the device is skipped with a tidy WARNING."""
        from netbox_interface_name_rules.engine import apply_device_interface_rules

        rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern=r"Gi\d+/\d+",
            name_template="GigabitEthernet{vc_position}/{port}",
        )
        # The name the rule would produce for "Gi0/2" (vc_position=1, port=2) is already taken
        # on the device, so the device-scope pre-check skips the rename with a clean WARNING
        # (no full_clean ValidationError / ERROR traceback).
        Interface.objects.create(device=self.device, name="GigabitEthernet1/2", type="1000base-t")
        iface = Interface.objects.create(device=self.device, name="Gi0/2", type="1000base-t")

        with self.assertLogs("netbox_interface_name_rules", level="WARNING") as logs:
            apply_device_interface_rules(self.device)

        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi0/2")  # skipped: collision pre-checked, not renamed
        self.assertEqual(Interface.objects.filter(device=self.device, name="GigabitEthernet1/2").count(), 1)
        self.assertTrue(any("already exists" in m for m in logs.output))
        rule.delete()


# ---------------------------------------------------------------------------
# engine.py: preview of a breakout rule whose template cannot be evaluated
# ---------------------------------------------------------------------------


class PreviewTemplateErrorTest(TestCase):
    """A breakout rule the preview cannot evaluate reports the error instead of a name."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ChRuleXMfg", slug="chrulexmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ChRuleX-Dev", slug="chrulex-dev")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="ChRuleX-SFP", part_number="ChRuleX-SFP"
        )
        ModuleBayTemplate.objects.create(device_type=device_type, name="CRBay 0", position="0")
        role = DeviceRole.objects.create(name="ChRuleXRole", slug="chrulexrole")
        site = Site.objects.create(name="ChRuleXSite", slug="chrulexsite")
        cls.device = Device.objects.create(name="chrulex-dev-01", device_type=device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="CRBay 0")

    def test_an_unevaluable_template_previews_one_error_placeholder(self):
        """An undefined variable raises for real, so the family previews as a single placeholder."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}:{undefined_var}",  # The undefined variable raises ValueError.
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="Eth0", type="100gbase-x-qsfp28")

        results, total = find_interfaces_for_rule(rule)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["new_names"]), 1)
        self.assertTrue(results[0]["new_names"][0].startswith("<error:"))
        self.assertEqual(total, 1)


# ---------------------------------------------------------------------------
# engine.py: preview of a module that carries no interfaces
# ---------------------------------------------------------------------------


class PreviewEmptyModuleTest(TestCase):
    """A module with no interfaces contributes nothing to a breakout preview."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="PcmEMfg", slug="pcmemfg")
        device_type = DeviceType.objects.create(manufacturer=mfg, model="PcmE-Dev", slug="pcme-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=mfg, model="PcmE-SFP", part_number="PcmE-SFP")
        ModuleBayTemplate.objects.create(device_type=device_type, name="PEBay 0", position="0")
        role = DeviceRole.objects.create(name="PcmERole", slug="pcmerole")
        site = Site.objects.create(name="PcmESite", slug="pcmesite")
        cls.device = Device.objects.create(name="pcme-dev-01", device_type=device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="PEBay 0")

    def test_a_module_without_interfaces_previews_nothing(self):
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)

        self.assertEqual(find_interfaces_for_rule(rule), ([], 0))


# ---------------------------------------------------------------------------
# engine.py: preview stops at the batch limit
# ---------------------------------------------------------------------------


class PreviewLimitTest(TestCase):
    """The preview stops once it has collected as many changed families as the limit allows."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ChLimMfg", slug="chlimmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ChLim-Dev", slug="chlim-dev")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="ChLim-SFP", part_number="ChLim-SFP"
        )
        for position in ("0", "1"):
            ModuleBayTemplate.objects.create(device_type=device_type, name=f"CLBay {position}", position=position)
        role = DeviceRole.objects.create(name="ChLimRole", slug="chlimrole")
        site = Site.objects.create(name="ChLimSite", slug="chlimsite")
        cls.device = Device.objects.create(name="chlim-dev-01", device_type=device_type, role=role, site=site)

    def test_the_scan_stops_at_the_limit_and_still_counts_what_is_left(self):
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        for position in ("0", "1"):
            bay = ModuleBay.objects.get(device=self.device, name=f"CLBay {position}")
            module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
            Interface.objects.create(device=self.device, module=module, name=f"Eth{position}", type="100gbase-x-qsfp28")

        results, total = find_interfaces_for_rule(rule, limit=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(total, 2)


# ---------------------------------------------------------------------------
# engine.py — two-level nested bay grandparent slot (lines 418–419)
# ---------------------------------------------------------------------------


class TwoLevelNestedBayTest(TestCase):
    """Test _resolve_slot grandparent-slot detection for a 3-deep bay hierarchy.

    Real hardware: chassis → line card bay → SFP bay.
    When build_variables is called for the innermost bay, slot must resolve
    to the chassis bay position (the grandparent), not the immediate parent.
    """

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="NestMfg", slug="nestmfg")
        # Device type with two top-level bays
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="Nest-Chassis", slug="nest-chassis")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Chassis Bay", position="2")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Direct Bay", position="5")

        # Chassis module type — has one sub-bay for line cards
        cls.chassis_type = ModuleType.objects.create(manufacturer=mfg, model="Nest-LC-Chassis", part_number="NLC")
        ModuleBayTemplate.objects.create(module_type=cls.chassis_type, name="LC Bay", position="1")

        # Line-card module type — has one sub-bay for SFPs
        cls.line_card_type = ModuleType.objects.create(manufacturer=mfg, model="Nest-LineCard", part_number="NLC-LC")
        ModuleBayTemplate.objects.create(module_type=cls.line_card_type, name="SFP Bay", position="0")

        # SFP module type (leaf, no sub-bays needed for the test)
        cls.sfp_type = ModuleType.objects.create(manufacturer=mfg, model="Nest-SFP", part_number="NSFP")

        role = DeviceRole.objects.create(name="NestRole", slug="nestrole")
        site = Site.objects.create(name="NestSite", slug="nestsite")
        cls.device = Device.objects.create(name="nest-sw-01", device_type=cls.device_type, role=role, site=site)
        cls.outer_bay = ModuleBay.objects.get(device=cls.device, name="Chassis Bay")
        cls.direct_bay = ModuleBay.objects.get(device=cls.device, name="Direct Bay")

        # Install chassis → creates mid_bay (parent=outer_bay, auto via ModuleBay.save)
        cls.chassis = Module.objects.create(device=cls.device, module_bay=cls.outer_bay, module_type=cls.chassis_type)
        cls.mid_bay = ModuleBay.objects.get(device=cls.device, module=cls.chassis, name="LC Bay")

        # Install line card in mid_bay → creates inner_bay (parent=mid_bay, auto via ModuleBay.save)
        cls.line_card = Module.objects.create(device=cls.device, module_bay=cls.mid_bay, module_type=cls.line_card_type)
        cls.inner_bay = ModuleBay.objects.get(device=cls.device, module=cls.line_card, name="SFP Bay")

    def test_slot_resolves_to_grandparent_position(self):
        """build_variables on inner_bay resolves slot to outer_bay.position (lines 418-419).

        inner_bay.parent = mid_bay, mid_bay.parent = outer_bay,
        outer_bay.installed_module = chassis → slot = outer_bay.position = '2'.
        """
        variables = build_variables(self.inner_bay)
        self.assertEqual(variables["slot"], "2")

    def test_parent_bay_position_is_mid_bay_position(self):
        """parent_bay_position for inner_bay is mid_bay.position ('1')."""
        variables = build_variables(self.inner_bay)
        self.assertEqual(variables["parent_bay_position"], "1")


# ---------------------------------------------------------------------------
# engine.py — apply_rule_to_existing exception in plain-interface loop (lines 773-780)
# ---------------------------------------------------------------------------


class ApplyRuleExceptionInLoopTest(EngineAdvancedFixtures):
    """An unexpected executor failure reaches the caller, and committed families stay committed."""

    @staticmethod
    def _failing_after_the_first_family():
        """Return an executor that runs the first family for real and fails on the next one."""
        from netbox_interface_name_rules.family import batch

        execute = batch.execute_family_plan
        calls = [0]

        def _side_effect(plan):
            calls[0] += 1
            if calls[0] >= 2:
                raise ValueError("forced failure on the second family")
            return execute(plan)

        return _side_effect

    def test_a_failing_plain_rename_leaves_the_earlier_module_renamed(self):
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module0 = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        module1 = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        iface0 = Interface.objects.create(device=self.device, module=module0, name="0", type="10gbase-x-sfpp")
        iface1 = Interface.objects.create(device=self.device, module=module1, name="1", type="10gbase-x-sfpp")

        with (
            patch(
                "netbox_interface_name_rules.family.batch.execute_family_plan",
                side_effect=self._failing_after_the_first_family(),
            ),
            self.assertRaises(ValueError),
        ):
            apply_rule_to_existing(rule)

        iface0.refresh_from_db()
        iface1.refresh_from_db()
        self.assertEqual(iface0.name, "et-0/0/0")  # first family had already committed
        self.assertEqual(iface1.name, "1")  # second never ran

    def test_a_failing_breakout_family_leaves_the_earlier_module_built(self):
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module0 = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        module1 = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        iface0 = Interface.objects.create(device=self.device, module=module0, name="0", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module1, name="1", type="10gbase-x-sfpp")

        with (
            patch(
                "netbox_interface_name_rules.family.batch.execute_family_plan",
                side_effect=self._failing_after_the_first_family(),
            ),
            self.assertRaises(ValueError),
        ):
            apply_rule_to_existing(rule)

        iface0.refresh_from_db()
        self.assertEqual(iface0.name, "xe-0/0/0:0")  # first family had already committed
        self.assertEqual(Interface.objects.get(module=module1).name, "1")


# ---------------------------------------------------------------------------
# engine.py — _build_module_qs parent_module_type filter (line 588)
# ---------------------------------------------------------------------------


class BuildModuleQsParentTypeTest(TestCase):
    """Test _build_module_qs applies rule.parent_module_type filter (line 588).

    A rule scoped to chassis_type as parent should include only SFPs installed
    in bays whose parent bay hosts a chassis, not SFPs installed directly.
    Uses an independent fixture (not the 3-level nested hierarchy) to keep
    mid_bay available for SFP installation.
    """

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="BmqMfg", slug="bmqmfg")
        device_type = DeviceType.objects.create(manufacturer=mfg, model="BMQ-Switch", slug="bmq-switch")
        # Two top-level bays: one for the chassis, one for direct SFP
        ModuleBayTemplate.objects.create(device_type=device_type, name="Outer Bay", position="2")
        ModuleBayTemplate.objects.create(device_type=device_type, name="Direct Bay", position="5")

        # Chassis module type — has one sub-bay (mid_bay)
        cls.chassis_type = ModuleType.objects.create(manufacturer=mfg, model="BMQ-Chassis", part_number="BMQ-C")
        ModuleBayTemplate.objects.create(module_type=cls.chassis_type, name="Mid Bay", position="1")

        cls.sfp_type = ModuleType.objects.create(manufacturer=mfg, model="BMQ-SFP", part_number="BMQ-SFP")

        role = DeviceRole.objects.create(name="BmqRole", slug="bmqrole")
        site = Site.objects.create(name="BmqSite", slug="bmqsite")
        cls.device = Device.objects.create(name="bmq-sw-01", device_type=device_type, role=role, site=site)
        cls.outer_bay = ModuleBay.objects.get(device=cls.device, name="Outer Bay")
        cls.direct_bay = ModuleBay.objects.get(device=cls.device, name="Direct Bay")

        # Install chassis → creates mid_bay (parent=outer_bay)
        cls.chassis = Module.objects.create(device=cls.device, module_bay=cls.outer_bay, module_type=cls.chassis_type)
        cls.mid_bay = ModuleBay.objects.get(device=cls.device, module=cls.chassis, name="Mid Bay")

        # SFP installed in mid_bay (parent chain: mid_bay → outer_bay with chassis)
        cls.sfp_in_mid = Module.objects.create(device=cls.device, module_bay=cls.mid_bay, module_type=cls.sfp_type)
        # SFP installed directly in a top-level device bay (no chassis in parent chain)
        cls.sfp_direct = Module.objects.create(device=cls.device, module_bay=cls.direct_bay, module_type=cls.sfp_type)

    def test_parent_module_type_filters_to_matching_module(self):
        """Only modules whose bay's parent has the matching module type are returned (line 588)."""
        from netbox_interface_name_rules.engine import _build_module_qs

        rule = InterfaceNameRule.objects.create(
            module_type=self.sfp_type,
            parent_module_type=self.chassis_type,
            name_template="et-0/0/{bay_position}",
        )
        qs = _build_module_qs(rule)
        pks = list(qs.values_list("pk", flat=True))
        self.assertIn(self.sfp_in_mid.pk, pks)
        self.assertNotIn(self.sfp_direct.pk, pks)

    def test_no_parent_module_type_returns_all(self):
        """Rule without parent_module_type returns all matching modules regardless of nesting."""
        from netbox_interface_name_rules.engine import _build_module_qs

        rule = InterfaceNameRule.objects.create(
            module_type=self.sfp_type,
            name_template="et-0/0/{bay_position}",
        )
        qs = _build_module_qs(rule)
        pks = list(qs.values_list("pk", flat=True))
        self.assertIn(self.sfp_in_mid.pk, pks)
        self.assertIn(self.sfp_direct.pk, pks)


# ---------------------------------------------------------------------------
# engine.py — _flag_rule_potentially_deprecated exception handler (lines 282-283)
# ---------------------------------------------------------------------------


class FlagDeprecatedExceptionTest(TestCase):
    """Test that _flag_rule_potentially_deprecated swallows exceptions (lines 282-283)."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="FlagXMfg", slug="flagxmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=mfg, model="FlagX-SFP", part_number="FlagX-SFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="et-0/0/{bay_position}",
        )

    def test_tags_add_exception_is_swallowed(self):
        """_flag_rule_potentially_deprecated does not propagate exception from tags.add (lines 282-283)."""
        from netbox_interface_name_rules.engine import _flag_rule_potentially_deprecated

        with patch.object(self.rule.tags, "add", side_effect=Exception("tag DB error")):
            _flag_rule_potentially_deprecated(self.rule)  # Must not raise

    def test_tag_getorcreate_exception_is_swallowed(self):
        """_flag_rule_potentially_deprecated swallows Tag.objects.get_or_create failures."""
        from extras.models import Tag

        from netbox_interface_name_rules.engine import _flag_rule_potentially_deprecated

        with patch.object(Tag.objects, "get_or_create", side_effect=Exception("tag table error")):
            _flag_rule_potentially_deprecated(self.rule)  # Must not raise


# ---------------------------------------------------------------------------
# Device interface save exception rollback
# ---------------------------------------------------------------------------


class DeviceInterfaceSaveExceptionTest(TestCase):
    """Test the device rule path rolls back the name when an interface save fails."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="SaveXMfg", slug="savexmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="SaveX-Dev", slug="savex-dev")
        role = DeviceRole.objects.create(name="SaveXRole", slug="savexrole")
        site = Site.objects.create(name="SaveXSite", slug="savexsite")
        vc = VirtualChassis.objects.create(name="savex-vc")
        cls.device = Device.objects.create(
            name="savex-sw1",
            device_type=cls.device_type,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=1,
        )

    def test_save_exception_restores_name_and_returns_zero(self):
        """Restore the old name and return zero when an interface save fails."""
        from netbox_interface_name_rules.engine import apply_device_interface_rules

        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="xe-{vc_position}/{port}",
        )
        iface = Interface.objects.create(device=self.device, name="Gi0/1", type="1000base-t")

        from django.db import IntegrityError

        with patch.object(Interface, "save", side_effect=IntegrityError("disk full")):
            result = apply_device_interface_rules(self.device)

        iface.refresh_from_db()
        self.assertEqual(result, 0)
        self.assertEqual(iface.name, "Gi0/1")  # rolled back


# ---------------------------------------------------------------------------
# engine.py — idempotency of force_reapply=True on breakout channel names (T7)
# ---------------------------------------------------------------------------


class ForceReapplyBreakoutIdempotencyTest(EngineAdvancedFixtures):
    """Test that a second apply_interface_name_rules with force_reapply=True is idempotent.

    The first call creates the breakout channel interfaces from a raw "0" interface.
    The second call must not create duplicates or rename them again.
    """

    def test_second_force_reapply_does_not_create_duplicates(self):
        """Calling apply_interface_name_rules twice with force_reapply=True produces no duplicates."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="Hu0/0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="100gbase-x-qsfp28")

        # First apply: renames base to :0 and creates :1 :2 :3
        count1 = apply_interface_name_rules(module, self.bay0, force_reapply=False)
        self.assertEqual(count1, 4)

        iface_names_after_first = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        expected = ["Hu0/0/0/0:0", "Hu0/0/0/0:1", "Hu0/0/0/0:2", "Hu0/0/0/0:3"]
        self.assertEqual(iface_names_after_first, expected)

        # Second apply with force_reapply=True: must not create duplicates or rename
        count2 = apply_interface_name_rules(module, self.bay0, force_reapply=True)
        self.assertEqual(count2, 0)

        iface_names_after_second = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(iface_names_after_second, expected)
        self.assertEqual(Interface.objects.filter(module=module).count(), 4)


# ---------------------------------------------------------------------------
# engine.py — regex rule specificity tie-breaking by pk (line 349)
# ---------------------------------------------------------------------------


class RegexTiebreakerTest(TestCase):
    """Test that _find_regex_match returns the lower-pk rule when pattern lengths are equal (line 349).

    The unique constraint on (module_type_pattern, parent_mt, device_type, platform) means two rules
    cannot share an identical pattern with the same scope.  Tie-breaking is therefore exercised with
    two *different* patterns of equal character length that both match the target module type.
    """

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="TieMfg", slug="tiemfg")
        # "TSAME-100" is matched by both patterns below
        cls.module_type = ModuleType.objects.create(manufacturer=mfg, model="TSAME-100", part_number="TSAME-100")
        # Two rules with equal-length (9-char) patterns that both fullmatch "TSAME-100"
        cls.rule_first = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="TSAME-1..",  # 9 chars
            name_template="first-{bay_position}",
        )
        cls.rule_second = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="TSAME-...",  # 9 chars (different pattern, same length)
            name_template="second-{bay_position}",
        )

    def test_lower_pk_wins_on_same_pattern_length(self):
        """When two regex rules have equal-length patterns, the one with the lower pk is returned."""
        from netbox_interface_name_rules.engine import find_matching_rule

        self.assertLess(self.rule_first.pk, self.rule_second.pk)
        matched = find_matching_rule(self.module_type, None, None)
        self.assertEqual(matched, self.rule_first)

    def test_longer_pattern_wins_over_shorter(self):
        """A more specific (longer) pattern beats a shorter one regardless of pk order."""
        from netbox_interface_name_rules.engine import find_matching_rule

        mfg = Manufacturer.objects.get(name="TieMfg")
        # Use a different prefix so these rules don't interact with the TSAME rules above
        module_type_specific = ModuleType.objects.create(
            manufacturer=mfg, model="TLONG-100G-LR4", part_number="TLONG-LR4"
        )
        rule_short = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="TLONG-.*",  # 8 chars
            name_template="short-{bay_position}",
        )
        rule_long = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="TLONG-100G-LR4",  # 14 chars (more specific)
            name_template="long-{bay_position}",
        )
        # rule_short was created first → lower pk; rule_long has longer pattern → must win
        self.assertLess(rule_short.pk, rule_long.pk)
        matched = find_matching_rule(module_type_specific, None, None)
        self.assertEqual(matched, rule_long)


# ---------------------------------------------------------------------------
# engine.py — find_interfaces_for_rule uses set for processed_pks
# ---------------------------------------------------------------------------


class FindInterfacesProcessedPksTest(EngineAdvancedFixtures):
    """Test find_interfaces_for_rule correctness with multiple modules."""

    def test_multiple_modules_all_counted(self):
        """find_interfaces_for_rule processes all matching modules without duplication."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module0 = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        module1 = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module0, name="0", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module1, name="1", type="10gbase-x-sfpp")

        results, total = find_interfaces_for_rule(rule)
        self.assertEqual(total, 2)
        self.assertEqual(len(results), 2)
        result_module_ids = {r["module"].pk for r in results}
        self.assertEqual(result_module_ids, {module0.pk, module1.pk})


# ---------------------------------------------------------------------------
# engine.py — breakout transaction rollback on mid-channel failure
# ---------------------------------------------------------------------------


class BreakoutTransactionRollbackTest(EngineAdvancedFixtures):
    """The install path writes a breakout family whole or not at all."""

    def test_partial_breakout_rolls_back(self):
        """If channel 2 fails validation, channels 0–1 are rolled back too."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="Hu0/0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="100gbase-x-qsfp28")

        original_full_clean = Interface.full_clean
        call_count = [0]

        def failing_full_clean(self_iface, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:  # Fail on channel 2
                from django.core.exceptions import ValidationError

                raise ValidationError("simulated failure")
            return original_full_clean(self_iface, *args, **kwargs)

        with patch.object(Interface, "full_clean", failing_full_clean):
            renamed = apply_interface_name_rules(module, module.module_bay)

        # The transaction rolled back. Only the original interface remains with its original name.
        self.assertEqual(renamed, 0)
        iface_names = list(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(iface_names, ["0"])


# ---------------------------------------------------------------------------
# engine.py — _get_raw_interface_names with no templates
# ---------------------------------------------------------------------------


class GetRawInterfaceNamesNoTemplatesTest(EngineAdvancedFixtures):
    """Test _get_raw_interface_names when module_type has no InterfaceTemplate entries."""

    def test_no_templates_returns_empty_set(self):
        """_get_raw_interface_names returns empty set when module_type has no templates."""
        from netbox_interface_name_rules.engine import _get_raw_interface_names

        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        result = _get_raw_interface_names(module)
        self.assertEqual(result, set())


class PredictRuleOutputTest(EngineAdvancedFixtures):
    """Tests for predict_rule_output — pure name prediction without DB mutations."""

    def test_no_rule_returns_input_unchanged(self):
        """When no rule matches, the raw names pass through verbatim."""
        from netbox_interface_name_rules.engine import predict_rule_output

        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        result = predict_rule_output(module, self.bay0, ["raw-a", "raw-b"])
        self.assertEqual(result, ["raw-a", "raw-b"])

    def test_simple_rule_rewrites_each_name(self):
        """Channel-less rule: 1 input → 1 output, evaluated through name_template."""
        from netbox_interface_name_rules.engine import predict_rule_output

        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}/1",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        result = predict_rule_output(module, self.bay0, ["2/x1/1/c9"])
        self.assertEqual(result, ["2/x1/1/c9/1"])

    def test_breakout_rule_expands_each_name_to_channel_count(self):
        """channel_count=4 rule: 1 input → 4 outputs (one per channel)."""
        from netbox_interface_name_rules.engine import predict_rule_output

        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        result = predict_rule_output(module, self.bay0, ["xe-0/0/0"])
        self.assertEqual(result, ["xe-0/0/0:0", "xe-0/0/0:1", "xe-0/0/0:2", "xe-0/0/0:3"])

    def test_multiple_raw_names_all_transformed(self):
        """Each raw name is independently transformed; output preserves order."""
        from netbox_interface_name_rules.engine import predict_rule_output

        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}/1",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        result = predict_rule_output(module, self.bay0, ["a", "b", "c"])
        self.assertEqual(result, ["a/1", "b/1", "c/1"])

    def test_pure_does_not_touch_interfaces(self):
        """predict_rule_output must not create, rename, or delete Interface rows."""
        from netbox_interface_name_rules.engine import predict_rule_output

        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}/1",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="leave-me", type="10gbase-x-sfpp")

        predict_rule_output(module, self.bay0, ["leave-me"])

        iface.refresh_from_db()
        self.assertEqual(iface.name, "leave-me")
        self.assertEqual(Interface.objects.filter(module=module).count(), 1)

    def test_template_eval_failure_falls_back_to_raw(self):
        """When evaluate_name_template raises, the raw name is kept in the output."""
        from netbox_interface_name_rules.engine import predict_rule_output

        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{nonexistent_variable}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        result = predict_rule_output(module, self.bay0, ["fallback-me"])
        self.assertEqual(result, ["fallback-me"])


class PredictRuleOutputPlainTemplateTest(EngineAdvancedFixtures):
    """A module type whose templates are not channelized keeps the flat per-name prediction."""

    def test_breakout_expands_a_plain_template_name(self):
        """No template declares a channel count, so every raw name expands to channel_count names."""
        from netbox_interface_name_rules.engine import predict_rule_output

        InterfaceTemplate.objects.create(module_type=self.module_type, name="{module}", type="10gbase-x-sfpp")
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)

        result = predict_rule_output(module, self.bay0, ["0"])

        self.assertEqual(result, ["0:0", "0:1", "0:2", "0:3"])

    def test_simple_rename_of_a_plain_template_name(self):
        """The same holds for a channel-less rule: one name in, one name out."""
        from netbox_interface_name_rules.engine import predict_rule_output

        InterfaceTemplate.objects.create(module_type=self.module_type, name="{module}", type="10gbase-x-sfpp")
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{base}")
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)

        result = predict_rule_output(module, self.bay0, ["0"])

        self.assertEqual(result, ["et-0/0/0"])


# ---------------------------------------------------------------------------
# engine.py — name-collision handling (skip + log, never raise / partial-abort)
# ---------------------------------------------------------------------------


class NameCollisionTest(EngineAdvancedFixtures):
    """A computed name already taken on the device is skipped, never raised."""

    def test_install_path_skips_collision_without_raising(self):
        """apply_interface_name_rules logs + skips when the target name is taken; no exception, no deprecated flag."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        # A device-level interface already owns the name the rule would produce.
        Interface.objects.create(device=self.device, name="et-0/0/0", type="10gbase-x-sfpp")
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        with self.assertLogs("netbox_interface_name_rules", level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, self.bay0)

        self.assertEqual(renamed, 0)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "0")  # left untouched, not renamed onto the collision
        self.assertTrue(any("already exists" in m for m in logs.output))
        # A collision-driven 0-count must NOT mark the rule potentially-deprecated.
        self.assertFalse(rule.tags.filter(slug="potentially-deprecated").exists())

    def test_apply_rule_to_existing_records_conflict_and_continues(self):
        """A taken target name is reported as a skipped member; the batch keeps going."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        Interface.objects.create(device=self.device, name="et-0/0/0", type="10gbase-x-sfpp")
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        count = apply_rule_to_existing(rule)

        self.assertEqual(count.changed_count, 0)
        self.assertEqual([member.target_name for member in count.skipped_members], ["et-0/0/0"])
        iface.refresh_from_db()
        self.assertEqual(iface.name, "0")

    def test_breakout_collision_skips_only_that_channel(self):
        """A breakout channel whose name is taken elsewhere on the device is skipped; the rest are created."""
        # Rule looked up internally by apply_interface_name_rules via module_type.
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="Hu0/0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        # Channel :2's name is already taken by an unrelated device-level interface.
        Interface.objects.create(device=self.device, name="Hu0/0/0/0:2", type="100gbase-x-qsfp28")
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="100gbase-x-qsfp28")

        renamed = apply_interface_name_rules(module, self.bay0)

        # base→:0, plus :1 and :3 created; :2 skipped (collision)
        self.assertEqual(renamed, 3)
        module_names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(module_names, ["Hu0/0/0/0:0", "Hu0/0/0/0:1", "Hu0/0/0/0:3"])
        # The pre-existing device-level interface is untouched.
        self.assertTrue(Interface.objects.filter(device=self.device, module=None, name="Hu0/0/0/0:2").exists())

    def test_install_path_save_race_skips_one_interface_without_aborting_batch(self):
        """A post-check save race on one interface is logged + skipped; the rest still rename.

        The collision pre-check closes the common case, but a concurrent insert can
        still win between the check and the save, a true race we cannot reproduce
        deterministically. The first save injects the PostgreSQL constraint diagnostic
        that identifies this race. Everything else uses the real install path.
        """
        from django.db import IntegrityError

        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-{base}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="a", type="10gbase-x-sfpp")
        Interface.objects.create(device=self.device, module=module, name="b", type="10gbase-x-sfpp")

        real_save = Interface.save
        calls = {"n": 0}

        def flaky_save(self_iface, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:  # first interface loses the race
                cause = Exception("duplicate key value violates unique constraint")
                cause.diag = SimpleNamespace(constraint_name=INTERFACE_NAME_CONSTRAINT)
                raise IntegrityError("duplicate key value violates unique constraint") from cause
            return real_save(self_iface, *args, **kwargs)

        with patch.object(Interface, "save", flaky_save):
            # force_reapply so both interfaces are in scope regardless of raw names.
            renamed = apply_interface_name_rules(module, self.bay0, force_reapply=True)

        # Batch continued past the racing interface: exactly one renamed, one rolled back.
        self.assertEqual(renamed, 1)
        names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(len(names), 2)
        self.assertEqual(len([n for n in names if n.startswith("et-")]), 1)  # one renamed
        self.assertEqual(len([n for n in names if not n.startswith("et-")]), 1)  # one raced (unchanged)

    def test_idempotent_breakout_reapply_records_no_conflict(self):
        """Re-applying an already-applied breakout records no false conflicts for its own channels."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="Hu0/0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="100gbase-x-qsfp28")

        first = apply_interface_name_rules(module, self.bay0)
        self.assertEqual(first, 4)

        second = apply_rule_to_existing(rule)
        self.assertEqual(second.changed_count, 0)
        self.assertEqual(second.skipped_members, ())  # its own existing channels are NOT conflicts
        self.assertEqual(Interface.objects.filter(module=module).count(), 4)
