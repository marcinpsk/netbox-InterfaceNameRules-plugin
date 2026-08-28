# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for structural (channelized) interface-family plans.

A structural plan turns one plain port into the topology NetBox models: the base row becomes the
physical parent and N channel subinterfaces are created under it.  The whole family is one unit —
it is installed completely or not at all — and the family module, not the engine, decides whether
the active NetBox release can hold it.
"""

from unittest import skipIf, skipUnless

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.db import IntegrityError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    build_variables,
    supports_channelization,
)
from netbox_interface_name_rules.family import (
    FamilyStatus,
    FamilyTopology,
    execute_structural_family,
    install_channelized_family,
    plan_structural_family,
)
from netbox_interface_name_rules.family import names as family_names
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_breakout_mode import CHANNELIZED, _plain_module_type
from netbox_interface_name_rules.tests.test_channelization import (
    PLAIN_TYPE,
    PLUGIN_LOGGER,
    REQUIRES_CHANNELIZATION,
    ChannelizationTestCase,
    _build_device,
)

REQUIRES_NO_CHANNELIZATION = "requires a NetBox that cannot model channelized interfaces (4.6 and older)"


class StructuralFamilyTestCase(ChannelizationTestCase):
    """One device, one plain module type, and the channelized rule under test."""

    NAME_TEMPLATE = "xe-0/0/{bay_position}:{channel}"
    PREFIX = "Struct"

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(cls.PREFIX, ["3", "4"])
        cls.module_type = _plain_module_type(manufacturer, f"{cls.PREFIX}-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template=cls.NAME_TEMPLATE,
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def _plan(self, position="3"):
        """Install a raw-named module and return its module, bay and structural plan."""
        module, bay = self._install(self.module_type, position, run_rules=False)
        base = Interface.objects.get(module=module)
        plan = plan_structural_family(module, self.rule, build_variables(bay, device=self.device), base)
        return module, bay, plan


@skipIf(supports_channelization(), REQUIRES_NO_CHANNELIZATION)
class StructuralFamilyWithoutChannelizationTest(StructuralFamilyTestCase):
    """Where NetBox has no channel model the family module says so, in the outcome itself."""

    PREFIX = "StructNoSup"

    def test_the_plan_reports_unsupported_and_describes_no_channels(self):
        _module, _bay, plan = self._plan()

        self.assertEqual(plan.precondition_status, FamilyStatus.UNSUPPORTED)
        self.assertEqual(plan.topology, FamilyTopology.CHANNELIZED)
        self.assertEqual(plan.channels, ())
        self.assertEqual(plan.channel_count, 0)
        self.assertEqual(plan.target_names, ("3",))
        self.assertEqual(plan.base.name, "3")

    def test_executing_an_unsupported_plan_creates_nothing(self):
        module, _bay, plan = self._plan()

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            outcome = execute_structural_family(plan)

        self.assertEqual(outcome.status, FamilyStatus.UNSUPPORTED)
        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(self._names(module), ["3"])
        self.assertEqual(len(logs.records), 1, logs.output)
        self.assertIn("channelized", logs.output[0].lower())

    def test_the_outcome_names_the_base_interface_it_left_alone(self):
        module, _bay, plan = self._plan()
        base = Interface.objects.get(module=module)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING"):
            outcome = execute_structural_family(plan)

        self.assertEqual(len(outcome.members), 1)
        member = outcome.members[0]
        self.assertEqual(member.interface_pk, base.pk)
        self.assertEqual(member.current_name, "3")
        self.assertEqual(member.status, FamilyStatus.UNSUPPORTED)
        self.assertTrue(member.reason)

    def test_the_install_entry_point_reaches_the_same_outcome(self):
        module, bay = self._install(self.module_type, "4", run_rules=False)
        base = Interface.objects.get(module=module)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING"):
            outcome = install_channelized_family(module, self.rule, build_variables(bay, device=self.device), base)

        self.assertEqual(outcome.status, FamilyStatus.UNSUPPORTED)
        self.assertEqual(self._names(module), ["4"])


class DeferredChannelNameReconciliationTest(TestCase):
    """The family module restores the channel names NetBox's deferred parent cascade overwrites.

    The reconciliation is plain name arithmetic over committed rows, so it is exercised here on
    ordinary interfaces: no release-specific channel model is involved in the decision it makes.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ReconMfg", slug="recon-mfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="RECON-DEVICE", slug="recon-device")
        role = DeviceRole.objects.create(name="ReconRole", slug="recon-role")
        site = Site.objects.create(name="ReconSite", slug="recon-site")
        cls.device = Device.objects.create(name="recon-device-01", device_type=device_type, role=role, site=site)

    def _interface(self, name):
        """Create one plain interface on the shared device."""
        return Interface.objects.create(device=self.device, name=name, type=PLAIN_TYPE)

    @staticmethod
    def _cascade(child, name):
        """Rename *child* the way NetBox's parent cascade does, without running the plugin."""
        rename_out_of_band(child, name)

    def test_the_intended_name_is_restored_after_the_cascade(self):
        child = self._interface("et-0/0/3:1")

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            family_names.reconcile_after_parent_cascade(
                "et-0/0/3", "xe-0/0/3", ((child.pk, 1, "et-0/0/3:1"),), child._state.db
            )
            self._cascade(child, "xe-0/0/3:1")

        self.assertTrue(callbacks, "no deferred reconciliation was registered")
        child.refresh_from_db()
        self.assertEqual(child.name, "et-0/0/3:1")

    def test_a_parent_that_kept_its_name_registers_no_callback(self):
        child = self._interface("et-0/0/3:1")

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            family_names.reconcile_after_parent_cascade(
                "et-0/0/3", "et-0/0/3", ((child.pk, 1, "et-0/0/3:1"),), child._state.db
            )

        self.assertEqual(callbacks, [])

    def test_a_channel_the_cascade_will_not_touch_registers_no_callback(self):
        child = self._interface("ge-0/0/3-1")

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            family_names.reconcile_after_parent_cascade(
                "et-0/0/3", "xe-0/0/3", ((child.pk, 1, "ge-0/0/3-1"),), child._state.db
            )

        self.assertEqual(callbacks, [])

    def test_a_channel_the_cascade_left_alone_keeps_its_intended_name(self):
        child = self._interface("et-0/0/3:1")

        with self.captureOnCommitCallbacks(execute=True):
            family_names.reconcile_after_parent_cascade(
                "et-0/0/3", "xe-0/0/3", ((child.pk, 1, "et-0/0/3:1"),), child._state.db
            )

        child.refresh_from_db()
        self.assertEqual(child.name, "et-0/0/3:1")

    def test_a_channel_moved_to_an_unexpected_name_is_left_alone(self):
        child = self._interface("et-0/0/3:1")

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                family_names.reconcile_after_parent_cascade(
                    "et-0/0/3", "xe-0/0/3", ((child.pk, 1, "et-0/0/3:1"),), child._state.db
                )
                self._cascade(child, "someone-else-renamed-it")

        child.refresh_from_db()
        self.assertEqual(child.name, "someone-else-renamed-it")
        self.assertTrue(any("unexpected name" in line for line in logs.output), logs.output)

    def test_a_name_taken_since_the_cascade_is_not_reclaimed(self):
        child = self._interface("et-0/0/3:1")

        with self.assertLogs(PLUGIN_LOGGER, level="ERROR"), self.captureOnCommitCallbacks(execute=True):
            family_names.reconcile_after_parent_cascade(
                "et-0/0/3", "xe-0/0/3", ((child.pk, 1, "et-0/0/3:1"),), child._state.db
            )
            self._cascade(child, "xe-0/0/3:1")
            occupant = self._interface("et-0/0/3:1")

        child.refresh_from_db()
        occupant.refresh_from_db()
        self.assertEqual(child.name, "xe-0/0/3:1")
        self.assertEqual(occupant.name, "et-0/0/3:1")

    def test_the_reconciliation_locks_only_interfaces_in_primary_key_order(self):
        """A joined device row must not be locked, and overlapping runs must agree on lock order."""
        children = [self._interface("et-0/0/3:1"), self._interface("et-0/0/3:2")]
        reconciliations = tuple(
            (child.pk, child.name, f"xe-0/0/3:{index}") for index, child in enumerate(children, start=1)
        )

        with CaptureQueriesContext(connection) as queries:
            family_names.restore_deferred_channel_names(reconciliations, children[0]._state.db)

        locking = [query["sql"] for query in queries.captured_queries if "FOR UPDATE" in query["sql"]]
        self.assertEqual(len(locking), 1, locking)
        self.assertIn('FOR UPDATE OF "dcim_interface"', locking[0])
        self.assertNotIn("dcim_device", locking[0].split("FOR UPDATE")[1])
        self.assertIn('ORDER BY "dcim_interface"."id" ASC', locking[0])

    def test_an_unrelated_integrity_failure_propagates(self):
        interface = self._interface("cascade-name")

        def reject_interface_update(execute, sql, params, many, context):
            if sql.lstrip().startswith('UPDATE "dcim_interface"'):
                raise IntegrityError("injected deferred database failure")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_interface_update):
            with self.assertRaisesMessage(IntegrityError, "injected deferred database failure"):
                family_names.restore_deferred_channel_names(
                    ((interface.pk, "final-name", "cascade-name"),),
                    interface._state.db,
                )


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class StructuralFamilyPlanTest(StructuralFamilyTestCase):
    """The plan describes the whole family before a single row is written."""

    PREFIX = "StructPlan"

    def test_the_plan_describes_the_parent_capacity_channels_and_snapshot(self):
        module, _bay, plan = self._plan()
        base = Interface.objects.get(module=module)

        self.assertIsNone(plan.precondition_status)
        self.assertEqual(plan.topology, FamilyTopology.CHANNELIZED)
        self.assertEqual(plan.module_id, module.pk)
        self.assertEqual(plan.device_id, self.device.pk)
        self.assertEqual(plan.parent_target_name, "et-0/0/3")
        self.assertEqual(plan.channel_count, 4)
        self.assertEqual([channel.channel_id for channel in plan.channels], [1, 2, 3, 4])
        self.assertEqual(
            plan.target_names,
            ("et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"),
        )
        self.assertEqual(plan.base.pk, base.pk)
        self.assertEqual(plan.base.name, "3")
        self.assertIsNone(plan.base.channels)

    def test_planning_writes_nothing(self):
        module, _bay, _plan = self._plan()

        self.assertEqual(self._names(module), ["3"])

    def test_execution_creates_the_complete_family(self):
        module, _bay, plan = self._plan()

        outcome = execute_structural_family(plan)

        self.assertEqual(outcome.status, FamilyStatus.CHANGED)
        self.assertEqual(outcome.changed_count, 5)
        self.assertEqual(sorted(self._names(module)), sorted(plan.target_names))
        parent = self._parent(module)
        self.assertEqual(parent.pk, plan.base.pk)
        self.assertEqual(parent.channels, 4)
        self.assertEqual(
            sorted(
                Interface.objects.filter(parent=parent).values_list("channel_id", "name"),
            ),
            [(1, "xe-0/0/3:0"), (2, "xe-0/0/3:1"), (3, "xe-0/0/3:2"), (4, "xe-0/0/3:3")],
        )

    def test_a_base_that_changed_after_planning_is_stale_and_untouched(self):
        module, _bay, plan = self._plan()
        rename_out_of_band(Interface.objects.get(pk=plan.base.pk), "renamed-by-someone-else")

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            outcome = execute_structural_family(plan)

        self.assertEqual(outcome.status, FamilyStatus.STALE)
        self.assertEqual(self._names(module), ["renamed-by-someone-else"])
        self.assertTrue(any("changed after planning" in line for line in logs.output), logs.output)

    def test_a_sibling_added_after_planning_is_stale(self):
        """A row added after planning would be stranded beside the family, so the plan is refused."""
        module, _bay, plan = self._plan()
        Interface.objects.create(device=self.device, module=module, name="added-after-planning", type=PLAIN_TYPE)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            outcome = execute_structural_family(plan)

        self.assertEqual(outcome.status, FamilyStatus.STALE)
        self.assertEqual(self._names(module), ["3", "added-after-planning"])
        self.assertTrue(any("module's interfaces changed" in line for line in logs.output), logs.output)

    def test_a_base_deleted_after_planning_is_stale(self):
        module, _bay, plan = self._plan()
        Interface.objects.filter(pk=plan.base.pk).delete()

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING"):
            outcome = execute_structural_family(plan)

        self.assertEqual(outcome.status, FamilyStatus.STALE)
        self.assertEqual(self._names(module), [])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class StructuralFamilyRollbackTest(StructuralFamilyTestCase):
    """A family that cannot be completed leaves the port exactly as NetBox instantiated it."""

    PREFIX = "StructRoll"
    # Every channel resolves to one name, so the second insert hits NetBox's own uniqueness index.
    NAME_TEMPLATE = "xe-0/0/{bay_position}"

    def _assert_untouched(self, module):
        """Assert the port is still one plain, raw-named row with no channel anywhere on it."""
        self.assertEqual(Interface.objects.filter(module=module).count(), 1)
        base = Interface.objects.get(module=module)
        self.assertEqual(base.name, "3")
        self.assertIsNone(base.channels)

    def test_a_second_channel_with_the_same_name_rolls_the_family_back(self):
        module, _bay, plan = self._plan()

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            outcome = execute_structural_family(plan)

        self.assertEqual(outcome.status, FamilyStatus.BLOCKED)
        self.assertEqual(outcome.changed_count, 0)
        self.assertTrue(outcome.reason)
        self._assert_untouched(module)
        self.assertTrue(any("already exists" in line for line in logs.output), logs.output)

    def test_the_install_entry_point_reports_the_rollback(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING"):
            built = apply_interface_name_rules(module, bay)

        self.assertEqual(built, 0)
        self._assert_untouched(module)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class StructuralFamilyValidationTest(StructuralFamilyTestCase):
    """NetBox's own validation runs against the real rows, inside the structural transaction."""

    PREFIX = "StructVal"
    # 64 characters is NetBox's interface-name limit, so every channel name here is rejected.
    NAME_TEMPLATE = "xe-0/0/{bay_position}:{channel}" + "x" * 64

    def test_a_channel_netbox_rejects_rolls_the_whole_family_back(self):
        module, _bay, plan = self._plan()

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            outcome = execute_structural_family(plan)

        self.assertEqual(outcome.status, FamilyStatus.BLOCKED)
        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(Interface.objects.filter(module=module).count(), 1)
        base = Interface.objects.get(module=module)
        self.assertEqual(base.name, "3")
        self.assertIsNone(base.channels)
        self.assertTrue(any("64 characters" in line for line in logs.output), logs.output)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class StructuralFamilyTemplateFailureTest(StructuralFamilyTestCase):
    """A rule whose channel template cannot be evaluated builds nothing and says why."""

    PREFIX = "StructTpl"
    NAME_TEMPLATE = "xe-0/0/{bay_position}:{missing_variable}"

    def test_the_plan_carries_the_template_failure_instead_of_raising(self):
        _module, _bay, plan = self._plan()

        self.assertEqual(plan.precondition_status, FamilyStatus.FAILED)
        self.assertEqual(plan.channels, ())
        self.assertIn("missing_variable", plan.precondition_reason)

    def test_the_install_creates_nothing_and_does_not_read_the_rule_as_obsolete(self):
        module, bay = self._install(self.module_type, "4", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            built = apply_interface_name_rules(module, bay)

        self.assertEqual(built, 0)
        self.assertEqual(self._names(module), ["4"])
        self.assertFalse(self.rule.tags.filter(slug="potentially-deprecated").exists())
        self.assertTrue(any("missing_variable" in line for line in logs.output), logs.output)
