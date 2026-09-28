# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Rename triggers, driven through real module and device saves and asserted in the database.

A rename trigger reads the previous state before the save, compares it with the saved row, and
reapplies the rules after commit. Each test saves through NetBox's own models or REST API, runs the
committed callbacks, and reads the interface names back.
"""

import gc
import re
from contextlib import contextmanager
from unittest import skipUnless
from unittest.mock import patch

from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay, VirtualChassis
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, DataError, IntegrityError, connection, transaction
from django.db.models.signals import post_save
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from extras.choices import JournalEntryKindChoices
from extras.models import JournalEntry
from rest_framework import status
from utilities.testing import APITestCase

from netbox_interface_name_rules import engine, rename_triggers
from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.engine import supports_channelization
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import (
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
)
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_channelization import (
    PARENT_TYPE,
    REQUIRES_CHANNELIZATION,
    _channelized_module_type,
)

PLUGIN_LOGGER = "netbox_interface_name_rules"
PLAIN_TYPE = "10gbase-x-sfpp"
MODULE_STATE_READ = re.compile(
    r'SELECT "dcim_module"\."module_type_id"(?: AS "module_type_id")?, '
    r'"dcim_module"\."module_bay_id"(?: AS "module_bay_id")?, '
    r'"dcim_module"\."device_id"(?: AS "device_id")? FROM "dcim_module"'
)
DEVICE_STATE_READ = re.compile(
    r'SELECT "dcim_device"\."virtual_chassis_id"(?: AS "virtual_chassis_id")?, '
    r'"dcim_device"\."vc_position"(?: AS "vc_position")? FROM "dcim_device"'
)


@contextmanager
def _counting(entry_point):
    """Count the calls to one engine entry point; each call still runs the real function."""
    with patch.object(engine, entry_point, wraps=getattr(engine, entry_point)) as spy:
        yield spy


def _module_reapplies():
    return _counting("module_rule_outcomes")


def _device_reapplies():
    return _counting("device_module_rule_outcomes")


def _journal(instance):
    """Return the journal entries on *instance*, oldest first."""
    return list(
        JournalEntry.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(instance), assigned_object_id=instance.pk
        ).order_by("pk")
    )


def _give_the_next_module_id(pk):
    """Make the database assign *pk* to the next module that NetBox creates."""
    # NetBox before 4.7 creates no components for a module saved with an explicit pk.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence(%s, %s), %s, false)",
            [Module._meta.db_table, Module._meta.pk.column, pk],
        )


class _RenameTriggerFixture:
    """One device at virtual-chassis position 1 and three module types, each with its own rule.

    Type A names its interface `{module}` and type B `port{module}`, so an interface NetBox created
    for type A is not a raw name of type B: only a forced reapply renames it after a type change.
    """

    @classmethod
    def build(cls, prefix):
        """Create the fixture objects and return them as class attributes of *cls*."""
        manufacturer = make_manufacturer(prefix)
        device_type = make_device_type(manufacturer, prefix)
        make_module_bay_templates(device_type, ("Bay 0", "Bay 1"))
        cls.virtual_chassis = VirtualChassis.objects.create(name=f"{prefix} VC")
        cls.device = make_device(prefix, device_type, virtual_chassis=cls.virtual_chassis, vc_position=1)
        cls.type_a = cls._module_type(manufacturer, f"{prefix} A", "{module}", "et")
        cls.type_b = cls._module_type(manufacturer, f"{prefix} B", "port{module}", "xe")
        cls.type_c = cls._module_type(manufacturer, f"{prefix} C", "{module}", "ge")

    @staticmethod
    def _module_type(manufacturer, model, template, name_prefix):
        module_type = make_module_type(manufacturer, model, model=model)
        InterfaceTemplate.objects.create(module_type=module_type, name=template, type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            module_type=module_type, name_template=f"{name_prefix}-{{vc_position}}/0/{{bay_position}}"
        )
        return module_type

    def _bay(self, name="Bay 0"):
        return ModuleBay.objects.get(device=self.device, name=name)

    @staticmethod
    def _names(module):
        return sorted(Interface.objects.filter(module=module).values_list("name", flat=True))


class RenameTriggerTestCase(_RenameTriggerFixture, TestCase):
    """Shared fixture for rename triggers inside the test transaction."""

    prefix = "RenTrig"

    @classmethod
    def setUpTestData(cls):
        cls.build(cls.prefix)

    def _install(self, module_type=None):
        """Install a module in Bay 0 and run its install reapply."""
        with self.captureOnCommitCallbacks(execute=True):
            return Module.objects.create(
                device=self.device, module_bay=self._bay(), module_type=module_type or self.type_a
            )

    @staticmethod
    def _change_type(module, module_type):
        module.module_type = module_type
        module.save()

    def _move_to_position(self, position):
        self.device.vc_position = position
        self.device.save()


class RenameTriggerTest(RenameTriggerTestCase):
    """The three rename triggers rename as they did before the lifecycle module."""

    def test_installing_a_module_renames_its_interfaces(self):
        module = self._install()

        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_changing_the_module_type_reapplies_with_the_new_rule(self):
        module = self._install()

        with self.captureOnCommitCallbacks(execute=True):
            self._change_type(module, self.type_b)

        self.assertEqual(self._names(module), ["xe-1/0/0"])

    def test_changing_the_virtual_chassis_position_renames_the_module_interfaces(self):
        module = self._install()

        with self.captureOnCommitCallbacks(execute=True):
            self._move_to_position(2)

        self.assertEqual(self._names(module), ["et-2/0/0"])

    def test_joining_a_virtual_chassis_renames_the_device_interfaces(self):
        standalone = make_device(f"{self.prefix} Solo", self.device.device_type)
        interface = Interface.objects.create(device=standalone, name="mgmt0", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            name_template="mgmt-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="mgmt0"
        )

        with self.captureOnCommitCallbacks(execute=True):
            standalone.virtual_chassis = self.virtual_chassis
            standalone.vc_position = 3
            standalone.save()

        interface.refresh_from_db()
        self.assertEqual(interface.name, "mgmt-3")

    def test_installing_a_module_without_a_rule_keeps_the_raw_name(self):
        module_type = make_module_type(self.type_a.manufacturer, "RenTrig Unruled", model="RenTrig Unruled")
        InterfaceTemplate.objects.create(module_type=module_type, name="{module}", type=PLAIN_TYPE)

        module = self._install(module_type)

        self.assertEqual(self._names(module), ["0"])

    def test_a_save_that_changes_no_compared_value_causes_no_reapply(self):
        module = self._install()

        with (
            _module_reapplies() as module_reapplies,
            _device_reapplies() as device_reapplies,
            self.captureOnCommitCallbacks(execute=True),
        ):
            module.description = "unrelated edit"
            module.save()
            self.device.description = "unrelated edit"
            self.device.save()
            make_device(
                f"{self.prefix} New", self.device.device_type, virtual_chassis=self.virtual_chassis, vc_position=4
            )

        self.assertEqual((module_reapplies.call_count, device_reapplies.call_count), (0, 0))

    def test_a_module_deleted_before_commit_is_skipped(self):
        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True):
            module = Module.objects.create(device=self.device, module_bay=self._bay(), module_type=self.type_a)
            module.delete()

        self.assertEqual(reapplies.call_count, 0)
        self.assertFalse(Interface.objects.filter(device=self.device).exists())

    def test_a_device_deleted_before_commit_is_skipped(self):
        with _device_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True):
            self._move_to_position(2)
            self.device.delete()

        self.assertEqual(reapplies.call_count, 0)
        self.assertFalse(Device.objects.filter(pk=self.device.pk).exists())

    def test_a_save_stores_no_previous_state_on_the_instance(self):
        module = self._install()

        with self.captureOnCommitCallbacks(execute=True):
            self._change_type(module, self.type_b)
            self._move_to_position(2)

        for instance in (module, self.device):
            with self.subTest(model=type(instance).__name__):
                self.assertEqual([name for name in vars(instance) if name.startswith("_prev")], [])

    def test_the_device_reapply_reads_the_rule_fingerprint_once_for_all_modules(self):
        from netbox_interface_name_rules import rule_selection

        first = self._install()
        with self.captureOnCommitCallbacks(execute=True):
            second = Module.objects.create(device=self.device, module_bay=self._bay("Bay 1"), module_type=self.type_a)
        calls = []
        real_version = rule_selection._enabled_rules_version

        def counting_version(*args, **kwargs):
            calls.append(1)
            return real_version(*args, **kwargs)

        rule_selection._enabled_rules_version = counting_version
        try:
            with self.captureOnCommitCallbacks(execute=True):
                self._move_to_position(2)
        finally:
            rule_selection._enabled_rules_version = real_version

        self.assertEqual((self._names(first), self._names(second)), (["et-2/0/0"], ["et-2/0/1"]))
        self.assertEqual(calls, [1], "The device reapply must read the rule-set fingerprint once.")


class RenameTriggerAPITest(_RenameTriggerFixture, APITestCase):
    """A REST API edit reaches the rename trigger through NetBox's own write path."""

    model = Module
    user_permissions = ("dcim.view_module", "dcim.change_module", "dcim.view_device", "dcim.change_device")

    @classmethod
    def setUpTestData(cls):
        cls.build("RenTrigApi")

    def setUp(self):
        super().setUp()
        with self.captureOnCommitCallbacks(execute=True):
            self.module = Module.objects.create(device=self.device, module_bay=self._bay(), module_type=self.type_a)

    def _patch(self, instance, data):
        url = reverse(f"dcim-api:{instance._meta.model_name}-detail", kwargs={"pk": instance.pk})
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_patching_the_module_type_reapplies_with_the_new_rule(self):
        self._patch(self.module, {"module_type": self.type_b.pk})

        self.assertEqual(self._names(self.module), ["xe-1/0/0"])

    def test_patching_the_virtual_chassis_position_renames_the_module_interfaces(self):
        self._patch(self.device, {"vc_position": 2})

        self.assertEqual(self._names(self.module), ["et-2/0/0"])

    def test_the_journal_entry_names_the_request_user_as_its_author(self):
        Interface.objects.create(device=self.device, name="xe-1/0/0", type=PLAIN_TYPE)

        self._patch(self.module, {"module_type": self.type_b.pk})

        (entry,) = _journal(self.module)
        self.assertEqual(entry.created_by, self.user)


