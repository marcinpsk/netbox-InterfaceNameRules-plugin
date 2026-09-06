# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for the lower-level naming seam."""

from dcim.models import (
    Device,
    DeviceType,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    VirtualChassis,
)
from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.naming import build_variables, evaluate_name_template
from netbox_interface_name_rules.tests.helpers import make_placement


class NamingTest(TestCase):
    """Exercise naming through its lower-level public interface."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="NamingMfg", slug="naming-mfg")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="NAMING-DEVICE",
            slug="naming-device",
        )
        ModuleBayTemplate.objects.create(device_type=device_type, name="Slot 7", position="7")
        module_type = ModuleType.objects.create(
            manufacturer=manufacturer,
            model="NAMING-SFP",
            part_number="NAMING-SFP",
        )
        placement = make_placement("Naming")
        virtual_chassis = VirtualChassis.objects.create(name="naming-vc")
        cls.device = Device.objects.create(
            name="naming-device-01",
            device_type=device_type,
            role=placement.role,
            site=placement.site,
            virtual_chassis=virtual_chassis,
            vc_position=3,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="Slot 7")
        cls.module = Module.objects.create(device=cls.device, module_bay=cls.bay, module_type=module_type)
        cls.rule = InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="xe-{vc_position}/{slot}/{bay_position_num}",
        )

    def test_real_module_context_builds_and_evaluates_the_rule_name(self):
        variables = build_variables(self.module.module_bay, device=self.module.device)

        name = evaluate_name_template(self.rule.name_template, variables)

        self.assertEqual(
            variables,
            {
                "slot": "7",
                "bay_position": "7",
                "bay_position_num": "7",
                "parent_bay_position": "0",
                "sfp_slot": "7",
                "vc_position": "3",
            },
        )
        self.assertEqual(name, "xe-3/7/7")
