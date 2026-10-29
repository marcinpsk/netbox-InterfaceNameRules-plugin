# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Apply Rules acts only on the modules for which the engine selects the applied rule."""

from unittest import skipUnless

from dcim.models import Interface, Module, ModuleBay

from netbox_interface_name_rules.engine import (
    apply_rule_to_existing,
    convert_flat_families,
    find_convertible_families,
    find_interfaces_for_rule,
    has_applicable_interfaces,
    supports_channelization,
)
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import (
    CHANNELIZED,
    FLAT,
    REQUIRES_CHANNELIZATION,
    ChannelizationTestCase,
    build_device,
    make_device,
    make_device_type,
    make_module_bay_templates,
    plain_module_type,
)
from netbox_interface_name_rules.tests.platform_cases import PlatformTreeFixtures


def _previewed(rule, limit=None):
    """Return the previewed module pks of *rule*, and the count of what the preview examined."""
    results, total_checked = find_interfaces_for_rule(rule, limit=limit)
    return {entry["module"].pk for entry in results}, total_checked


class PlatformScopedApplyTest(PlatformTreeFixtures):
    """A broad rule leaves alone a module for which a rule on a closer platform wins."""

    def _name(self, module):
        return Interface.objects.get(module=module).name

    def test_a_rule_with_no_platform_skips_the_module_a_platform_rule_selects(self):
        broad = self._regex_rule("any-{bay_position}")
        self._regex_rule("evo-{bay_position}", platform=self.evo)
        on_build = self._module_on(self.evo_build, "plattree-build")
        on_eos = self._module_on(self.eos, "plattree-eos")

        self.assertEqual(_previewed(broad), ({on_eos.pk}, 1))
        apply_rule_to_existing(broad)

        self.assertEqual((self._name(on_build), self._name(on_eos)), ("et-0/0/0", "any-0"))

    def test_a_parent_platform_rule_skips_the_module_a_child_platform_rule_selects(self):
        on_junos = self._regex_rule("junos-{bay_position}", platform=self.junos)
        self._regex_rule("evo-{bay_position}", platform=self.evo)
        on_build = self._module_on(self.evo_build, "plattree-build")
        on_sibling = self._module_on(self.junos_sibling, "plattree-sibling")

        self.assertEqual(_previewed(on_junos), ({on_sibling.pk}, 1))
        apply_rule_to_existing(on_junos)

        self.assertEqual((self._name(on_build), self._name(on_sibling)), ("et-0/0/0", "junos-0"))

    def test_the_narrower_rule_still_renames_its_own_module(self):
        self._regex_rule("any-{bay_position}")
        on_evo = self._regex_rule("evo-{bay_position}", platform=self.evo)
        on_build = self._module_on(self.evo_build, "plattree-build")

        self.assertEqual(_previewed(on_evo), ({on_build.pk}, 1))
        apply_rule_to_existing(on_evo)

        self.assertEqual(self._name(on_build), "evo-0")

    def test_the_limit_and_the_remaining_count_leave_out_the_skipped_modules(self):
        broad = self._regex_rule("any-{bay_position}")
        self._regex_rule("evo-{bay_position}", platform=self.evo)
        self._module_on(self.evo_build, "plattree-build")
        self._module_on(self.eos, "plattree-eos")
        self._module_on(None, "plattree-none")

        previewed, total_checked = _previewed(broad, limit=1)

        self.assertEqual((len(previewed), total_checked), (1, 2))

    def test_a_rule_that_another_rule_outranks_everywhere_is_not_applicable(self):
        broad = self._regex_rule("any-{bay_position}")
        self._regex_rule("evo-{bay_position}", platform=self.evo)
        self._module_on(self.evo_build, "plattree-build")

        self.assertFalse(has_applicable_interfaces(broad))


class DeviceTypeScopedApplyTest(PlatformTreeFixtures):
    """A rule with no device type leaves alone a module for which a device-type rule wins."""

    def test_a_rule_with_no_device_type_skips_the_module_a_device_type_rule_selects(self):
        broad = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="any-{bay_position}")
        InterfaceNameRule.objects.create(
            module_type=self.module_type, device_type=self.device_type, name_template="dev-{bay_position}"
        )
        other_type = make_device_type(self.module_type.manufacturer, "PlatTreeOther")
        make_module_bay_templates(other_type, ("Bay 0",))
        on_typed = self._module_on(None, "plattree-typed")
        other = make_device("PlatTreeOther", other_type, self.placement, name="plattree-other")
        on_other = Module.objects.create(
            device=other, module_bay=ModuleBay.objects.get(device=other), module_type=self.module_type
        )
        Interface.objects.create(device=other, module=on_other, name="et-0/0/0", type="10gbase-x-sfpp")

        self.assertEqual(_previewed(broad), ({on_other.pk}, 1))
        apply_rule_to_existing(broad)

        names = (Interface.objects.get(module=on_typed).name, Interface.objects.get(module=on_other).name)
        self.assertEqual(names, ("et-0/0/0", "any-0"))


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ConversionOfAnOutrankedRuleTest(ChannelizationTestCase):
    """Neither the conversion preview nor the conversion takes a module for which another rule wins."""

    FLAT_NAMES = ("xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3")

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = build_device("ConvOutranked", ["3"])
        cls.module_type = plain_module_type(manufacturer, "ConvOutranked-QSFP")
        cls.breakout = {"name_template": "xe-0/0/{bay_position}:{channel}", "channel_count": 4, "channel_start": 0}
        cls.rule = InterfaceNameRule.objects.create(module_type=cls.module_type, breakout_mode=FLAT, **cls.breakout)

    def setUp(self):
        self.module, _ = self._install(self.module_type, "3")
        self.rule.breakout_mode = CHANNELIZED
        self.rule.parent_name_template = "et-0/0/{bay_position}"
        self.rule.save()
        InterfaceNameRule.objects.create(
            module_type=self.module_type, device_type=self.device.device_type, breakout_mode=FLAT, **self.breakout
        )

    def test_the_conversion_preview_skips_the_module(self):
        self.assertEqual(find_convertible_families(self.rule).candidates, ())

    def test_the_conversion_skips_the_module(self):
        self.assertEqual(len(convert_flat_families(self.rule).changed_families), 0)
        self.assertEqual(self._names(self.module), list(self.FLAT_NAMES))

    def test_the_rule_converts_the_module_again_once_the_narrower_rule_is_gone(self):
        InterfaceNameRule.objects.filter(device_type=self.device.device_type).delete()

        self.assertEqual(len(find_convertible_families(self.rule).candidates), 1)
