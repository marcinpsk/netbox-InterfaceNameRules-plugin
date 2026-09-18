# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for the lower-level naming seam."""

from unittest import skipUnless

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
from netbox_interface_name_rules.naming import build_variables, evaluate_name_template, numeric_suffix
from netbox_interface_name_rules.tests.helpers import make_placement


def _supports_module_placeholder():
    """Return True when this NetBox resolves ``{module}`` in module-bay positions.

    Probed from the helper that performs the resolution rather than a version comparison, so a
    backport or an upstream removal is detected by what NetBox actually provides. Without it a
    bay position stays literal, so a device type cannot compose the parent in at all.
    """
    from dcim import utils

    return hasattr(utils, "resolve_module_placeholder")


REQUIRES_MODULE_PLACEHOLDER = "NetBox does not resolve {module} in module-bay positions"


class NumericSuffixTest(TestCase):
    """The `_num` variables exist to be used in arithmetic, so they must parse as literals."""

    def test_a_zero_padded_position_yields_a_usable_literal(self):
        """Python rejects `02` as a decimal literal, so the suffix has to be canonical."""
        self.assertEqual(numeric_suffix("TenGigabitEthernet3/02"), "2")
        self.assertEqual(evaluate_name_template("Gi{8 + {n}}", {"n": numeric_suffix("bay/02")}), "Gi10")

    def test_a_non_ascii_digit_run_is_not_a_number(self):
        """`isdigit` accepts these, but neither the evaluator nor `int` can read them."""
        self.assertEqual(numeric_suffix("abc\u0662"), "0")
        self.assertEqual(numeric_suffix("p\u00b2"), "0")

    def test_a_position_without_digits_is_zero(self):
        self.assertEqual(numeric_suffix("swp"), "0")
        self.assertEqual(numeric_suffix(""), "0")

    def test_an_ordinary_position_is_unchanged(self):
        self.assertEqual(numeric_suffix("TenGigabitEthernet3/2"), "2")
        self.assertEqual(numeric_suffix("swp11"), "11")


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
                "slot_num": "7",
                "bay_position": "7",
                "bay_position_num": "7",
                "parent_bay_position": "0",
                "parent_bay_position_num": "0",
                "sfp_slot": "7",
                "vc_position": "3",
            },
        )
        self.assertEqual(name, "xe-3/7/7")


@skipUnless(_supports_module_placeholder(), REQUIRES_MODULE_PLACEHOLDER)
class NestedBayNumericTest(TestCase):
    """Composed bay positions are normal input, so every position has a numeric accessor.

    A device type that composes the parent into a bay position, the way the Catalyst 4900M and
    the MX304 do, gives leaf bays path-shaped positions such as ``TenGigabitEthernet3/2/1``.
    Arithmetic templates need the number out of each level.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="NestMfg", slug="nest-mfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="NEST-4900M", slug="nest-4900m")
        ModuleBayTemplate.objects.create(device_type=device_type, name="Slot 3", position="3")

        cls.line_card_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="NEST-LINECARD", part_number="NEST-LINECARD"
        )
        ModuleBayTemplate.objects.create(
            module_type=cls.line_card_type, name="X2 Port 2", position="TenGigabitEthernet{module}/2"
        )

        cls.twingig_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="NEST-CVR-X2-SFP", part_number="NEST-CVR-X2-SFP"
        )
        ModuleBayTemplate.objects.create(module_type=cls.twingig_type, name="SFP 1", position="{module}/1")

        placement = make_placement("Nest")
        cls.device = Device.objects.create(
            name="nest-4900m-01", device_type=device_type, role=placement.role, site=placement.site
        )
        slot = ModuleBay.objects.get(device=cls.device, name="Slot 3")
        line_card = Module.objects.create(device=cls.device, module_bay=slot, module_type=cls.line_card_type)
        x2_port = ModuleBay.objects.get(device=cls.device, module=line_card, name="X2 Port 2")
        twingig = Module.objects.create(device=cls.device, module_bay=x2_port, module_type=cls.twingig_type)
        cls.sfp_bay = ModuleBay.objects.get(device=cls.device, module=twingig, name="SFP 1")

    def test_the_fixture_composes_the_positions_the_device_type_intends(self):
        """Guards the fixture itself: without composed positions the rest asserts nothing."""
        self.assertEqual(self.sfp_bay.position, "TenGigabitEthernet3/2/1")
        self.assertEqual(self.sfp_bay.parent.position, "TenGigabitEthernet3/2")

    def test_every_position_variable_has_a_numeric_counterpart(self):
        variables = build_variables(self.sfp_bay)

        self.assertEqual(variables["bay_position"], "TenGigabitEthernet3/2/1")
        self.assertEqual(variables["bay_position_num"], "1")
        self.assertEqual(variables["parent_bay_position"], "TenGigabitEthernet3/2")
        self.assertEqual(variables["parent_bay_position_num"], "2")
        self.assertEqual(variables["slot"], "3")
        self.assertEqual(variables["slot_num"], "3")

    def test_a_two_level_hierarchy_resolves_the_slot_from_the_bare_parent(self):
        """A transceiver straight in a line card, no converter: the parent bay is a bare slot."""
        line_card_bay = ModuleBay.objects.get(device=self.device, name="X2 Port 2")
        variables = build_variables(line_card_bay)

        self.assertEqual(variables["bay_position"], "TenGigabitEthernet3/2")
        self.assertEqual(variables["bay_position_num"], "2")
        self.assertEqual(variables["parent_bay_position"], "3")
        self.assertEqual(variables["slot"], "3")
        self.assertEqual(variables["slot_num"], "3")

    def test_the_twingig_conversion_template_evaluates(self):
        """The Cisco TwinGig offset formula, which is why a numeric parent position is needed."""
        variables = build_variables(self.sfp_bay)

        name = evaluate_name_template(
            "GigabitEthernet{slot_num}/{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}",
            variables,
        )

        self.assertEqual(name, "GigabitEthernet3/11")
