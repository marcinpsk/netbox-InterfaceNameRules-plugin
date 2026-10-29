# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for the lower-level rule-selection seam."""

from dcim.models import DeviceType, Manufacturer, ModuleType, Platform
from django.db import IntegrityError, transaction
from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.rule_selection import (
    _build_candidates,
    _platform_lineage,
    _platform_subtree,
    find_matching_rule,
    platform_lineage,
    platform_subtree,
)


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

    def test_more_scope_beats_a_higher_score_from_a_longer_pattern(self):
        """A short parent-scoped regex beats a long device-and-platform regex with a higher score."""
        parent_module_type = ModuleType.objects.create(
            manufacturer=self.module_type.manufacturer,
            model="SELECTOR-LONG-CHASSIS",
            part_number="SELECTOR-LONG-CHASSIS",
        )
        platform = Platform.objects.create(name="SelectorLongOS", slug="selector-long-os")
        parent_scoped = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="S.*",
            parent_module_type=parent_module_type,
            name_template="parent{bay_position}",
        )
        device_and_platform_scoped = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="S" + "E?" * 100 + ".*",
            device_type=self.device_type,
            platform=platform,
            name_template="devplat{bay_position}",
        )

        selected = find_matching_rule(self.module_type, parent_module_type, self.device_type, platform)

        self.assertEqual(parent_scoped.specificity_score, 403)
        self.assertEqual(device_and_platform_scoped.specificity_score, 503)
        self.assertEqual(selected, parent_scoped)

    def test_a_root_platform_scope_matches_its_exact_platform_on_every_release(self):
        """NetBox 4.3 does not nest platforms, so there a platform scope is its exact platform."""
        platform = Platform.objects.create(name="SelectorExactOS", slug="selector-exact-os")
        other = Platform.objects.create(name="SelectorOtherOS", slug="selector-other-os")
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type, platform=platform, name_template="exact{bay_position}"
        )

        self.assertEqual(find_matching_rule(self.module_type, None, self.device_type, platform), rule)
        self.assertIsNone(find_matching_rule(self.module_type, None, self.device_type, other))
        self.assertEqual(platform_lineage(platform), [platform.pk])
        self.assertEqual(platform_subtree(platform.pk), {platform.pk})

    def test_a_device_interface_rule_is_never_selected_for_a_module(self):
        """Module selection skips device-interface rules, and the row that used to leak cannot exist.

        ``save()`` turns ``module_type_is_regex`` off on a device rule, and the check constraint
        refuses a write that skips ``save()``.
        """
        rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_is_regex=True,
            module_type_pattern="SELECTOR-.*",
            name_template="leaked{vc_position}",
        )
        rule.refresh_from_db()
        self.assertFalse(rule.module_type_is_regex)
        with self.assertRaises(IntegrityError), transaction.atomic():
            InterfaceNameRule.objects.filter(pk=rule.pk).update(module_type_is_regex=True)

        self.assertIsNone(find_matching_rule(self.module_type, None, self.device_type))

    def test_candidate_order_descends_by_specificity_score(self):
        """Candidate order is the scope weighting of specificity_score, counted down from 7 to 0."""
        candidates = _build_candidates("parent", "device", ["platform"])

        weights = [
            (4 if parent else 0) + (2 if device else 0) + (1 if platform else 0)
            for parent, device, platform in candidates
        ]

        self.assertEqual(weights, [7, 6, 5, 4, 3, 2, 1, 0])

    def test_candidates_try_the_closer_platform_first_in_each_scope(self):
        """Within one parent and device scope, the device platform comes before its ancestors, then no platform."""
        candidates = _build_candidates("parent", None, ["child", "ancestor"])

        self.assertEqual(
            candidates,
            [
                ("parent", None, "child"),
                ("parent", None, "ancestor"),
                ("parent", None, None),
                (None, None, "child"),
                (None, None, "ancestor"),
                (None, None, None),
            ],
        )

    def test_a_subtree_walks_only_from_the_requested_platform(self):
        """A parent cycle in an unrelated tree does not change, or stop, the subtree of another platform."""
        parents = {1: None, 2: 1, 3: 2, 4: 1, 10: 11, 11: 10, 12: None}

        self.assertEqual(_platform_subtree(1, parents), {1, 2, 3, 4})
        self.assertEqual(_platform_subtree(2, parents), {2, 3})
        self.assertEqual(_platform_subtree(12, parents), {12})
        self.assertEqual(_platform_subtree(99, parents), {99})

    def test_a_subtree_walk_ends_inside_a_cycle(self):
        """A cycle reachable from the requested platform ends the walk instead of looping."""
        self.assertEqual(_platform_subtree(10, {10: 11, 11: 10, 12: 11}), {10, 11, 12})

    def test_a_platform_cycle_fails_instead_of_looping(self):
        """A platform map in which a platform is its own ancestor raises instead of looping."""
        self.assertEqual(_platform_lineage(3, {3: 2, 2: 1, 1: None}), [3, 2, 1])
        with self.assertRaisesMessage(ValueError, "Platform 3 is its own ancestor."):
            _platform_lineage(3, {3: 2, 2: 3})
