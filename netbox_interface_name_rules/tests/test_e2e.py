# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""End-to-end integration tests using the REST API.

These tests exercise the full pipeline: create device types, module types
with interface templates, devices, interface name rules, then install
modules via API and verify that interfaces are correctly renamed.

Scenarios covered:
- Simple rename (QSFP-100G on Juniper)
- Breakout channels (QSFP-4X10G on Juniper)
- Channel start offset (QSFP-DD 2x100G on Cisco)
- Converter offset with arithmetic ({parent_bay_position})
- bay_position_num extraction (UfiSpace swpN naming)
"""

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
    Site,
)
from django.test import TestCase

from netbox_interface_name_rules.engine import apply_interface_name_rules
from netbox_interface_name_rules.models import InterfaceNameRule


class E2ESimpleRenameTest(TestCase):
    """Juniper ACX7024: QSFP-100G-LR4 → et-0/0/{bay_position}."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="Juniper-E2E", slug="juniper-e2e")
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="ACX7024-E2E", slug="acx7024-e2e")
        cls.module_type = ModuleType.objects.create(
            manufacturer=mfg, model="QSFP-100G-LR4-E2E", part_number="QSFP-100G-LR4-E2E"
        )
        # Module type has one interface template
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="{module}", type="100gbase-x-qsfp28")
        # Device has module bays
        for i in range(4):
            ModuleBayTemplate.objects.create(device_type=cls.device_type, name=f"Transceiver {i}", position=str(i))
        role = DeviceRole.objects.create(name="E2E-Router", slug="e2e-router")
        site = Site.objects.create(name="E2E-Site", slug="e2e-site")
        cls.device = Device.objects.create(name="acx7024-e2e-01", device_type=cls.device_type, role=role, site=site)
        # Create the rename rule
        InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            device_type=cls.device_type,
            name_template="et-0/0/{bay_position}",
        )

    def test_install_module_renames_interface(self):
        """Installing a module creates an interface and renames it."""
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 2")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        # Module.save() creates interfaces from templates
        interfaces = list(Interface.objects.filter(module=module))
        self.assertEqual(len(interfaces), 1)
        # The interface was created with the bay position as name
        # Apply the rename rules
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 1)
        interfaces[0].refresh_from_db()
        self.assertEqual(interfaces[0].name, "et-0/0/2")

    def test_multiple_bays(self):
        """Rules work across different bay positions."""
        for bay_pos in ["0", "1", "3"]:
            bay = ModuleBay.objects.get(device=self.device, name=f"Transceiver {bay_pos}")
            module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
            renamed = apply_interface_name_rules(module, bay)
            self.assertEqual(renamed, 1)
            iface = Interface.objects.filter(module=module).first()
            self.assertEqual(iface.name, f"et-0/0/{bay_pos}")


class E2EBreakoutTest(TestCase):
    """Juniper ACX7024: QSFP-4X10G-LR → xe-0/0/{bay_position}:{channel}."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="Juniper-BR", slug="juniper-br")
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="ACX7024-BR", slug="acx7024-br")
        cls.module_type = ModuleType.objects.create(
            manufacturer=mfg, model="QSFP-4X10G-LR-BR", part_number="QSFP-4X10G-LR-BR"
        )
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="{module}", type="10gbase-x-sfpp")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 4", position="4")
        role = DeviceRole.objects.create(name="BR-Router", slug="br-router")
        site = Site.objects.create(name="BR-Site", slug="br-site")
        cls.device = Device.objects.create(name="acx7024-br-01", device_type=cls.device_type, role=role, site=site)
        InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            device_type=cls.device_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )

    def test_breakout_creates_four_channels(self):
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 4")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 4)
        names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(names, ["xe-0/0/4:0", "xe-0/0/4:1", "xe-0/0/4:2", "xe-0/0/4:3"])


class E2EChannelStartOffsetTest(TestCase):
    """Cisco 8201-SYS: QSFP28-DD-2X100G → HundredGigE0/0/0/{bay_position}/{channel} with channel_start=0."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="Cisco-CS", slug="cisco-cs")
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="8201-SYS-CS", slug="8201-sys-cs")
        cls.module_type = ModuleType.objects.create(
            manufacturer=mfg, model="QSFP28-DD-2X100G-CS", part_number="QSFP28-DD-2X100G-CS"
        )
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="{module}", type="100gbase-x-qsfp28")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 5", position="5")
        role = DeviceRole.objects.create(name="CS-Router", slug="cs-router")
        site = Site.objects.create(name="CS-Site", slug="cs-site")
        cls.device = Device.objects.create(name="8201-cs-01", device_type=cls.device_type, role=role, site=site)
        InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            device_type=cls.device_type,
            name_template="HundredGigE0/0/0/{bay_position}/{channel}",
            channel_count=2,
            channel_start=0,
        )

    def test_dual_breakout_with_offset(self):
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 5")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 2)
        names = sorted(Interface.objects.filter(module=module).values_list("name", flat=True))
        self.assertEqual(names, ["HundredGigE0/0/0/5/0", "HundredGigE0/0/0/5/1"])