@contextmanager
def _previous_state_read_fails(statement):
    """Replace the previous-state read that *statement* matches by SQL that PostgreSQL rejects."""
    replaced = []

    def divide_by_zero(execute, sql, params, many, context):
        if statement.match(sql):
            replaced.append(sql)
            return execute("SELECT 1/0", None, many, context)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(divide_by_zero):
        yield replaced


class PreviousStateReadFailureTest(RenameTriggerTestCase):
    """A save whose previous state cannot be read fails with the read error, on both paths."""

    def test_a_module_save_fails_with_the_read_error(self):
        module = self._install()

        with (
            _previous_state_read_fails(MODULE_STATE_READ) as replaced,
            self.assertRaisesMessage(DataError, "division by zero"),
            transaction.atomic(),
        ):
            self._change_type(module, self.type_b)

        self.assertEqual(len(replaced), 1)
        self.assertEqual(Module.objects.get(pk=module.pk).module_type, self.type_a)

    def test_a_device_save_fails_with_the_read_error(self):
        with (
            _previous_state_read_fails(DEVICE_STATE_READ) as replaced,
            self.assertRaisesMessage(DataError, "division by zero"),
            transaction.atomic(),
        ):
            self._move_to_position(2)

        self.assertEqual(len(replaced), 1)
        self.assertEqual(Device.objects.get(pk=self.device.pk).vc_position, 1)


