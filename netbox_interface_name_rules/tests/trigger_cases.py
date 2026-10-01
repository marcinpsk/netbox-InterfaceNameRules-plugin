# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Fixtures and probes that several rename-trigger test modules share.

``MoveFixture`` builds two device types with the same bays and three devices in two virtual chassis.
``ModuleMoveTestCase`` and ``BayEditTestCase`` install, move and edit through real saves. The probes
count the reapplies, read the journal and inject database failures.
"""

import re
from contextlib import contextmanager
from typing import NamedTuple
from unittest.mock import patch

from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay, ModuleBayTemplate, Platform, VirtualChassis
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from extras.models import JournalEntry

from netbox_interface_name_rules import engine
from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import (
    PLAIN_TYPE,
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_type,
    make_placement,
    slug_for,
)

BAYS = (("Bay 0", "0"), ("Bay 1", "1"), ("Bay 2", "2"), ("Bay 10", "10"))
REQUIRES_SUBTREE_MOVES = "requires a NetBox that moves a module's nested bays with it (4.7+)"
FLAT = "a flat breakout family is not renamed after a move, a bay edit or a parent module type change"
NO_RULE = "no rule matches the module after the change"
TAKEN = "target name is already in use"
UNAVAILABLE = "{vc_position} is not available on this device"
NAMING_READ = re.compile(r'SELECT .* FROM "dcim_module" .*"dcim_platform"')


class ChassisRule(NamedTuple):
    """One rule shape that the tests of a chassis change with a module change cover."""

    model: str
    name_template: str


# A plain rule, a rule that reads {base}, and a rule with the virtual-chassis position in arithmetic.
CHASSIS_RULES = (
    ChassisRule("Plain", "et-{vc_position}/{slot}/{bay_position}"),
    ChassisRule("Base", "p{base}-{vc_position}/{slot}"),
    ChassisRule("Arithmetic", "x{{vc_position} * 10 + {slot_num}}/{bay_position}"),
)


def reject_interface_updates(execute, sql, params, many, context):
    """Fail each update of an interface, as an execute wrapper."""
    if sql.lstrip().startswith('UPDATE "dcim_interface"'):
        raise IntegrityError("injected reapply failure")
    return execute(sql, params, many, context)


def fail_the_naming_read(execute, sql, params, many, context):
    """Replace the subtree naming read by SQL that PostgreSQL rejects, as an execute wrapper."""
    if NAMING_READ.match(sql):
        return execute("SELECT 1/0", None, many, context)
    return execute(sql, params, many, context)


def journal(instance):
    """Return the journal entries on *instance*, oldest first."""
    return list(
        JournalEntry.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(instance), assigned_object_id=instance.pk
        ).order_by("pk")
    )


@contextmanager
def module_reapplies():
    """Count the module reapplies; each call still runs the real function."""
    with patch.object(engine, "module_rule_outcomes", wraps=engine.module_rule_outcomes) as spy:
        yield spy


def reapplied(spy):
    """Return the primary key of the module of each reapply that *spy* recorded, sorted."""
    return sorted(call.args[0].pk for call in spy.call_args_list)


@contextmanager
def naming_reads():
    """Record the queries and the result of each subtree naming read; each call still runs the real function."""
    reads = []
    real = engine.read_subtree_naming

    def read(module_pk):
        with CaptureQueriesContext(connection) as queries:
            naming = real(module_pk)
        reads.append((queries.captured_queries, naming))
        return naming

    with patch.object(engine, "read_subtree_naming", read):
        yield reads


def give_the_next_module_id(pk):
    """Make the database assign *pk* to the next module that NetBox creates."""
    # NetBox before 4.7 creates no components for a module saved with an explicit pk.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence(%s, %s), %s, false)",
            [Module._meta.db_table, Module._meta.pk.column, pk],
        )


@contextmanager
def previous_state_read_fails(statement):
    """Replace the previous-state read that *statement* matches by SQL that PostgreSQL rejects."""
    replaced = []

    def divide_by_zero(execute, sql, params, many, context):
        if statement.match(sql):
            replaced.append(sql)
            return execute("SELECT 1/0", None, many, context)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(divide_by_zero):
        yield replaced


def reject_reads_of(table):
    """Return an execute wrapper that fails every read of *table*."""

    def reject(execute, sql, params, many, context):
        if sql.lstrip().startswith("SELECT") and (f'FROM "{table}"' in sql or f'JOIN "{table}"' in sql):
            raise DatabaseError(f"injected {table} read failure")
        return execute(sql, params, many, context)

    return reject


class MoveFixture:
    """Two device types with the same bays, and three devices in two virtual chassis.

    ``device`` and ``peer`` are virtual-chassis positions 1 and 2 of one chassis; ``remote`` has
    another device type and platform, at position 5 of another chassis.
    """

    @classmethod
    def build(cls, prefix):
        """Create the fixture objects and return them as class attributes of *cls*."""
        cls.prefix = prefix
        cls.manufacturer = make_manufacturer(prefix)
        cls.device_type = make_device_type(cls.manufacturer, prefix)
        cls.other_device_type = make_device_type(cls.manufacturer, f"{prefix} Other")
        for device_type in (cls.device_type, cls.other_device_type):
            for name, position in BAYS:
                ModuleBayTemplate.objects.create(device_type=device_type, name=name, position=position)
        cls.platform = Platform.objects.create(name=f"{prefix} OS", slug=slug_for(prefix, "os"))
        cls.other_platform = Platform.objects.create(name=f"{prefix} Other OS", slug=slug_for(prefix, "other-os"))
        placement = make_placement(prefix)
        chassis = VirtualChassis.objects.create(name=f"{prefix} VC")
        remote_chassis = VirtualChassis.objects.create(name=f"{prefix} Remote VC")
        cls.device = cls._device(placement, "01", cls.device_type, cls.platform, chassis, 1)
        cls.peer = cls._device(placement, "02", cls.device_type, cls.other_platform, chassis, 2)
        cls.remote = cls._device(placement, "03", cls.other_device_type, cls.other_platform, remote_chassis, 5)

    @classmethod
    def _device(cls, placement, suffix, device_type, platform, chassis, position):
        return make_device(
            cls.prefix,
            device_type,
            placement,
            name=slug_for(cls.prefix, suffix),
            platform=platform,
            virtual_chassis=chassis,
            vc_position=position,
        )

    @classmethod
    def _module_type(cls, model, *templates):
        module_type = make_module_type(cls.manufacturer, model, model=f"{cls.prefix} {model}")
        for template in templates:
            InterfaceTemplate.objects.create(module_type=module_type, name=template, type=PLAIN_TYPE)
        return module_type

    @classmethod
    def _card_type(cls, model, bay_position):
        """Return a module type that holds one nested bay at *bay_position*, and no interfaces."""
        card_type = make_module_type(cls.manufacturer, model, model=f"{cls.prefix} {model}")
        ModuleBayTemplate.objects.create(module_type=card_type, name="Port", position=bay_position)
        return card_type

    @staticmethod
    def _bay(device, name="Bay 0"):
        return ModuleBay.objects.get(device=device, module__isnull=True, name=name)

    @staticmethod
    def _names(module):
        return sorted(Interface.objects.filter(module=module).values_list("name", flat=True))


class ModuleMoveTestCase(MoveFixture, TestCase):
    """Install and move modules through real saves, with the committed callbacks run."""

    @classmethod
    def setUpTestData(cls):
        cls.build(cls.__name__)

    def _install(self, module_type, bay):
        with self.captureOnCommitCallbacks(execute=True):
            return Module.objects.create(device=bay.device, module_bay=bay, module_type=module_type)

    @staticmethod
    def _save_move(module, bay):
        module.device = bay.device
        module.module_bay = bay
        module.save()

    def _move(self, module, bay):
        with self.captureOnCommitCallbacks(execute=True):
            self._save_move(module, bay)

    def _install_card(self, card_type, bay):
        """Install *card_type* in *bay* and return it with its nested bay."""
        card = self._install(card_type, bay)
        return card, ModuleBay.objects.get(module=card)

    def _change_the_chassis_position(self, position=3):
        self.device.vc_position = position
        self.device.save()

    def _leave_the_chassis(self):
        self.device.virtual_chassis = None
        self.device.vc_position = None
        self.device.save()

    def _join_the_chassis(self, chassis):
        self.device.virtual_chassis = chassis
        self.device.vc_position = 3
        self.device.save()

    def _save_in_one_transaction(self, *saves):
        """Run each of *saves* in order in one transaction; return the spy of the module reapplies."""
        with module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            for save in saves:
                save()
        return reapplies

    def _save_with_a_device_change(self, device_change, save, device_first, before=()):
        """Run *device_change* and *save* in one transaction, *device_change* first when *device_first*.

        The saves in *before* run first in the same transaction.
        """
        ordered = (device_change, save) if device_first else (save, device_change)
        return self._save_in_one_transaction(*before, *ordered)


def flat_rule(module_type, name_template, **scope):
    """Create a flat breakout rule with two channels for *module_type*."""
    return InterfaceNameRule.objects.create(
        module_type=module_type,
        name_template=name_template,
        breakout_mode=BreakoutModeChoices.FLAT,
        channel_count=2,
        channel_start=0,
        **scope,
    )


class BayEditTestCase(ModuleMoveTestCase):
    """Install modules and edit their bays through real saves, with the committed callbacks run."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.plain_type = cls._module_type("Plain", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.plain_type, name_template="et-{vc_position}/0/{bay_position}")

    @staticmethod
    def _save_edit(bay, **values):
        for field, value in values.items():
            setattr(bay, field, value)
        bay.save()

    def _edit(self, bay, **values):
        with self.captureOnCommitCallbacks(execute=True):
            self._save_edit(bay, **values)
