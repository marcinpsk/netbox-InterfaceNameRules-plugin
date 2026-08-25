# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for installed interface-family plans."""

from dataclasses import FrozenInstanceError, replace
from unittest import skipUnless
from unittest.mock import patch

from dcim.choices import InterfaceTypeChoices
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
    VirtualChassis,
)
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connection, connections
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    build_variables,
    supports_channelization,
    supports_vc_position_token,
)
from netbox_interface_name_rules.family import (
    FamilyStatus,
    FamilyTopology,
    execute_installed_plan_set,
    plan_installed_families,
)
from netbox_interface_name_rules.family import execution as family_execution
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.naming import evaluate_name_template

CHANNEL_TYPE = getattr(InterfaceTypeChoices, "TYPE_CHANNEL", "channel")
PARENT_TYPE = InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS
PLAIN_TYPE = InterfaceTypeChoices.TYPE_10GE_SFP_PLUS
REQUIRES_CHANNELIZATION = "requires a NetBox that models channelized interfaces"


class InstalledFlatFamilyPlanningTest(TestCase):
    """Plan installed flat breakout families from real NetBox rows."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="FamilyPlanMfg", slug="family-plan-mfg")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="FAMILY-PLAN-DEVICE",
            slug="family-plan-device",
        )
        ModuleBayTemplate.objects.create(device_type=device_type, name="Bay 7", position="7")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer,
            model="FAMILY-PLAN-QSFP",
            part_number="FAMILY-PLAN-QSFP",
        )
        InterfaceTemplate.objects.create(
            module_type=cls.module_type,
            name="{module}",
            type="100gbase-x-qsfp28",
        )
        role = DeviceRole.objects.create(name="FamilyPlanRole", slug="family-plan-role")
        site = Site.objects.create(name="FamilyPlanSite", slug="family-plan-site")
        cls.device = Device.objects.create(
            name="family-plan-device-01",
            device_type=device_type,
            role=role,
            site=site,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="Bay 7")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )

    def test_complete_flat_family_has_one_immutable_plan_with_every_snapshot(self):
        module = Module.objects.create(
            device=self.device,
            module_bay=self.bay,
            module_type=self.module_type,
        )
        self.assertEqual(apply_interface_name_rules(module, self.bay), 2)

        plan_set = plan_installed_families(
            module,
            self.rule,
            build_variables(self.bay, device=self.device),
        )

        self.assertEqual(len(plan_set.plans), 1)
        plan = plan_set.plans[0]
        self.assertEqual(plan.topology, FamilyTopology.FLAT)
        self.assertEqual(
            [member.snapshot.name for member in plan.members],
            ["xe-0/0/7:0", "xe-0/0/7:1"],
        )
        self.assertEqual(
            {member.snapshot.pk for member in plan.members},
            set(Interface.objects.filter(module=module).values_list("pk", flat=True)),
        )
        with self.assertRaises(FrozenInstanceError):
            plan.module_id = 0

    def _current_family_plan(self):
        """Install and plan one complete flat family without version-specific tokens."""
        module = Module.objects.create(
            device=self.device,
            module_bay=self.bay,
            module_type=self.module_type,
        )
        self.assertEqual(apply_interface_name_rules(module, self.bay), 2)
        plan_set = plan_installed_families(
            module,
            self.rule,
            build_variables(self.bay, device=self.device),
        )
        return module, plan_set

    @staticmethod
    def _retarget(plan_set, prefix):
        """Return *plan_set* with deterministic new member targets."""
        plans = tuple(
            replace(
                plan,
                members=tuple(
                    replace(member, target_name=f"{prefix}-{plan_index}-{member_index}")
                    for member_index, member in enumerate(plan.members)
                ),
            )
            for plan_index, plan in enumerate(plan_set.plans)
        )
        return replace(plan_set, plans=plans)

    def test_current_flat_family_execution_locks_and_renames_every_member(self):
        module, plan_set = self._current_family_plan()
        plan_set = self._retarget(plan_set, "renamed")

        with CaptureQueriesContext(connection) as queries:
            result = execute_installed_plan_set(plan_set)

        self.assertEqual(result.families[0].status, FamilyStatus.CHANGED)
        self.assertEqual(result.changed_count, 2)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["renamed-0-0", "renamed-0-1"],
        )
        lock_queries = [query["sql"] for query in queries.captured_queries if "FOR UPDATE" in query["sql"]]
        self.assertTrue(lock_queries)
        self.assertTrue(all("ORDER BY" in query for query in lock_queries))

    def test_current_flat_family_execution_propagates_unrelated_integrity_failure(self):
        module, plan_set = self._current_family_plan()
        plan_set = self._retarget(plan_set, "renamed")

        def reject_interface_update(execute, sql, params, many, context):
            if sql.lstrip().startswith('UPDATE "dcim_interface"'):
                raise IntegrityError("injected current-family database failure")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_interface_update):
            with self.assertRaisesMessage(IntegrityError, "injected current-family database failure"):
                execute_installed_plan_set(plan_set)

        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["xe-0/0/7:0", "xe-0/0/7:1"],
        )

    def test_current_flat_family_collision_blocks_each_occupied_target(self):
        module, plan_set = self._current_family_plan()
        plan_set = self._retarget(plan_set, "occupied")
        for member in plan_set.plans[0].members:
            Interface.objects.create(
                device=self.device,
                name=member.target_name,
                type=PLAIN_TYPE,
            )

        result = execute_installed_plan_set(plan_set)

        self.assertEqual(result.families[0].status, FamilyStatus.BLOCKED)
        self.assertEqual(
            {member.status for member in result.families[0].members},
            {FamilyStatus.BLOCKED},
        )
        self.assertEqual(result.changed_count, 0)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["xe-0/0/7:0", "xe-0/0/7:1"],
        )

    def test_failed_precondition_returns_member_facts_without_changes(self):
        module, plan_set = self._current_family_plan()
        plan = replace(
            plan_set.plans[0],
            precondition_status=FamilyStatus.FAILED,
            precondition_reason="failed to evaluate family targets",
        )

        result = execute_installed_plan_set(replace(plan_set, plans=(plan,)))

        self.assertEqual(result.families[0].status, FamilyStatus.FAILED)
        self.assertEqual(
            {member.status for member in result.families[0].members},
            {FamilyStatus.FAILED},
        )
        self.assertEqual(result.changed_count, 0)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["xe-0/0/7:0", "xe-0/0/7:1"],
        )

    def test_incomplete_current_flat_family_is_not_planned(self):
        module, plan_set = self._current_family_plan()
        Interface.objects.filter(pk=plan_set.plans[0].members[1].snapshot.pk).delete()

        replanned = plan_installed_families(
            module,
            self.rule,
            build_variables(self.bay, device=self.device),
        )

        self.assertEqual(replanned.plans, ())

    def test_simple_rule_has_no_flat_family_plan(self):
        module = Module.objects.create(
            device=self.device,
            module_bay=self.bay,
            module_type=self.module_type,
        )
        self.rule.channel_count = 0

        plan_set = plan_installed_families(
            module,
            self.rule,
            build_variables(self.bay, device=self.device),
        )

        self.assertEqual(plan_set.plans, ())

    def test_automatic_reapplication_propagates_planner_failures(self):
        module = Module.objects.create(
            device=self.device,
            module_bay=self.bay,
            module_type=self.module_type,
        )

        with (
            patch(
                "netbox_interface_name_rules.engine.family_ops.plan_installed_families",
                side_effect=TypeError("planner defect"),
            ),
            self.assertRaisesMessage(TypeError, "planner defect"),
        ):
            apply_interface_name_rules(module, self.bay, force_reapply=True)

    def test_deferred_reconciliation_propagates_unrelated_integrity_failures(self):
        interface = Interface.objects.create(
            device=self.device,
            name="cascade-name",
            type=PLAIN_TYPE,
        )

        def reject_interface_update(execute, sql, params, many, context):
            if sql.lstrip().startswith('UPDATE "dcim_interface"'):
                raise IntegrityError("injected deferred database failure")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_interface_update):
            with self.assertRaisesMessage(IntegrityError, "injected deferred database failure"):
                family_execution._restore_deferred_channel_names(
                    ((interface.pk, "final-name", "cascade-name"),),
                    interface._state.db,
                )

    def test_changed_member_makes_the_plan_stale_without_partial_execution(self):
        module = Module.objects.create(
            device=self.device,
            module_bay=self.bay,
            module_type=self.module_type,
        )
        apply_interface_name_rules(module, self.bay)
        plan_set = plan_installed_families(
            module,
            self.rule,
            build_variables(self.bay, device=self.device),
        )
        changed_pk = plan_set.plans[0].members[1].snapshot.pk
        Interface.objects.filter(pk=changed_pk).update(name="operator-change")

        result = execute_installed_plan_set(plan_set)

        self.assertEqual(result.families[0].status, FamilyStatus.STALE)
        self.assertEqual(
            {member.status for member in result.families[0].members},
            {FamilyStatus.STALE},
        )
        self.assertEqual(result.changed_count, 0)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["operator-change", "xe-0/0/7:0"],
        )

    def test_deleted_member_makes_the_complete_family_plan_stale(self):
        module = Module.objects.create(
            device=self.device,
            module_bay=self.bay,
            module_type=self.module_type,
        )
        apply_interface_name_rules(module, self.bay)
        plan_set = plan_installed_families(
            module,
            self.rule,
            build_variables(self.bay, device=self.device),
        )
        Interface.objects.filter(pk=plan_set.plans[0].members[1].snapshot.pk).delete()

        result = execute_installed_plan_set(plan_set)

        self.assertEqual(result.families[0].status, FamilyStatus.STALE)
        self.assertEqual(result.changed_count, 0)
        self.assertEqual(
            list(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["xe-0/0/7:0"],
        )

    @skipUnless(supports_vc_position_token(), "requires NetBox virtual-chassis position templates")
    def test_complete_historical_family_maps_to_current_raw_template_names(self):
        _module, plan_set = self._historical_family_plan()

        self.assertEqual(len(plan_set.plans), 1)
        self.assertEqual(
            [member.snapshot.name for member in plan_set.plans[0].members],
            ["brk-xe-1/0/7:0", "brk-xe-1/0/7:1"],
        )
        self.assertEqual(
            [member.target_name for member in plan_set.plans[0].members],
            ["brk-xe-2/0/7:0", "brk-xe-2/0/7:1"],
        )

    def _historical_family_plan(self):
        """Install a flat family at VC position 1 and plan it at position 2."""
        module, rule = self._historical_family_state()
        plan_set = plan_installed_families(
            module,
            rule,
            build_variables(module.module_bay, device=module.device),
        )
        return module, plan_set

    def _historical_family_state(self, name_template="brk-{base}:{channel}"):
        """Return a flat family installed at VC position 1 after moving to position 2."""
        virtual_chassis = VirtualChassis.objects.create(name="family-plan-vc")
        self.device.virtual_chassis = virtual_chassis
        self.device.vc_position = 1
        self.device.save()
        token_type = ModuleType.objects.create(
            manufacturer=self.module_type.manufacturer,
            model="FAMILY-PLAN-VC-QSFP",
            part_number="FAMILY-PLAN-VC-QSFP",
        )
        InterfaceTemplate.objects.create(
            module_type=token_type,
            name="xe-{vc_position:0}/0/{module}",
            type="100gbase-x-qsfp28",
        )
        rule = InterfaceNameRule.objects.create(
            module_type=token_type,
            name_template=name_template,
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=token_type)
        apply_interface_name_rules(module, self.bay)
        expected = [
            evaluate_name_template(name_template, {"base": "xe-1/0/7", "channel": str(channel)}) for channel in range(2)
        ]
        self.assertEqual(sorted(Interface.objects.filter(module=module).values_list("name", flat=True)), expected)
        self.device.vc_position = 2
        self.device.save()
        module = Module.objects.select_related("device", "module_bay").get(pk=module.pk)
        return module, rule

    @skipUnless(supports_vc_position_token(), "requires NetBox virtual-chassis position templates")
    def test_execution_locks_and_renames_the_complete_flat_family(self):
        module, plan_set = self._historical_family_plan()

        with CaptureQueriesContext(connection) as queries:
            result = execute_installed_plan_set(plan_set)

        self.assertEqual(result.families[0].status, FamilyStatus.CHANGED)
        self.assertEqual(
            [member.status for member in result.families[0].members],
            [FamilyStatus.CHANGED, FamilyStatus.CHANGED],
        )
        self.assertEqual(result.changed_count, 2)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["brk-xe-2/0/7:0", "brk-xe-2/0/7:1"],
        )
        lock_queries = [query["sql"] for query in queries.captured_queries if "FOR UPDATE" in query["sql"]]
        self.assertTrue(lock_queries)
        self.assertTrue(all("ORDER BY" in query for query in lock_queries))

    @skipUnless(supports_vc_position_token(), "requires NetBox virtual-chassis position templates")
    def test_unrelated_integrity_failure_propagates_and_rolls_back_the_family(self):
        module, plan_set = self._historical_family_plan()

        def reject_interface_update(execute, sql, params, many, context):
            if sql.lstrip().startswith('UPDATE "dcim_interface"'):
                raise IntegrityError("injected non-uniqueness database failure")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_interface_update):
            with self.assertRaisesMessage(IntegrityError, "injected non-uniqueness database failure"):
                execute_installed_plan_set(plan_set)

        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["brk-xe-1/0/7:0", "brk-xe-1/0/7:1"],
        )

    @skipUnless(supports_vc_position_token(), "requires NetBox virtual-chassis position templates")
    def test_automatic_reapplication_uses_the_installed_family_executor(self):
        module, _rule = self._historical_family_state("{base}:{channel}")

        with CaptureQueriesContext(connection) as queries:
            renamed = apply_interface_name_rules(module, module.module_bay, force_reapply=True)

        self.assertEqual(renamed, 2)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["xe-2/0/7:0", "xe-2/0/7:1"],
        )
        self.assertTrue(any("FOR UPDATE" in query["sql"] for query in queries.captured_queries))

    @skipUnless(supports_vc_position_token(), "requires NetBox virtual-chassis position templates")
    def test_forced_reapplication_preserves_a_wrapped_historical_family(self):
        module, _rule = self._historical_family_state()

        renamed = apply_interface_name_rules(module, module.module_bay, force_reapply=True)

        self.assertEqual(renamed, 0)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["brk-xe-1/0/7:0", "brk-xe-1/0/7:1"],
        )

    @skipUnless(
        supports_channelization() and supports_vc_position_token(),
        "requires NetBox channelization and virtual-chassis position templates",
    )
    def test_normal_reapplication_preserves_a_wrapped_historical_family(self):
        module, _rule = self._historical_family_state()

        renamed = apply_interface_name_rules(module, module.module_bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["brk-xe-1/0/7:0", "brk-xe-1/0/7:1"],
        )

    def _two_historical_family_state(self):
        """Return two complete historical flat families and their current rule context."""
        virtual_chassis = VirtualChassis.objects.create(name="family-plan-two-vc")
        self.device.virtual_chassis = virtual_chassis
        self.device.vc_position = 1
        self.device.save()
        module_type = ModuleType.objects.create(
            manufacturer=self.module_type.manufacturer,
            model="FAMILY-PLAN-TWO-QSFP",
            part_number="FAMILY-PLAN-TWO-QSFP",
        )
        for suffix in ("a", "b"):
            InterfaceTemplate.objects.create(
                module_type=module_type,
                name=f"xe-{{vc_position:0}}/0/{{module}}-{suffix}",
                type="100gbase-x-qsfp28",
            )
        rule = InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="brk-{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=module_type)
        self.assertEqual(apply_interface_name_rules(module, self.bay), 4)
        self.device.vc_position = 2
        self.device.save()
        module = Module.objects.select_related("device", "module_bay").get(pk=module.pk)
        return module, rule

    @skipUnless(supports_vc_position_token(), "requires NetBox virtual-chassis position templates")
    def test_incomplete_flat_family_is_rejected_without_hiding_a_complete_family(self):
        module, rule = self._two_historical_family_state()
        Interface.objects.filter(module=module, name="brk-xe-1/0/7-a:1").delete()

        plan_set = plan_installed_families(
            module,
            rule,
            build_variables(module.module_bay, device=module.device),
        )

        self.assertEqual(len(plan_set.plans), 1)
        self.assertEqual(
            [member.snapshot.name for member in plan_set.plans[0].members],
            ["brk-xe-1/0/7-b:0", "brk-xe-1/0/7-b:1"],
        )

    @skipUnless(supports_vc_position_token(), "requires NetBox virtual-chassis position templates")
    def test_blocked_member_does_not_stop_an_unrelated_flat_family(self):
        module, rule = self._two_historical_family_state()
        Interface.objects.create(
            device=self.device,
            name="brk-xe-2/0/7-a:1",
            type=PLAIN_TYPE,
        )
        plan_set = plan_installed_families(
            module,
            rule,
            build_variables(module.module_bay, device=module.device),
        )

        result = execute_installed_plan_set(plan_set)

        self.assertEqual(len(result.families), 2)
        self.assertEqual(result.changed_count, 3)
        self.assertEqual(
            [member.status for member in result.families[0].members],
            [FamilyStatus.CHANGED, FamilyStatus.BLOCKED],
        )
        self.assertEqual(
            [member.status for member in result.families[1].members],
            [FamilyStatus.CHANGED, FamilyStatus.CHANGED],
        )
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            [
                "brk-xe-1/0/7-a:1",
                "brk-xe-2/0/7-a:0",
                "brk-xe-2/0/7-b:0",
                "brk-xe-2/0/7-b:1",
            ],
        )

    @skipUnless(supports_vc_position_token(), "requires NetBox virtual-chassis position templates")
    def test_overlapping_historical_matchers_reject_every_ambiguous_flat_family(self):
        virtual_chassis = VirtualChassis.objects.create(name="family-plan-ambiguous-vc")
        self.device.virtual_chassis = virtual_chassis
        self.device.vc_position = 1
        self.device.save()
        module_type = ModuleType.objects.create(
            manufacturer=self.module_type.manufacturer,
            model="FAMILY-PLAN-AMBIGUOUS-QSFP",
            part_number="FAMILY-PLAN-AMBIGUOUS-QSFP",
        )
        for name in ("xe-{vc_position}/0/{module}", "xe-1/{vc_position}/{module}"):
            InterfaceTemplate.objects.create(
                module_type=module_type,
                name=name,
                type="100gbase-x-qsfp28",
            )
        rule = InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="brk-{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=module_type)
        self.assertEqual(apply_interface_name_rules(module, self.bay), 4)
        self.device.vc_position = 2
        self.device.save()
        module = Module.objects.select_related("device", "module_bay").get(pk=module.pk)

        plan_set = plan_installed_families(
            module,
            rule,
            build_variables(module.module_bay, device=module.device),
        )

        self.assertEqual(plan_set.plans, ())


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class InstalledChannelizedFamilyTest(TestCase):
    """Plan and execute existing channelized families through real NetBox rows."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="InstalledChanMfg", slug="installed-chan-mfg")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model="INSTALLED-CHAN-DEVICE",
            slug="installed-chan-device",
        )
        for position in ("3", "4", "5", "6"):
            ModuleBayTemplate.objects.create(
                device_type=device_type,
                name=f"Bay {position}",
                position=position,
            )
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer,
            model="INSTALLED-CHAN-QSFP",
            part_number="INSTALLED-CHAN-QSFP",
        )
        parent = InterfaceTemplate.objects.create(
            module_type=cls.module_type,
            name="{module}",
            type=PARENT_TYPE,
            channels=4,
        )
        for channel_id in (1, 3):
            InterfaceTemplate.objects.create(
                module_type=cls.module_type,
                name=f"{{module}}:{channel_id}",
                type=CHANNEL_TYPE,
                parent=parent,
                channel_id=channel_id,
            )
        role = DeviceRole.objects.create(name="InstalledChanRole", slug="installed-chan-role")
        site = Site.objects.create(name="InstalledChanSite", slug="installed-chan-site")
        cls.device = Device.objects.create(
            name="installed-chan-device-01",
            device_type=device_type,
            role=role,
            site=site,
        )
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )

    def _raw_module(self, position):
        """Install one module without executing the plugin's committed callback."""
        bay = ModuleBay.objects.get(device=self.device, name=f"Bay {position}")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        return module, bay

    def _plan(self, module, bay):
        """Plan the installed family on *module*."""
        return plan_installed_families(module, self.rule, build_variables(bay, device=self.device))

    def test_incomplete_channelized_family_is_one_plan_with_only_installed_members(self):
        module, bay = self._raw_module("3")

        plan_set = self._plan(module, bay)

        self.assertEqual(len(plan_set.plans), 1)
        plan = plan_set.plans[0]
        self.assertEqual(plan.topology, FamilyTopology.CHANNELIZED)
        self.assertEqual(
            [(member.role.value, member.snapshot.channel_id) for member in plan.members],
            [("parent", None), ("channel", 1), ("channel", 3)],
        )
        self.assertEqual(
            [member.target_name for member in plan.members],
            ["3", "xe-0/0/3:0", "xe-0/0/3:2"],
        )

    def test_automatic_installation_uses_locked_family_execution(self):
        bay = ModuleBay.objects.get(device=self.device, name="Bay 4")

        with CaptureQueriesContext(connection) as queries, self.captureOnCommitCallbacks(execute=True):
            module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)

        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["4", "xe-0/0/4:0", "xe-0/0/4:2"],
        )
        self.assertTrue(any("FOR UPDATE" in query["sql"] for query in queries.captured_queries))

    def test_blocked_parent_prevents_every_child_rename(self):
        self.rule.breakout_mode = BreakoutModeChoices.CHANNELIZED
        self.rule.parent_name_template = "et-0/0/{bay_position}"
        self.rule.save()
        Interface.objects.create(device=self.device, name="et-0/0/5", type=PLAIN_TYPE)
        module, bay = self._raw_module("5")

        result = execute_installed_plan_set(self._plan(module, bay))

        self.assertEqual(result.families[0].status, FamilyStatus.BLOCKED)
        self.assertEqual(
            {member.status for member in result.families[0].members},
            {FamilyStatus.BLOCKED},
        )
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["5", "5:1", "5:3"],
        )

    def test_blocked_child_leaves_only_that_child_unchanged(self):
        Interface.objects.create(device=self.device, name="xe-0/0/6:2", type=PLAIN_TYPE)
        module, bay = self._raw_module("6")

        result = execute_installed_plan_set(self._plan(module, bay))

        self.assertEqual(result.families[0].status, FamilyStatus.CHANGED)
        self.assertEqual(
            [member.status for member in result.families[0].members],
            [FamilyStatus.UNCHANGED, FamilyStatus.CHANGED, FamilyStatus.BLOCKED],
        )
        self.assertEqual(
            sorted(Interface.objects.filter(module=module).values_list("name", flat=True)),
            ["6", "6:3", "xe-0/0/6:0"],
        )


