# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for apply_device_interface_rules — VC device-level interface renaming."""

from dcim.models import (
    Device,
    DeviceType,
    Interface,
    Manufacturer,
    ModuleBayTemplate,
    ModuleType,
    Platform,
    VirtualChassis,
)
from django.test import TestCase

from netbox_interface_name_rules.engine import apply_device_interface_rules
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import make_placement


class ApplyDeviceInterfaceRulesTest(TestCase):
    """Test apply_device_interface_rules with VC-member devices."""

    @classmethod
    def setUpTestData(cls):
        """Create shared DB fixtures for device-rule tests."""
        manufacturer = Manufacturer.objects.create(name="DevRuleMfg", slug="devrulemfg")
        cls.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="DevRule-Switch", slug="devrule-switch"
        )
        cls.platform = Platform.objects.create(name="DevRule-IOS", slug="devrule-ios")
        placement = make_placement("DevRule")

        cls.vc = VirtualChassis.objects.create(name="devrule-vc")
        cls.device1 = Device.objects.create(
            name="devrule-sw1",
            device_type=cls.device_type,
            role=placement.role,
            site=placement.site,
            virtual_chassis=cls.vc,
            vc_position=1,
            platform=cls.platform,
        )
        cls.device_no_vc = Device.objects.create(
            name="devrule-standalone",
            device_type=cls.device_type,
            role=placement.role,
            site=placement.site,
        )
        # Another device with vc_position=None (edge case: VC set but position unset)
        cls.vc2 = VirtualChassis.objects.create(name="devrule-vc2")
        cls.device_no_pos = Device.objects.create(
            name="devrule-nopos",
            device_type=cls.device_type,
            role=placement.role,
            site=placement.site,
            virtual_chassis=cls.vc2,
            vc_position=None,
        )

    def _make_interface(self, name, device=None):
        """Create a module=None interface on device1 (or given device)."""
        if device is None:
            device = self.device1
        return Interface.objects.create(device=device, name=name, type="1000base-t", module=None)

    def _make_rule(self, template, pattern=None, device_type=None, platform=None):
        """Create a device-level rule; module_type_pattern is optional interface name filter."""
        return InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern=pattern or "",
            name_template=template,
            device_type=device_type,
            platform=platform,
        )

    # ------------------------------------------------------------------
    # Guard clauses
    # ------------------------------------------------------------------

    def test_non_vc_device_returns_zero(self):
        """Standalone device (no virtual_chassis) must be skipped."""
        self._make_rule("Gi{vc_position}/{port}")
        result = apply_device_interface_rules(self.device_no_vc)
        self.assertEqual(result, 0)

    def test_vc_position_none_returns_zero(self):
        """Device in VC but vc_position=None must be skipped."""
        self._make_rule("Gi{vc_position}/{port}")
        result = apply_device_interface_rules(self.device_no_pos)
        self.assertEqual(result, 0)

    def test_no_matching_rules_returns_zero(self):
        """No rules for device type → return 0 without error."""
        # Rule scoped to a different device_type — should not apply
        other_mfg = Manufacturer.objects.create(name="OtherMfg", slug="othermfg")
        other_dt = DeviceType.objects.create(manufacturer=other_mfg, model="Other-Switch", slug="other-switch")
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="Gi{vc_position}/{port}",
            device_type=other_dt,
        )
        self._make_interface("Gi0/1")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 0)

    def test_no_interfaces_returns_zero(self):
        """Device with no module=None interfaces → return 0."""
        self._make_rule("Gi{vc_position}/{port}")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 0)

    def test_python_only_filter_written_without_validation_is_skipped(self):
        """A legacy filter that RE2 cannot compile must not run through Python's engine."""
        self._make_rule("Gi{vc_position}/{port}", pattern=r"(?=Gi)Gi\d+/\d+")
        interface = self._make_interface("Gi0/1")

        self.assertEqual(apply_device_interface_rules(self.device1), 0)
        interface.refresh_from_db()
        self.assertEqual(interface.name, "Gi0/1")

    # ------------------------------------------------------------------
    # Successful renames
    # ------------------------------------------------------------------

    def test_simple_rename_with_port_variable(self):
        """Rename Gi0/1 → Gi1/1 using {vc_position}/{port} template."""
        self._make_rule("Gi{vc_position}/{port}")
        iface = self._make_interface("Gi0/1")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi1/1")

    def test_rename_uses_base_variable(self):
        """Template using {vc_position} and {base} (full name)."""
        self._make_rule("{vc_position}/{base}")
        iface = self._make_interface("Gi0/2")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "1/Gi0/2")

    def test_interface_already_correct_skipped(self):
        """Interface with name equal to template output is not saved."""
        self._make_rule("Gi{vc_position}/{port}")
        # vc_position=1, port from "Gi1/3" rsplit "/"[-1] = "3" → "Gi1/3"
        iface = self._make_interface("Gi1/3")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 0)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi1/3")

    def test_validation_failure_is_logged_and_skipped(self):
        """A computed name that fails full_clean (here, exceeds Interface.name max_length) is logged and
        skipped with the original name restored — it must not raise out of the VC re-rename batch."""
        # Derive the over-length name from the live field limit so the test still exercises the
        # validation branch if Interface.name's max_length ever changes. vc_position=1 adds one char,
        # so (max_length + 1) literal chars guarantees the rendered name exceeds the limit.
        max_length = Interface._meta.get_field("name").max_length
        self._make_rule("G" * (max_length + 1) + "{vc_position}")
        iface = self._make_interface("Gi0/1")

        # assertLogs asserts the warning branch actually fired — without it, "no rule matched" would
        # also yield result==0 and a preserved name, so the test would pass without covering the path.
        with self.assertLogs("netbox_interface_name_rules.family.execution", level="WARNING") as logs:
            result = apply_device_interface_rules(self.device1)

        self.assertEqual(result, 0)  # validation failed → nothing renamed
        self.assertTrue(
            any("NetBox rejected rename" in line for line in logs.output),
            logs.output,
        )
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi0/1")  # original name preserved (reverted)

    def test_rename_multiple_interfaces(self):
        """Multiple interfaces on same device renamed in one call."""
        self._make_rule("Gi{vc_position}/{port}")
        iface1 = self._make_interface("Gi0/1")
        iface2 = self._make_interface("Gi0/2")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 2)
        iface1.refresh_from_db()
        iface2.refresh_from_db()
        self.assertEqual(iface1.name, "Gi1/1")
        self.assertEqual(iface2.name, "Gi1/2")

    # ------------------------------------------------------------------
    # Pattern (interface name filter)
    # ------------------------------------------------------------------

    def test_pattern_filter_matches(self):
        """Rule with pattern only renames matching interface names."""
        self._make_rule("Gi{vc_position}/{port}", pattern=r"Gi\d+/\d+")
        iface = self._make_interface("Gi0/5")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi1/5")

    def test_pattern_filter_no_match_skipped(self):
        """Rule with pattern does not rename non-matching interface names."""
        self._make_rule("Gi{vc_position}/{port}", pattern=r"Gi\d+/\d+")
        iface = self._make_interface("Management0")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 0)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Management0")

    def test_invalid_regex_pattern_skips_interface(self):
        """Invalid regex in pattern → interface skipped, no exception raised."""
        self._make_rule("Gi{vc_position}/{port}", pattern="[invalid(")
        iface = self._make_interface("Gi0/1")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 0)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi0/1")

    # ------------------------------------------------------------------
    # Rule scoping (device_type and platform)
    # ------------------------------------------------------------------

    def test_device_type_scoped_rule_applies(self):
        """Rule scoped to this device_type applies correctly."""
        self._make_rule("Gi{vc_position}/{port}", device_type=self.device_type)
        iface = self._make_interface("Gi0/1")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi1/1")

    def test_platform_scoped_rule_applies(self):
        """Rule scoped to this platform applies correctly."""
        self._make_rule("Gi{vc_position}/{port}", platform=self.platform)
        iface = self._make_interface("Gi0/1")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi1/1")

    # ------------------------------------------------------------------
    # Priority / first-match-wins via renamed_pks
    # ------------------------------------------------------------------

    def test_first_matching_rule_wins(self):
        """When two rules match the same interface, only the first (by specificity) renames it."""
        # High-specificity rule (device_type scoped) renames first
        self._make_rule("Gi{vc_position}/{port}", device_type=self.device_type)
        # Low-specificity rule (global) should be skipped for the already-renamed interface
        self._make_rule("OVERWRITTEN{vc_position}/{port}")
        iface = self._make_interface("Gi0/1")
        result = apply_device_interface_rules(self.device1)
        # Only 1 rename total (second rule skipped via renamed_pks)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi1/1")

    def test_first_matching_rule_wins_when_name_is_already_correct(self):
        """A no-op from the first rule prevents a less specific rule from renaming the interface."""
        self._make_rule("Gi{vc_position}/{port}", device_type=self.device_type)
        self._make_rule("OVERWRITTEN{vc_position}/{port}")
        iface = self._make_interface("Gi1/1")

        result = apply_device_interface_rules(self.device1)

        self.assertEqual(result, 0)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi1/1")

    def test_disabled_rule_skipped(self):
        """Disabled rules are not applied."""
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="Gi{vc_position}/{port}",
            enabled=False,
        )
        iface = self._make_interface("Gi0/1")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 0)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi0/1")

    def test_no_slash_in_name_uses_full_name_as_port(self):
        """Interface without '/' — port variable equals the full name."""
        self._make_rule("{vc_position}-{port}")
        iface = self._make_interface("Management0")
        result = apply_device_interface_rules(self.device1)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "1-Management0")