class CoalescedTriggerTest(RenameTriggerTestCase):
    """Several triggers for one module or device in one transaction cause one reapply."""

    def test_two_module_type_changes_reapply_once_with_the_final_rule(self):
        module = self._install()

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._change_type(module, self.type_b)
            self._change_type(module, self.type_c)

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["ge-1/0/0"])

    def test_an_install_and_a_type_change_reapply_once_with_force(self):
        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            module = Module.objects.create(device=self.device, module_bay=self._bay(), module_type=self.type_a)
            self._change_type(module, self.type_b)

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["xe-1/0/0"])

    def test_a_module_type_changed_and_changed_back_is_compared_with_the_earliest_state(self):
        module = self._install()
        rename_out_of_band(Interface.objects.get(module=module), "operator-name")

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._change_type(module, self.type_b)
            self._change_type(module, self.type_a)

        self.assertEqual(reapplies.call_count, 0)
        self.assertEqual(self._names(module), ["operator-name"])

    def test_an_install_under_a_reused_key_is_not_absorbed_by_a_pending_type_change(self):
        module = self._install()

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._change_type(module, self.type_b)
            pk = module.pk
            module.delete()
            _give_the_next_module_id(pk)
            module = Module.objects.create(device=self.device, module_bay=self._bay(), module_type=self.type_a)

        self.assertEqual(module.pk, pk)
        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_two_position_changes_reapply_once_with_the_final_position(self):
        module = self._install()

        with _device_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._move_to_position(2)
            self._move_to_position(3)

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-3/0/0"])

    def test_a_position_changed_and_changed_back_is_compared_with_the_earliest_state(self):
        module = self._install()
        rename_out_of_band(Interface.objects.get(module=module), "operator-name")

        with _device_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._move_to_position(2)
            self._move_to_position(1)

        self.assertEqual(reapplies.call_count, 0)
        self.assertEqual(self._names(module), ["operator-name"])

    def test_leaving_and_rejoining_the_virtual_chassis_is_compared_with_the_earliest_state(self):
        module = self._install()
        rename_out_of_band(Interface.objects.get(module=module), "operator-name")

        with _device_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self.device.virtual_chassis = None
            self.device.vc_position = None
            self.device.save()
            self.device.virtual_chassis = self.virtual_chassis
            self._move_to_position(1)

        self.assertEqual(reapplies.call_count, 0)
        self.assertEqual(self._names(module), ["operator-name"])

    def test_a_trigger_in_a_rolled_back_savepoint_does_not_suppress_a_later_trigger(self):
        module = self._install()

        with _device_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._move_to_position(2)
                raise RuntimeError("roll back the savepoint")
            self.device.refresh_from_db()
            self._move_to_position(3)

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-3/0/0"])

    def test_a_rolled_back_savepoint_keeps_the_earlier_reapply(self):
        module = self._install()

        with _device_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._move_to_position(2)
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._move_to_position(1)
                raise RuntimeError("roll back the savepoint")

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-2/0/0"])


