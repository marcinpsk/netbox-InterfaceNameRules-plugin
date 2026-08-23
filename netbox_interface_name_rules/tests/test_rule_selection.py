# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for the lower-level rule-selection seam."""

from dcim.models import DeviceType, Manufacturer, ModuleType
from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.rule_selection import find_matching_rule


class RuleSelectionTest(TestCase):
    """Exercise rule selection through its lower-level public interface."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="SelectorMfg", slug="selector-mfg")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer,
            model="SELECTOR-SFP",
            part_number="SELECTOR-SFP",
        )
        cls.device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="SELECTOR-DEVICE",
            slug="selector-device",
        )

    def test_exact_rule_prefers_matching_device_scope(self):
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="generic{bay_position}",
        )
        scoped_rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="scoped{bay_position}",
        )

        selected = find_matching_rule(self.module_type, None, self.device_type)

        self.assertEqual(selected, scoped_rule)
