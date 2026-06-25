# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for rule matching and interface renaming.

These tests create real DB objects and exercise the full engine pipeline.
"""

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
    Platform,
    Site,
)
from django.test import TestCase

from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    build_variables,
    find_matching_rule,
)
from netbox_interface_name_rules.models import InterfaceNameRule


class FindMatchingRuleTest(TestCase):
    """Test rule lookup priority ordering."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="TestMfg", slug="testmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="TEST-SFP", part_number="TEST-SFP")
        cls.parent_module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="TEST-CONVERTER", part_number="TEST-CONVERTER"
        )
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="TEST-DEVICE", slug="test-device")
        cls.platform = Platform.objects.create(name="TEST-OS", slug="test-os")

    def test_no_rules_returns_none(self):
        result = find_matching_rule(self.module_type, None, None)
        self.assertIsNone(result)

    def test_universal_rule_matches(self):
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="port{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, None)
        self.assertEqual(result, rule)

    def test_device_specific_rule_preferred(self):
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        device_specific = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="specific{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, self.device_type)
        self.assertEqual(result, device_specific)

    def test_parent_specific_rule_preferred(self):
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        parent_specific = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            parent_module_type=self.parent_module_type,
            name_template="parent{bay_position}",
        )
        result = find_matching_rule(self.module_type, self.parent_module_type, None)
        self.assertEqual(result, parent_specific)

    def test_full_match_most_specific(self):
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="device{bay_position}",
        )
        full = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            parent_module_type=self.parent_module_type,
            device_type=self.device_type,
            name_template="full{bay_position}",
        )
        result = find_matching_rule(self.module_type, self.parent_module_type, self.device_type)
        self.assertEqual(result, full)

    def test_platform_specific_rule_preferred(self):
        """Platform-scoped rule is preferred over unscoped rule."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        platform_specific = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            platform=self.platform,
            name_template="platform{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, None, platform=self.platform)
        self.assertEqual(result, platform_specific)

    def test_disabled_rule_not_returned(self):
        """Disabled exact rule is never returned even if it matches."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="disabled{bay_position}",
            enabled=False,
        )
        result = find_matching_rule(self.module_type, None, None)
        self.assertIsNone(result)

    def test_platform_none_does_not_match_platform_scoped_rule(self):
        """With platform=None, a platform-scoped rule is NOT matched."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            platform=self.platform,
            name_template="platform-only{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, None, platform=None)
        self.assertIsNone(result)

    def test_platform_none_falls_back_to_unscoped_rule(self):
        """With platform=None, an unscoped rule is preferred over a platform-scoped one."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            platform=self.platform,
            name_template="platform{bay_position}",
        )
        unscoped = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        result = find_matching_rule(self.module_type, None, None, platform=None)
        self.assertEqual(result, unscoped)