def _reject_interface_updates(execute, sql, params, many, context):
    if sql.lstrip().startswith('UPDATE "dcim_interface"'):
        raise IntegrityError("injected reapply failure")
    return execute(sql, params, many, context)


def _reject_device_updates(execute, sql, params, many, context):
    if sql.lstrip().startswith('UPDATE "dcim_device"'):
        raise IntegrityError("injected device save failure")
    return execute(sql, params, many, context)


class PreviousStateHandoffTest(RenameTriggerTestCase):
    """The previous state lives from pre_save to post_save of one save, and no longer."""

    def test_a_completed_save_leaves_no_previous_state_behind(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._move_to_position(2)

        self.assertNotIn(id(self.device), rename_triggers._previous_states)

    def test_a_failed_save_leaves_no_previous_state_once_its_instance_is_gone(self):
        device = Device.objects.get(pk=self.device.pk)
        device.vc_position = 2
        with (
            connection.execute_wrapper(_reject_device_updates),
            self.assertRaises(IntegrityError),
            transaction.atomic(),
        ):
            device.save()
        key = id(device)
        self.assertIn(key, rename_triggers._previous_states)

        del device
        gc.collect()

        self.assertNotIn(key, rename_triggers._previous_states)

    def test_a_post_save_sent_without_a_model_save_is_not_a_trigger(self):
        module = self._install()
        moved = Module.objects.get(pk=module.pk)
        moved.module_type = self.type_b

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True):
            post_save.send(sender=Module, instance=moved, created=False, raw=False, using="default", update_fields=None)

        self.assertEqual(reapplies.call_count, 0)


class ReapplyFailureTest(RenameTriggerTestCase):
    """A reapply that fails after commit is logged, and the committed save stands."""

    def test_a_failed_module_reapply_is_logged(self):
        module = self._install()
        with self.captureOnCommitCallbacks() as callbacks:
            self._change_type(module, self.type_b)

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs(PLUGIN_LOGGER, "ERROR") as logs:
            for callback in callbacks:
                callback()

        self.assertIn("Failed to apply interface name rules", "\n".join(logs.output))
        self.assertEqual(Module.objects.get(pk=module.pk).module_type, self.type_b)
        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_a_failed_device_reapply_is_logged_for_modules_and_device_interfaces(self):
        module = self._install()
        Interface.objects.create(device=self.device, name="mgmt0", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            name_template="mgmt-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="mgmt0"
        )
        with self.captureOnCommitCallbacks() as callbacks:
            self._move_to_position(2)

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs(PLUGIN_LOGGER, "ERROR") as logs:
            for callback in callbacks:
                callback()

        output = "\n".join(logs.output)
        self.assertIn("Failed to re-apply module rules", output)
        self.assertIn("Failed to re-apply device interface rules", output)
        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_a_failed_module_reload_is_logged(self):
        module = self._install()
        with self.captureOnCommitCallbacks() as callbacks:
            self._change_type(module, self.type_b)

        with (
            connection.execute_wrapper(_reject_reads_of("dcim_module")),
            self.assertLogs(PLUGIN_LOGGER, "ERROR") as logs,
        ):
            _run_the_reapply(callbacks)

        self.assertEqual([str(record.exc_info[1]) for record in logs.records], ["injected dcim_module read failure"])
        self.assertEqual(Module.objects.get(pk=module.pk).module_type, self.type_b)
        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_a_module_type_read_failure_does_not_escape_the_reapply(self):
        module = self._install()
        with self.captureOnCommitCallbacks() as callbacks:
            self._change_type(module, self.type_b)

        with (
            connection.execute_wrapper(_reject_reads_of("dcim_moduletype")),
            self.assertLogs(PLUGIN_LOGGER, "ERROR") as logs,
        ):
            _run_the_reapply(callbacks)

        self.assertEqual(
            [str(record.exc_info[1]) for record in logs.records], ["injected dcim_moduletype read failure"]
        )
        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_a_failed_device_reload_is_logged(self):
        module = self._install()
        with self.captureOnCommitCallbacks() as callbacks:
            self._move_to_position(2)

        with (
            connection.execute_wrapper(_reject_reads_of("dcim_device")),
            self.assertLogs(PLUGIN_LOGGER, "ERROR") as logs,
        ):
            _run_the_reapply(callbacks)

        self.assertEqual([str(record.exc_info[1]) for record in logs.records], ["injected dcim_device read failure"])
        self.assertEqual(Device.objects.get(pk=self.device.pk).vc_position, 2)
        self.assertEqual(self._names(module), ["et-1/0/0"])


