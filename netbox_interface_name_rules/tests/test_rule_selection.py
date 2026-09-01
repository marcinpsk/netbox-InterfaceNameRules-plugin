# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for the lower-level rule-selection seam."""

from dcim.models import DeviceType, Manufacturer, ModuleType, Platform
from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.rule_selection import _build_candidates, find_matching_rule


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

    def test_parent_scope_outranks_device_and_platform_together(self):
        """A parent-scoped rule wins over a device-and-platform rule, as specificity_score says."""
        parent_module_type = ModuleType.objects.create(
            manufacturer=self.module_type.manufacturer,
            model="SELECTOR-CHASSIS",
            part_number="SELECTOR-CHASSIS",
        )
        platform = Platform.objects.create(name="SelectorOS", slug="selector-os")
        parent_scoped = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            parent_module_type=parent_module_type,
            name_template="parent{bay_position}",
        )
        device_and_platform_scoped = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            device_type=self.device_type,
            platform=platform,
            name_template="devplat{bay_position}",
        )

        selected = find_matching_rule(self.module_type, parent_module_type, self.device_type, platform)

        self.assertEqual(parent_scoped.specificity_score, 1004)
        self.assertEqual(device_and_platform_scoped.specificity_score, 1003)
        self.assertEqual(selected, parent_scoped)

    def test_candidate_order_descends_by_specificity_score(self):
        """Candidate order is the scope weighting of specificity_score, counted down from 7 to 0."""
        candidates = _build_candidates("parent", "device", "platform")

        weights = [
            (4 if parent else 0) + (2 if device else 0) + (1 if platform else 0)
            for parent, device, platform in candidates
        ]

        self.assertEqual(weights, [7, 6, 5, 4, 3, 2, 1, 0])