class BuildVariablesTest(TestCase):
    """Test build_variables from module bay context."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="VarMfg", slug="varmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="VAR-DEV", slug="var-dev")
        # Templates must be created BEFORE devices (instantiated on device creation)
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 5", position="5")
        role = DeviceRole.objects.create(name="VarRole", slug="varrole")
        site = Site.objects.create(name="VarSite", slug="varsite")
        cls.device = Device.objects.create(name="var-test-01", device_type=cls.device_type, role=role, site=site)

    def test_simple_bay_variables(self):
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 5")
        variables = build_variables(bay)
        self.assertEqual(variables["bay_position"], "5")
        self.assertEqual(variables["bay_position_num"], "5")
        self.assertEqual(variables["slot"], "5")

    def test_non_numeric_position(self):
        """Bay position with text prefix (e.g., 'swp1')."""
        manufacturer = Manufacturer.objects.create(name="NNMfg", slug="nnmfg")
        dt = DeviceType.objects.create(manufacturer=manufacturer, model="NN-DEV", slug="nn-dev")
        ModuleBayTemplate.objects.create(device_type=dt, name="Transceiver swp3", position="swp3")
        role = DeviceRole.objects.create(name="NNRole", slug="nnrole")
        site = Site.objects.create(name="NNSite", slug="nnsite")
        device = Device.objects.create(name="nn-test-01", device_type=dt, role=role, site=site)
        bay = ModuleBay.objects.get(device=device, name="Transceiver swp3")
        variables = build_variables(bay)
        self.assertEqual(variables["bay_position"], "swp3")
        self.assertEqual(variables["bay_position_num"], "3")


class ApplyInterfaceNameRulesTest(TestCase):
    """Test full apply_interface_name_rules pipeline."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ApplyMfg", slug="applymfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="APPLY-DEV", slug="apply-dev")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="APPLY-SFP", part_number="APPLY-SFP"
        )
        # Templates before device
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 0", position="0")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 1", position="1")
        role = DeviceRole.objects.create(name="ApplyRole", slug="applyrole")
        site = Site.objects.create(name="ApplySite", slug="applysite")
        cls.device = Device.objects.create(name="apply-test-01", device_type=cls.device_type, role=role, site=site)

    def test_simple_rename(self):
        """Module install with matching rule renames the interface."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="et-0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        # Simulate what NetBox does: create an interface with the bay position as name
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

    def test_no_matching_rule(self):
        """No rule exists — interfaces left untouched."""
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 1")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="1", type="10gbase-x-sfpp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 0)

    def test_idempotency_guard(self):
        """Already-renamed interfaces are not re-processed."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="et-0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        # Interface already has the final name (not the raw bay position)
        Interface.objects.create(device=self.device, module=module, name="et-0/0/0", type="10gbase-x-sfpp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 0)

    def test_breakout_creates_channels(self):
        """Breakout rule creates multiple channel interfaces."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 4)
        iface_names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(iface_names, ["xe-0/0/0:0", "xe-0/0/0:1", "xe-0/0/0:2", "xe-0/0/0:3"])

    def test_breakout_channel_start_offset(self):
        """Breakout with channel_start=1 (Cisco style)."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="HundredGigE0/0/0/{bay_position}/{channel}",
            channel_count=2,
            channel_start=1,
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        Interface.objects.create(device=self.device, module=module, name="0", type="100gbase-x-qsfp28")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 2)
        names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(names, ["HundredGigE0/0/0/0/1", "HundredGigE0/0/0/0/2"])

    def test_no_interfaces_returns_zero(self):
        """Module with no interfaces — nothing to rename."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="et-0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 1")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 0)


class FindMatchingRuleCachingTest(TestCase):
    """find_matching_rule loads rules once and memoizes per context, and reloads when rules change."""

    @classmethod
    def setUpTestData(cls):
        mfr = Manufacturer.objects.create(name="CacheMfg", slug="cachemfg")
        cls.module_type = ModuleType.objects.create(manufacturer=mfr, model="C-SFP", part_number="C-SFP")
        cls.device_type = DeviceType.objects.create(manufacturer=mfr, model="C-DEV", slug="c-dev")

    def test_repeated_calls_do_not_re_query_rules(self):
        """A second identical call is served from the cache — only the cheap version-fingerprint query runs."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="port{bay_position}")
        find_matching_rule(self.module_type, None, self.device_type)  # warm (rule create bumped the version)

        with CaptureQueriesContext(connection) as ctx:
            find_matching_rule(self.module_type, None, self.device_type)

        # No per-candidate rule lookups — at most the (count, max last_updated) fingerprint query.
        self.assertLessEqual(len(ctx), 1, [q["sql"] for q in ctx.captured_queries])

    def test_rule_change_invalidates_cache(self):
        """Adding a more specific rule changes the fingerprint, so the next call reloads and matches it."""
        find_matching_rule(self.module_type, None, self.device_type)  # warm with the current rule set

        specific = InterfaceNameRule.objects.create(
            module_type=self.module_type, device_type=self.device_type, name_template="specific{bay_position}"
        )
        result = find_matching_rule(self.module_type, None, self.device_type)

        self.assertEqual(result, specific)