class E2EBayPositionNumTest(TestCase):
    """UfiSpace S9610: bay position is numeric but interface uses swp prefix."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="UfiSpace-BPN", slug="ufispace-bpn")
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="S9610-36D-BPN", slug="s9610-36d-bpn")
        cls.module_type = ModuleType.objects.create(
            manufacturer=mfg, model="QSFP-DD-400G-BPN", part_number="QSFP-DD-400G-BPN"
        )
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="{module}", type="400gbase-x-qsfpdd")
        # Bay with numeric position — interface template creates "5",
        # rule renames to "swp5"
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 5", position="5")
        role = DeviceRole.objects.create(name="BPN-Switch", slug="bpn-switch")
        site = Site.objects.create(name="BPN-Site", slug="bpn-site")
        cls.device = Device.objects.create(name="s9610-bpn-01", device_type=cls.device_type, role=role, site=site)
        InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            device_type=cls.device_type,
            name_template="swp{bay_position_num}",
        )

    def test_bay_position_num_extraction(self):
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 5")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 1)
        iface = Interface.objects.filter(module=module).first()
        self.assertEqual(iface.name, "swp5")


class E2EConverterOffsetTest(TestCase):
    """CVR-X2-SFP converter: arithmetic offset expression."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="Cisco-CVR", slug="cisco-cvr")
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="WS-C4900M-CVR", slug="ws-c4900m-cvr")
        # X2 converter module type (parent) — has a sub-bay for SFP
        cls.converter_type = ModuleType.objects.create(
            manufacturer=mfg, model="CVR-X2-SFP-CVR", part_number="CVR-X2-SFP-CVR"
        )
        ModuleBayTemplate.objects.create(module_type=cls.converter_type, name="SFP Slot 0", position="0")
        # SFP module type (child, installed inside converter)
        cls.sfp_type = ModuleType.objects.create(manufacturer=mfg, model="SFP-1G-T-CVR", part_number="SFP-1G-T-CVR")
        InterfaceTemplate.objects.create(module_type=cls.sfp_type, name="{module}", type="1000base-t")

        # Device has X2 bays (parent bays for converter)
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="X2 Bay 1", position="1")
        role = DeviceRole.objects.create(name="CVR-Switch", slug="cvr-switch")
        site = Site.objects.create(name="CVR-Site", slug="cvr-site")
        cls.device = Device.objects.create(name="ws-c4900m-cvr-01", device_type=cls.device_type, role=role, site=site)

        InterfaceNameRule.objects.create(
            module_type=cls.sfp_type,
            parent_module_type=cls.converter_type,
            name_template="GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}",
        )

    def test_converter_offset_arithmetic(self):
        """SFP in CVR-X2-SFP at bay 1 → GigabitEthernet1/8."""
        # Install converter into X2 bay
        x2_bay = ModuleBay.objects.get(device=self.device, name="X2 Bay 1")
        converter = Module.objects.create(device=self.device, module_bay=x2_bay, module_type=self.converter_type)

        # NetBox auto-creates sub-bay from ModuleBayTemplate on converter module type
        sfp_bay = ModuleBay.objects.get(module=converter, name="SFP Slot 0")

        # Install SFP into the converter's sub-bay
        sfp_module = Module.objects.create(device=self.device, module_bay=sfp_bay, module_type=self.sfp_type)

        renamed = apply_interface_name_rules(sfp_module, sfp_bay)
        self.assertEqual(renamed, 1)
        iface = Interface.objects.filter(module=sfp_module).first()
        # slot=1, 8 + (1 - 1) * 2 + 0 = 8
        self.assertEqual(iface.name, "GigabitEthernet1/8")