def _run_the_reapply(callbacks):
    """Run the rename-trigger callbacks among *callbacks*: each trigger, then the plan after them."""
    for callback in callbacks:
        if isinstance(
            callback, (rename_triggers.ModuleTrigger, rename_triggers.DeviceTrigger, rename_triggers.ReapplyPlan)
        ):
            callback()


def _reject_reads_of(table):
    """Return an execute wrapper that fails every read of *table*."""

    def reject(execute, sql, params, many, context):
        if sql.lstrip().startswith("SELECT") and (f'FROM "{table}"' in sql or f'JOIN "{table}"' in sql):
            raise DatabaseError(f"injected {table} read failure")
        return execute(sql, params, many, context)

    return reject


def _reject_journal_writes(execute, sql, params, many, context):
    if sql.lstrip().startswith('INSERT INTO "extras_journalentry"'):
        raise DatabaseError("injected journal write failure")
    return execute(sql, params, many, context)


class RenameJournalTest(RenameTriggerTestCase):
    """A reapply that leaves an interface unrenamed or fails writes one journal entry."""

    def _module_type_with_rule(self, model, templates, name_template, interface_type=PLAIN_TYPE, **rule_fields):
        module_type = make_module_type(self.type_a.manufacturer, model, model=model)
        for template in templates:
            InterfaceTemplate.objects.create(module_type=module_type, name=template, type=interface_type)
        rule = InterfaceNameRule.objects.create(module_type=module_type, name_template=name_template, **rule_fields)
        return module_type, rule

    def _standalone_device(self):
        return make_device(f"{self.prefix} Solo", self.device.device_type)

    def _leave(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.device.virtual_chassis = None
            self.device.vc_position = None
            self.device.save()

    def _install_renamed_by_the_operator(self):
        """Install a module whose rule needs no {vc_position}, then rename its interface by hand."""
        fixed_type, _ = self._module_type_with_rule("RenTrig Fixed", ("{module}",), "ge-{bay_position}")
        with self.captureOnCommitCallbacks(execute=True):
            module = Module.objects.create(device=self.device, module_bay=self._bay("Bay 1"), module_type=fixed_type)
        rename_out_of_band(Interface.objects.get(module=module), "operator-name")
        return module

    def test_a_name_collision_writes_one_warning_entry_on_the_module(self):
        module = self._install()
        Interface.objects.create(device=self.device, name="xe-1/0/0", type=PLAIN_TYPE)

        with self.captureOnCommitCallbacks(execute=True):
            self._change_type(module, self.type_b)

        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn("`et-1/0/0` to `xe-1/0/0`", entry.comments)
        self.assertIn("already in use", entry.comments)
        self.assertIsNone(entry.created_by)
        self.assertEqual(_journal(self.device), [])
        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_the_entry_names_only_the_interfaces_left_unrenamed(self):
        module_type, _ = self._module_type_with_rule("RenTrig Pair", ("a{module}", "b{module}"), "{base}.{vc_position}")
        Interface.objects.create(device=self.device, name="b0.1", type=PLAIN_TYPE)

        module = self._install(module_type)

        self.assertEqual(self._names(module), ["a0.1", "b0"])
        (entry,) = _journal(module)
        self.assertIn("`b0` to `b0.1`", entry.comments)
        self.assertNotIn("a0", entry.comments)

    def test_an_interface_no_template_claims_is_reported(self):
        module = self._install()
        claiming_type, _ = self._module_type_with_rule("RenTrig Base", ("port{module}",), "xe-{base}")

        with self.captureOnCommitCallbacks(execute=True):
            self._change_type(module, claiming_type)

        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn("`et-1/0/0`", entry.comments)
        self.assertIn("no single interface template claims", entry.comments)
        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_a_template_variable_the_device_lacks_is_reported(self):
        standalone = self._standalone_device()

        with self.captureOnCommitCallbacks(execute=True):
            module = Module.objects.create(
                device=standalone,
                module_bay=ModuleBay.objects.get(device=standalone, name="Bay 0"),
                module_type=self.type_a,
            )

        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn("`0`", entry.comments)
        self.assertIn("{vc_position} is not available", entry.comments)
        self.assertEqual(self._names(module), ["0"])

    def test_a_template_variable_the_device_lacks_does_not_flag_the_rule_as_potentially_deprecated(self):
        standalone = self._standalone_device()

        with self.captureOnCommitCallbacks(execute=True):
            Module.objects.create(
                device=standalone,
                module_bay=ModuleBay.objects.get(device=standalone, name="Bay 0"),
                module_type=self.type_a,
            )

        rule = InterfaceNameRule.objects.get(module_type=self.type_a)
        self.assertFalse(rule.tags.filter(slug="potentially-deprecated").exists())

    def test_a_rule_that_cannot_be_evaluated_writes_a_danger_entry(self):
        module_type, rule = self._module_type_with_rule("RenTrig Divide", ("{module}",), "et-{1 // {bay_position_num}}")

        module = self._install(module_type)

        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertIn("`0`", entry.comments)
        self.assertIn("Invalid arithmetic expression", entry.comments)
        self.assertEqual(self._names(module), ["0"])
        self.assertFalse(rule.tags.filter(slug="potentially-deprecated").exists())

    def test_a_failed_module_reapply_writes_a_danger_entry(self):
        module = self._install()
        with self.captureOnCommitCallbacks() as callbacks:
            self._change_type(module, self.type_b)

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs(PLUGIN_LOGGER, "ERROR"):
            for callback in callbacks:
                callback()

        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertIn("injected reapply failure", entry.comments)

    def test_a_failed_device_reapply_writes_one_danger_entry_on_the_device(self):
        module = self._install()
        Interface.objects.create(device=self.device, name="mgmt0", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            name_template="mgmt-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="mgmt0"
        )
        with self.captureOnCommitCallbacks() as callbacks:
            self._move_to_position(2)

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs(PLUGIN_LOGGER, "ERROR"):
            for callback in callbacks:
                callback()

        (entry,) = _journal(self.device)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertEqual(entry.comments.count("injected reapply failure"), 2)
        self.assertEqual(_journal(module), [])

    def test_leaving_the_virtual_chassis_renames_nothing_and_reports_the_skip(self):
        module = self._install()
        by_hand = self._install_renamed_by_the_operator()
        Interface.objects.create(device=self.device, name="mgmt0", type=PLAIN_TYPE)
        Interface.objects.create(device=self.device, name="eth0", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            name_template="mgmt-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="mgmt0"
        )
        InterfaceNameRule.objects.create(
            name_template="lan-{port}", applies_to_device_interfaces=True, module_type_pattern="eth0"
        )

        self._leave()

        (entry,) = _journal(self.device)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn("`et-1/0/0`", entry.comments)
        self.assertIn("`mgmt0`", entry.comments)
        self.assertIn("{vc_position} is not available", entry.comments)
        self.assertNotIn("eth0", entry.comments)
        self.assertNotIn("operator-name", entry.comments)
        self.assertEqual(_journal(module), [])
        self.assertEqual((self._names(module), self._names(by_hand)), (["et-1/0/0"], ["operator-name"]))
        self.assertEqual(
            sorted(Interface.objects.filter(device=self.device, module=None).values_list("name", flat=True)),
            ["eth0", "mgmt0"],
        )

    def test_a_virtual_chassis_member_without_a_position_renames_nothing_and_reports_the_skip(self):
        module = self._install()
        by_hand = self._install_renamed_by_the_operator()

        with self.captureOnCommitCallbacks(execute=True):
            self._move_to_position(None)

        (entry,) = _journal(self.device)
        self.assertIn("`et-1/0/0`", entry.comments)
        self.assertNotIn("operator-name", entry.comments)
        self.assertEqual((self._names(module), self._names(by_hand)), (["et-1/0/0"], ["operator-name"]))

    def test_leaving_the_virtual_chassis_reports_every_member_of_a_flat_family(self):
        flat_type, _ = self._module_type_with_rule(
            "RenTrig Flat",
            ("{module}",),
            "et-{vc_position}/{bay_position}:{channel}",
            channel_count=2,
            breakout_mode=BreakoutModeChoices.FLAT,
        )
        module = self._install(flat_type)
        self.assertEqual(self._names(module), ["et-1/0:0", "et-1/0:1"])

        self._leave()

        (entry,) = _journal(self.device)
        self.assertIn("`et-1/0:0`", entry.comments)
        self.assertIn("`et-1/0:1`", entry.comments)
        self.assertEqual(self._names(module), ["et-1/0:0", "et-1/0:1"])

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_leaving_the_virtual_chassis_does_not_report_a_parent_that_keeps_its_name(self):
        channelized_type, _ = self._module_type_with_rule(
            "RenTrig Channelized",
            ("{module}",),
            "xe-{vc_position}/0/{bay_position}:{channel}",
            interface_type=PARENT_TYPE,
            channel_count=2,
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
        )
        module = self._install(channelized_type)
        self.assertEqual(self._names(module), ["0", "xe-1/0/0:0", "xe-1/0/0:1"])

        self._leave()

        (entry,) = _journal(self.device)
        self.assertIn("`xe-1/0/0:0`", entry.comments)
        self.assertIn("`xe-1/0/0:1`", entry.comments)
        self.assertNotIn("`0`", entry.comments)

    def _install_channelized_family(self, model, **rule_fields):
        """Install a module whose templates form a two-channel family, under a rule with *rule_fields*."""
        module_type = _channelized_module_type(self.type_a.manufacturer, model, channels=2, child_channel_ids=(1, 2))
        InterfaceNameRule.objects.create(module_type=module_type, **rule_fields)
        return self._install(module_type)

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_leaving_the_virtual_chassis_reports_a_parent_a_simple_rule_renames(self):
        module = self._install_channelized_family("RenTrig Lockstep", name_template="et-{vc_position}/{bay_position}")
        self.assertEqual(self._names(module), ["et-1/0", "et-1/0:1", "et-1/0:2"])

        self._leave()

        (entry,) = _journal(self.device)
        for name in ("et-1/0", "et-1/0:1", "et-1/0:2"):
            self.assertIn(f"`{name}`", entry.comments)

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_leaving_the_virtual_chassis_does_not_report_a_parent_a_flat_breakout_rule_keeps(self):
        module = self._install_channelized_family(
            "RenTrig Flat Breakout",
            name_template="xe-{vc_position}/0/{bay_position}:{channel}",
            channel_count=2,
            breakout_mode=BreakoutModeChoices.FLAT,
        )
        self.assertEqual(self._names(module), ["0", "xe-1/0/0:0", "xe-1/0/0:1"])

        self._leave()

        (entry,) = _journal(self.device)
        self.assertIn("`xe-1/0/0:0`", entry.comments)
        self.assertNotIn("`0`", entry.comments)

    def test_a_failure_stays_reported_when_a_lower_priority_rule_renames_the_interface(self):
        Interface.objects.create(device=self.device, name="mgmt0", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            name_template="mgmt-{8 // ({vc_position} - 2)}",
            applies_to_device_interfaces=True,
            module_type_pattern="mgmt0",
        )
        InterfaceNameRule.objects.create(
            name_template="oob-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="mgmt."
        )

        with self.captureOnCommitCallbacks(execute=True):
            self._move_to_position(2)

        self.assertTrue(Interface.objects.filter(device=self.device, name="oob-2").exists())
        (entry,) = _journal(self.device)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertIn("`mgmt0`", entry.comments)

    def test_an_interface_a_lower_priority_rule_renames_is_not_reported(self):
        Interface.objects.create(device=self.device, name="mgmt0", type=PLAIN_TYPE)
        Interface.objects.create(device=self.device, name="mgmt-2", type=PLAIN_TYPE)
        Interface.objects.create(device=self.device, name="eth0", type=PLAIN_TYPE)
        Interface.objects.create(device=self.device, name="lan-2", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            name_template="mgmt-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="mgmt0"
        )
        InterfaceNameRule.objects.create(
            name_template="oob-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="mgmt."
        )
        # A control: eth0 stays blocked, so the entry exists and must still leave mgmt0 out.
        InterfaceNameRule.objects.create(
            name_template="lan-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="eth0"
        )

        with self.captureOnCommitCallbacks(execute=True):
            self._move_to_position(2)

        self.assertTrue(Interface.objects.filter(device=self.device, name="oob-2").exists())
        (entry,) = _journal(self.device)
        self.assertIn("`eth0` to `lan-2`", entry.comments)
        self.assertNotIn("mgmt0", entry.comments)

    def test_a_device_reapply_that_fails_on_one_module_keeps_the_earlier_outcomes(self):
        blocked = self._install()
        with self.captureOnCommitCallbacks(execute=True):
            Module.objects.create(device=self.device, module_bay=self._bay("Bay 1"), module_type=self.type_a)
        Interface.objects.create(device=self.device, name="et-2/0/0", type=PLAIN_TYPE)
        with self.captureOnCommitCallbacks() as callbacks:
            self._move_to_position(2)

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs(PLUGIN_LOGGER, "ERROR"):
            for callback in callbacks:
                callback()

        (entry,) = _journal(self.device)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertIn("`et-1/0/0` to `et-2/0/0`", entry.comments)
        self.assertIn("injected reapply failure", entry.comments)
        self.assertEqual(self._names(blocked), ["et-1/0/0"])

    def test_a_device_reapply_that_fails_on_one_device_interface_family_keeps_the_earlier_outcomes(self):
        Interface.objects.create(device=self.device, name="mgmt0", type=PLAIN_TYPE)
        Interface.objects.create(device=self.device, name="eth0", type=PLAIN_TYPE)
        Interface.objects.create(device=self.device, name="mgmt-2", type=PLAIN_TYPE)
        # The longer pattern sorts first, so the blocked mgmt0 family runs before the failing eth0 family.
        InterfaceNameRule.objects.create(
            name_template="mgmt-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="mgmt0"
        )
        InterfaceNameRule.objects.create(
            name_template="lan-{vc_position}", applies_to_device_interfaces=True, module_type_pattern="eth0"
        )
        with self.captureOnCommitCallbacks() as callbacks:
            self._move_to_position(2)

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs(PLUGIN_LOGGER, "ERROR"):
            for callback in callbacks:
                callback()

        (entry,) = _journal(self.device)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertIn("`mgmt0` to `mgmt-2`", entry.comments)
        self.assertIn("injected reapply failure", entry.comments)
        self.assertEqual(
            sorted(Interface.objects.filter(device=self.device, module=None).values_list("name", flat=True)),
            ["eth0", "mgmt-2", "mgmt0"],
        )

    def test_a_module_reapply_that_fails_on_one_family_keeps_the_earlier_outcomes(self):
        module_type, _ = self._module_type_with_rule("RenTrig Pair", ("a{module}", "b{module}"), "{base}.{vc_position}")
        Interface.objects.create(device=self.device, name="a0.1", type=PLAIN_TYPE)
        with self.captureOnCommitCallbacks() as callbacks:
            module = Module.objects.create(device=self.device, module_bay=self._bay(), module_type=module_type)

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs(PLUGIN_LOGGER, "ERROR"):
            for callback in callbacks:
                callback()

        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertIn("`a0` to `a0.1`", entry.comments)
        self.assertIn("injected reapply failure", entry.comments)
        self.assertEqual(self._names(module), ["a0", "b0"])

    def test_a_trigger_that_renames_or_keeps_every_name_writes_no_entry(self):
        renamed = self._install()
        fixed_type, _ = self._module_type_with_rule("RenTrig Fixed", ("{module}",), "ge-{bay_position}")
        with self.captureOnCommitCallbacks(execute=True):
            kept = Module.objects.create(device=self.device, module_bay=self._bay("Bay 1"), module_type=fixed_type)

        with self.captureOnCommitCallbacks(execute=True):
            self._move_to_position(2)

        self.assertEqual((self._names(renamed), self._names(kept)), (["et-2/0/0"], ["ge-1"]))
        self.assertFalse(JournalEntry.objects.exists())

        # A control: a later trigger that leaves a name taken writes the entry the first one did not.
        Interface.objects.create(device=self.device, name="et-3/0/0", type=PLAIN_TYPE)
        with self.captureOnCommitCallbacks(execute=True):
            self._move_to_position(3)

        (entry,) = JournalEntry.objects.all()
        self.assertIn("`et-2/0/0` to `et-3/0/0`", entry.comments)

    def test_a_failed_journal_write_is_logged_and_does_not_raise(self):
        module = self._install()
        Interface.objects.create(device=self.device, name="xe-1/0/0", type=PLAIN_TYPE)
        with self.captureOnCommitCallbacks() as callbacks:
            self._change_type(module, self.type_b)

        with connection.execute_wrapper(_reject_journal_writes), self.assertLogs(PLUGIN_LOGGER, "ERROR") as logs:
            for callback in callbacks:
                callback()

        self.assertEqual([str(record.exc_info[1]) for record in logs.records], ["injected journal write failure"])
        self.assertEqual(_journal(module), [])
        self.assertEqual(Module.objects.get(pk=module.pk).module_type, self.type_b)


class CommittedRenameTriggerTest(_RenameTriggerFixture, TransactionTestCase):
    """Rename triggers against real commits and rollbacks, not the test transaction."""

    def setUp(self):
        self.build("RenTrigTx")
        with transaction.atomic():
            self.module = Module.objects.create(device=self.device, module_bay=self._bay(), module_type=self.type_a)

    def test_the_install_reapply_runs_after_commit(self):
        self.assertEqual(self._names(self.module), ["et-1/0/0"])

    def test_without_an_open_transaction_the_device_reapply_reads_the_saved_row(self):
        self.device.vc_position = 2
        self.device.save()

        self.assertEqual(self._names(self.module), ["et-2/0/0"])

    def test_without_an_open_transaction_the_module_reapply_reads_the_saved_row(self):
        self.module.module_type = self.type_b
        self.module.save(update_fields=["module_type"])

        self.assertEqual(self._names(self.module), ["xe-1/0/0"])

    def test_a_rolled_back_transaction_does_not_suppress_the_next_trigger(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.device.vc_position = 2
            self.device.save()
            raise RuntimeError("roll back the transaction")
        self.device.refresh_from_db()

        with transaction.atomic():
            self.device.vc_position = 3
            self.device.save()

        self.assertEqual(self._names(self.module), ["et-3/0/0"])
