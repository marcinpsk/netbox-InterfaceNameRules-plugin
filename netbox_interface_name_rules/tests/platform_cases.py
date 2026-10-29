# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The platform tree and the breakout module type that the platform-scope test modules share."""

from unittest import skipUnless

from dcim.models import Interface, Module, ModuleBay, Platform
from django.test import TestCase

from netbox_interface_name_rules.engine import find_matching_rule
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.rule_selection import platforms_nest
from netbox_interface_name_rules.tests.helpers import (
    REQUIRES_NESTED_PLATFORMS,
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
    make_placement,
)

PATTERN = r"QSFPP-4X10GE-.*"


class BreakoutModuleFixtures(TestCase):
    """A breakout module type and a device type with one bay, on every NetBox release."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = make_manufacturer("PlatTree")
        cls.module_type = make_module_type(manufacturer, "PlatTree", model="QSFPP-4X10GE-LR")
        cls.device_type = make_device_type(manufacturer, "PlatTree", model="PTX10008-PlatTree")
        make_module_bay_templates(cls.device_type, ("Bay 0",))
        cls.placement = make_placement("PlatTree")

    def _regex_rule(self, template, **scope):
        return InterfaceNameRule.objects.create(
            module_type_is_regex=True, module_type_pattern=PATTERN, name_template=template, **scope
        )

    def _select(self, platform, device_type=None):
        return find_matching_rule(self.module_type, None, device_type or self.device_type, platform)

    def _module_on(self, platform, name):
        """Install the breakout module on a new device on *platform*, with one raw interface."""
        device = make_device("PlatTree", self.device_type, self.placement, name=name, platform=platform)
        module = Module.objects.create(
            device=device, module_bay=ModuleBay.objects.get(device=device), module_type=self.module_type
        )
        Interface.objects.create(device=device, module=module, name="et-0/0/0", type="10gbase-x-sfpp")
        return module


@skipUnless(platforms_nest(), REQUIRES_NESTED_PLATFORMS)
class PlatformTreeFixtures(BreakoutModuleFixtures):
    """A Junos platform tree and an unrelated platform, on a NetBox that nests platforms."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.junos = Platform.objects.create(name="PlatTree JunOS", slug="plattree-junos")
        cls.evo = Platform.objects.create(name="PlatTree JunOS 24.4R2-EVO", slug="plattree-evo", parent=cls.junos)
        cls.evo_build = Platform.objects.create(
            name="PlatTree JunOS 24.4R2-S5.4-EVO", slug="plattree-s54", parent=cls.evo
        )
        cls.junos_sibling = Platform.objects.create(name="PlatTree JunOS 23.4R1", slug="plattree-234", parent=cls.junos)
        cls.eos = Platform.objects.create(name="PlatTree EOS", slug="plattree-eos")