class InstalledFamilyDatabaseAliasTest(TestCase):
    """Automatic renaming reads leftover interfaces from the alias the module row came from.

    A second alias onto the same test database gets its own connection, so it cannot see the rows
    this test created inside its own open transaction. Binding the module to that alias therefore
    leaves the leftover query with nothing to rename, unless the query still runs on the default
    connection, which is the split the review found.
    """

    ALIAS = "family_alias"

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="AliasMfg", slug="alias-mfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ALIAS-DEV", slug="alias-dev")
        ModuleBayTemplate.objects.create(device_type=device_type, name="Bay 7", position="7")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer,
            model="ALIAS-QSFP",
            part_number="ALIAS-QSFP",
        )
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="{module}", type="100gbase-x-qsfp28")
        role = DeviceRole.objects.create(name="AliasRole", slug="alias-role")
        site = Site.objects.create(name="AliasSite", slug="alias-site")
        cls.device = Device.objects.create(
            name="alias-device-01",
            device_type=device_type,
            role=role,
            site=site,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="Bay 7")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )

    def _use_second_alias(self):
        """Point a second alias at the live test database and let this test reach it."""
        connections.settings[self.ALIAS] = dict(connections[DEFAULT_DB_ALIAS].settings_dict)
        declared = type(self).databases
        type(self).databases = frozenset({*declared, self.ALIAS})
        self.addCleanup(connections.settings.pop, self.ALIAS, None)
        self.addCleanup(connections[self.ALIAS].close)
        self.addCleanup(setattr, type(self), "databases", declared)
        return self.ALIAS

    @staticmethod
    def _names(module):
        return sorted(Interface.objects.filter(module=module).values_list("name", flat=True))

    def test_leftover_interfaces_are_read_from_the_module_alias(self):
        module = Module.objects.create(
            device=self.device,
            module_bay=self.bay,
            module_type=self.module_type,
        )
        module._state.db = self._use_second_alias()

        with CaptureQueriesContext(connections[self.ALIAS]) as aliased:
            renamed = apply_interface_name_rules(module, self.bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["7"])
        self.assertTrue([query for query in aliased.captured_queries if 'FROM "dcim_interface"' in query["sql"]])

    def test_the_same_module_on_the_default_alias_breaks_out_the_family(self):
        """The control: only the alias makes the leftover pass find no interface to rename."""
        module = Module.objects.create(
            device=self.device,
            module_bay=self.bay,
            module_type=self.module_type,
        )

        self.assertEqual(apply_interface_name_rules(module, self.bay), 2)
        self.assertEqual(self._names(module), ["xe-0/0/7:0", "xe-0/0/7:1"])
