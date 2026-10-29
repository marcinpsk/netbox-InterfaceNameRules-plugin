# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""A rule's platform scope covers the child platforms of that platform, and the closest platform wins."""

from dcim.models import Interface, Module, ModuleBay, Platform, VirtualChassis
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from netbox_interface_name_rules.engine import (
    apply_device_interface_rules,
    apply_rule_to_existing,
    find_interfaces_for_rule,
    find_matching_rule,
)
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import (
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
    make_placement,
)

PATTERN = r"QSFPP-4X10GE-.*"


class PlatformTreeFixtures(TestCase):
    """A Junos platform tree, an unrelated platform, and a breakout module type."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = make_manufacturer("PlatTree")
        cls.module_type = make_module_type(manufacturer, "PlatTree", model="QSFPP-4X10GE-LR")
        cls.device_type = make_device_type(manufacturer, "PlatTree", model="PTX10008-PlatTree")
        make_module_bay_templates(cls.device_type, ("Bay 0",))
        cls.placement = make_placement("PlatTree")
        cls.junos = Platform.objects.create(name="PlatTree JunOS", slug="plattree-junos")
        cls.evo = Platform.objects.create(name="PlatTree JunOS 24.4R2-EVO", slug="plattree-evo", parent=cls.junos)
        cls.evo_build = Platform.objects.create(
            name="PlatTree JunOS 24.4R2-S5.4-EVO", slug="plattree-s54", parent=cls.evo
        )
        cls.junos_sibling = Platform.objects.create(name="PlatTree JunOS 23.4R1", slug="plattree-234", parent=cls.junos)
        cls.eos = Platform.objects.create(name="PlatTree EOS", slug="plattree-eos")

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


class ModuleRuleSelectionTest(PlatformTreeFixtures):
    """Module rule selection matches the device platform and each of its ancestors."""

    def test_a_parent_platform_rule_matches_a_child_platform_device(self):
        """The production case: a regex rule on JunOS, no device type, a device on a JunOS child platform."""
        rule = self._regex_rule("xe-{bay_position}", platform=self.junos)

        self.assertEqual(self._select(self.evo), rule)
        self.assertEqual(self._select(self.evo_build), rule)

    def test_the_closest_platform_wins(self):
        """A child platform rule beats the parent platform rule, which beats the rule with no platform."""
        unscoped = self._regex_rule("any{bay_position}")
        on_junos = self._regex_rule("junos{bay_position}", platform=self.junos)
        on_evo = self._regex_rule("evo{bay_position}", platform=self.evo)

        self.assertEqual(self._select(self.evo), on_evo)
        self.assertEqual(self._select(self.evo_build), on_evo)
        self.assertEqual(self._select(self.junos_sibling), on_junos)
        self.assertEqual(self._select(self.junos), on_junos)
        self.assertEqual(self._select(self.eos), unscoped)
        self.assertEqual(self._select(None), unscoped)

    def test_a_grandparent_platform_rule_loses_to_a_parent_platform_rule(self):
        """For a grandchild device, the rule on the parent platform wins over the rule on the grandparent."""
        self._regex_rule("junos{bay_position}", platform=self.junos)
        on_evo = self._regex_rule("evo{bay_position}", platform=self.evo)

        self.assertEqual(self._select(self.evo_build), on_evo)

    def test_a_sibling_child_or_unrelated_platform_rule_does_not_match(self):
        """Only the device platform and its ancestors match: not a sibling, a descendant or another tree."""
        self._regex_rule("sibling{bay_position}", platform=self.junos_sibling)
        self._regex_rule("eos{bay_position}", platform=self.eos)
        self._regex_rule("build{bay_position}", platform=self.evo_build)

        self.assertIsNone(self._select(self.evo))
        self.assertIsNone(self._select(self.junos))

    def test_device_type_scope_still_outranks_platform_closeness(self):
        """A device-type rule on the farther platform beats a rule with no device type on the closer platform."""
        device_and_junos = self._regex_rule("devjunos{bay_position}", device_type=self.device_type, platform=self.junos)
        self._regex_rule("evo{bay_position}", platform=self.evo)

        self.assertEqual(self._select(self.evo), device_and_junos)

    def test_an_exact_rule_on_an_ancestor_beats_a_regex_rule_on_the_device_platform(self):
        """The exact tier comes first, also when its platform is only an ancestor of the device platform."""
        exact = InterfaceNameRule.objects.create(
            module_type=self.module_type, platform=self.junos, name_template="exact{bay_position}"
        )
        self._regex_rule("evo{bay_position}", platform=self.evo)

        self.assertEqual(self._select(self.evo_build), exact)

    def test_a_repeated_selection_on_a_child_platform_runs_at_most_the_fingerprint_query(self):
        """Ancestor matching reads the platform tree with the rule snapshot, not once per selection."""
        rule = self._regex_rule("junos{bay_position}", platform=self.junos)
        self.assertEqual(self._select(self.evo_build), rule)

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self._select(self.evo_build), rule)

        self.assertLessEqual(len(queries), 1, [query["sql"] for query in queries.captured_queries])


class PlatformTreeCacheTest(PlatformTreeFixtures):
    """A platform tree edit reloads the rule snapshot, so a memoised selection does not stay stale."""

    def test_moving_a_platform_out_of_the_tree_drops_the_ancestor_rule(self):
        rule = self._regex_rule("junos{bay_position}", platform=self.junos)
        child = Platform.objects.create(name="PlatTree Moving", slug="plattree-moving", parent=self.junos)
        self.assertEqual(self._select(child), rule)

        child.parent = None
        child.save()

        self.assertIsNone(self._select(Platform.objects.get(pk=child.pk)))

    def test_moving_a_platform_into_the_tree_adds_the_ancestor_rule(self):
        rule = self._regex_rule("junos{bay_position}", platform=self.junos)
        newcomer = Platform.objects.create(name="PlatTree Newcomer", slug="plattree-newcomer")
        self.assertIsNone(self._select(newcomer))

        newcomer.parent = self.evo
        newcomer.save()

        self.assertEqual(self._select(Platform.objects.get(pk=newcomer.pk)), rule)


class DeviceInterfaceRuleScopeTest(PlatformTreeFixtures):
    """Device-interface rules match the device platform and each of its ancestors, the closest first."""

    def _vc_device(self, platform):
        device = make_device(
            "PlatTree",
            self.device_type,
            self.placement,
            name=f"plattree-vc-{platform.slug}",
            platform=platform,
            virtual_chassis=VirtualChassis.objects.create(name=f"plattree-vc-{platform.slug}"),
            vc_position=2,
        )
        interface = Interface.objects.create(device=device, name="Gi0/1", type="1000base-t")
        return device, interface

    def _device_rule(self, template, platform):
        return InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True, name_template=template, platform=platform
        )

    def test_a_parent_platform_device_rule_renames_a_child_platform_device(self):
        self._device_rule("junos{vc_position}/{port}", self.junos)
        device, interface = self._vc_device(self.evo_build)

        self.assertEqual(apply_device_interface_rules(device), 1)

        interface.refresh_from_db()
        self.assertEqual(interface.name, "junos2/1")

    def test_the_closest_platform_device_rule_wins(self):
        self._device_rule("junos{vc_position}/{port}", self.junos)
        self._device_rule("evo{vc_position}/{port}", self.evo)
        device, interface = self._vc_device(self.evo_build)

        apply_device_interface_rules(device)

        interface.refresh_from_db()
        self.assertEqual(interface.name, "evo2/1")

    def test_a_sibling_or_unrelated_platform_device_rule_does_not_match(self):
        self._device_rule("sibling{vc_position}/{port}", self.junos_sibling)
        self._device_rule("eos{vc_position}/{port}", self.eos)
        device, interface = self._vc_device(self.evo)

        self.assertEqual(apply_device_interface_rules(device), 0)

        interface.refresh_from_db()
        self.assertEqual(interface.name, "Gi0/1")


class ApplyScopeTest(PlatformTreeFixtures):
    """Preview and Apply Rules cover the modules on devices on the rule platform and on each descendant."""

    def test_a_parent_platform_rule_previews_and_applies_child_platform_modules(self):
        rule = self._regex_rule("xe-0/0/{bay_position}", platform=self.junos)
        on_build = self._module_on(self.evo_build, "plattree-build")
        on_junos = self._module_on(self.junos, "plattree-junos")
        on_eos = self._module_on(self.eos, "plattree-eos")
        on_sibling = self._module_on(self.junos_sibling, "plattree-sibling")

        results, _ = find_interfaces_for_rule(rule)

        previewed = {entry["module"].pk for entry in results}
        self.assertEqual(previewed, {on_build.pk, on_junos.pk, on_sibling.pk})

        apply_rule_to_existing(rule)

        self.assertEqual(Interface.objects.get(module=on_build).name, "xe-0/0/0")
        self.assertEqual(Interface.objects.get(module=on_eos).name, "et-0/0/0")

    def test_a_child_platform_rule_does_not_cover_a_parent_platform_module(self):
        rule = self._regex_rule("xe-0/0/{bay_position}", platform=self.evo)
        on_junos = self._module_on(self.junos, "plattree-junos")
        on_build = self._module_on(self.evo_build, "plattree-build")

        results, _ = find_interfaces_for_rule(rule)

        self.assertEqual({entry["module"].pk for entry in results}, {on_build.pk})
        self.assertNotIn(on_junos.pk, {entry["module"].pk for entry in results})


class RuleTesterPlatformScopeTest(PlatformTreeFixtures):
    """The rule tester previews the same modules that the engine applies the rule to."""

    def test_the_rule_tester_previews_child_platform_modules(self):
        user = get_user_model().objects.create_superuser(username="plattree-tester")
        self.client.force_login(user)
        on_build = self._module_on(self.evo_build, "plattree-build")
        on_eos = self._module_on(self.eos, "plattree-eos")

        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_test"),
            {
                "name_template": "xe-0/0/{bay_position}",
                "channel_count": "0",
                "channel_start": "0",
                "module_type_is_regex": "on",
                "module_type_pattern": PATTERN,
                "platform": str(self.junos.pk),
            },
        )

        self.assertEqual(response.status_code, 200)
        previewed = {entry["module"].pk for entry in response.context["db_preview"]}
        self.assertIn(on_build.pk, previewed)
        self.assertNotIn(on_eos.pk, previewed)