class ApplyDeviceInterfaceRulesModuleTypeTest(TestCase):
    """Test apply_device_interface_rules with a ModuleType setup (device_type = None rules)."""

    @classmethod
    def setUpTestData(cls):
        """Create VC device with module type for cross-fixture isolation."""
        manufacturer = Manufacturer.objects.create(name="DevRuleMfg2", slug="devrulemfg2")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="DR2-Switch", slug="dr2-switch")
        ModuleBayTemplate.objects.create(device_type=device_type, name="Slot 0", position="0")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="DR2-LC", part_number="DR2-LC")
        placement = make_placement("DR2")
        vc = VirtualChassis.objects.create(name="dr2-vc")
        cls.device = Device.objects.create(
            name="dr2-sw1",
            device_type=device_type,
            role=placement.role,
            site=placement.site,
            virtual_chassis=vc,
            vc_position=2,
        )

    def test_global_rule_applies_to_any_device_type(self):
        """Rule with no device_type scoping applies to all devices."""
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="Gi{vc_position}/{port}",
        )
        iface = Interface.objects.create(device=self.device, name="Gi0/3", type="1000base-t", module=None)
        result = apply_device_interface_rules(self.device)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Gi2/3")


class DeviceInterfaceEdgeCaseNamesTest(TestCase):
    """Test device-level rules with edge-case interface names."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="EdgeMfg", slug="edgemfg")
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="Edge-Dev", slug="edge-dev")
        placement = make_placement("Edge")
        vc = VirtualChassis.objects.create(name="edge-vc")
        cls.device = Device.objects.create(
            name="edge-sw1",
            device_type=cls.device_type,
            role=placement.role,
            site=placement.site,
            virtual_chassis=vc,
            vc_position=1,
        )

    def test_name_with_multiple_slashes(self):
        """Interface name like FortyGigE1/2/3/4 extracts port='4'."""
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="Fo{vc_position}/2/3/{port}",
        )
        iface = Interface.objects.create(
            device=self.device, name="FortyGigE1/2/3/4", type="40gbase-x-qsfpp", module=None
        )
        result = apply_device_interface_rules(self.device)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Fo1/2/3/4")

    def test_name_without_slash(self):
        """Interface name without / uses full name as port."""
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="eth{vc_position}-{port}",
        )
        iface = Interface.objects.create(device=self.device, name="eth0", type="1000base-t", module=None)
        result = apply_device_interface_rules(self.device)
        self.assertEqual(result, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "eth1-eth0")
