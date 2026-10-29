# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""In a branch, a platform scope reads the platform tree of the branch, also when the rule models are exempt."""

from dcim.models import Device, Interface, Module, ModuleBay, Platform, VirtualChassis
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from netbox_interface_name_rules.engine import _build_module_qs, _device_interface_rules, find_matching_rule
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.branch_cases import BranchTestCase
from netbox_interface_name_rules.tests.helpers import (
    activate,
    make_device_type,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
    make_placement,
)

User = get_user_model()


class _BranchPlatformTreeTests:
    """A rule on platform JunOS, child EVO under it, both made on main before the branch."""

    EXEMPT = False

    def setUp(self):
        prefix = type(self).__name__[:24]
        manufacturer = make_manufacturer(prefix)
        self.module_type = make_module_type(manufacturer, prefix, model=f"{prefix}-QSFPP-4X10GE-LR")
        self.device_type = make_device_type(manufacturer, prefix)
        make_module_bay_templates(self.device_type, ("Bay 0",))
        self.placement = make_placement(prefix)
        self.junos = Platform.objects.create(name=f"{prefix} JunOS", slug=f"{prefix.lower()}-junos")
        self.evo = Platform.objects.create(name=f"{prefix} EVO", slug=f"{prefix.lower()}-evo", parent=self.junos)
        self.rule = InterfaceNameRule.objects.create(
            module_type=self.module_type, platform=self.junos, name_template="xe-{bay_position}"
        )
        self.device_rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True, platform=self.junos, name_template="junos{vc_position}/{port}"
        )
        self.upper = Platform.objects.create(name=f"{prefix} A", slug=f"{prefix.lower()}-a")
        self.lower = Platform.objects.create(name=f"{prefix} B", slug=f"{prefix.lower()}-b", parent=self.upper)
        self.on_upper = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True, platform=self.upper, name_template="a{vc_position}/{port}"
        )
        self.on_lower = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True, platform=self.lower, name_template="b{vc_position}/{port}"
        )
        user = User.objects.create_user(username=f"{prefix.lower()}-operator")
        self.branch = self.provision_branch(prefix, user)
        if self.EXEMPT:
            branching = {
                **settings.PLUGINS_CONFIG["netbox_branching"],
                "exempt_models": ["netbox_interface_name_rules.*"],
            }
            exempt = override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "netbox_branching": branching})
            exempt.enable()
            self.addCleanup(exempt.disable)

    def _select(self, platform):
        return find_matching_rule(self.module_type, None, self.device_type, platform)

    def _branch_only_child(self):
        """Create a child platform of JunOS in the branch; main does not have it."""
        return Platform.objects.create(name="Branch Child", slug="branch-child", parent=self.junos)

    def test_a_branch_only_child_platform_selects_the_parent_platform_rule(self):
        with activate(self.branch):
            child = self._branch_only_child()
            self.assertEqual(self._select(child), self.rule)

        self.assertFalse(Platform.objects.filter(pk=child.pk).exists())

    def test_a_platform_reparented_only_in_the_branch_changes_the_selection(self):
        self.assertEqual(self._select(self.evo), self.rule)

        with activate(self.branch):
            evo = Platform.objects.get(pk=self.evo.pk)
            evo.parent = None
            evo.save()
            self.assertIsNone(self._select(Platform.objects.get(pk=self.evo.pk)))

        self.assertEqual(self._select(Platform.objects.get(pk=self.evo.pk)), self.rule)

    def test_the_apply_scope_leaves_out_a_platform_moved_out_of_the_tree_in_the_branch(self):
        """In the branch EVO is no longer under JunOS; on main it still is."""
        with activate(self.branch):
            evo = Platform.objects.get(pk=self.evo.pk)
            evo.parent = None
            evo.save()
            device = Device.objects.create(
                name="branch-detached-host",
                device_type=self.device_type,
                role=self.placement.role,
                site=self.placement.site,
                platform=evo,
            )
            Module.objects.create(
                device=device, module_bay=ModuleBay.objects.get(device=device), module_type=self.module_type
            )
            rule = InterfaceNameRule.objects.get(pk=self.rule.pk)
            self.assertEqual(list(_build_module_qs(rule).values_list("pk", flat=True)), [])

    def _move_junos_under_a_branch_root(self):
        """In the branch, move JunOS under a new root, so the tree paths in the branch differ from main."""
        junos = Platform.objects.get(pk=self.junos.pk)
        junos.parent = Platform.objects.create(name="Branch Root", slug="branch-root")
        junos.save()

    def test_device_interface_rules_read_the_branch_tree(self):
        with activate(self.branch):
            self._move_junos_under_a_branch_root()
            device = Device.objects.create(
                name="branch-vc-member",
                device_type=self.device_type,
                role=self.placement.role,
                site=self.placement.site,
                platform=self.evo,
                virtual_chassis=VirtualChassis.objects.create(name="branch-vc"),
                vc_position=1,
            )
            self.assertEqual(_device_interface_rules(Device.objects.get(pk=device.pk)), [self.device_rule])

    def test_the_apply_scope_reads_the_branch_tree(self):
        with activate(self.branch):
            self._move_junos_under_a_branch_root()
            device = Device.objects.create(
                name="branch-module-host",
                device_type=self.device_type,
                role=self.placement.role,
                site=self.placement.site,
                platform=self.evo,
            )
            module = Module.objects.create(
                device=device, module_bay=ModuleBay.objects.get(device=device), module_type=self.module_type
            )
            Interface.objects.create(device=device, module=module, name="0", type="10gbase-x-sfpp")
            rule = InterfaceNameRule.objects.get(pk=self.rule.pk)
            self.assertEqual(list(_build_module_qs(rule).values_list("pk", flat=True)), [module.pk])
            self.assertEqual(self._select(Platform.objects.get(pk=self.evo.pk)), self.rule)

    def test_device_interface_rules_rank_the_closer_platform_in_the_branch_tree(self):
        """Main has A over B; the branch swaps them, so on a device on A the rule on A comes first."""
        with activate(self.branch):
            lower = Platform.objects.get(pk=self.lower.pk)
            lower.parent = None
            lower.save()
            upper = Platform.objects.get(pk=self.upper.pk)
            upper.parent = lower
            upper.save()
            device = Device.objects.create(
                name="branch-swap-member",
                device_type=self.device_type,
                role=self.placement.role,
                site=self.placement.site,
                platform=upper,
                virtual_chassis=VirtualChassis.objects.create(name="branch-swap-vc"),
                vc_position=1,
            )
            rules = _device_interface_rules(Device.objects.get(pk=device.pk))
            self.assertEqual(rules, [self.on_upper, self.on_lower])

    def test_the_apply_scope_ignores_a_rule_platform_loaded_with_the_rule(self):
        """A rule read with select_related("platform") carries the platform from the rule alias."""
        with activate(self.branch):
            self._move_junos_under_a_branch_root()
            device = Device.objects.create(
                name="branch-joined-host",
                device_type=self.device_type,
                role=self.placement.role,
                site=self.placement.site,
                platform=self.evo,
            )
            module = Module.objects.create(
                device=device, module_bay=ModuleBay.objects.get(device=device), module_type=self.module_type
            )
            rule = InterfaceNameRule.objects.select_related("platform").get(pk=self.rule.pk)
            self.assertEqual(list(_build_module_qs(rule).values_list("pk", flat=True)), [module.pk])


class BranchPlatformTreeTest(_BranchPlatformTreeTests, BranchTestCase):
    """The rule models are branched, so a branch holds both the rules and the tree."""


class ExemptRuleModelPlatformTreeTest(_BranchPlatformTreeTests, BranchTestCase):
    """The rule models are exempt: the rules come from main, the platform tree from the branch."""

    EXEMPT = True
