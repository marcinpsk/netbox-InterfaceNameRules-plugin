# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""A module move is a rename trigger, driven through real module saves and asserted in the database.

The previous state of a move is read before the save: the old bay, the old device and the rule scope
of the moved module and of every module nested in it. After commit the reapply recognises the names
that state gave and renames them for the new position.

NetBox 4.7 moves a module's components and nested bays with it, and re-resolves the raw names and
the nested bay positions that still follow their templates. An earlier NetBox writes the module row
alone: the interfaces keep their raw names, and nested bays keep their parent, position and name.
"""

import functools
import importlib.util
import re
from contextlib import contextmanager
from typing import NamedTuple
from unittest import skipIf, skipUnless
from unittest.mock import patch

from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay, ModuleBayTemplate, Platform, VirtualChassis
from django.contrib.contenttypes.models import ContentType
from django.db import DataError, IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from extras.choices import JournalEntryKindChoices
from extras.models import JournalEntry
from rest_framework import status
from utilities.testing import APITestCase

from netbox_interface_name_rules import engine
from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.engine import supports_channelization, supports_vc_position_token
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.rename_triggers import PlanRunner
from netbox_interface_name_rules.tests.helpers import (
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_type,
    make_placement,
    slug_for,
)
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_channelization import REQUIRES_CHANNELIZATION, _channelized_module_type
from netbox_interface_name_rules.tests.test_vc_drift import REQUIRES_VC_POSITION_TOKEN

PLAIN_TYPE = "10gbase-x-sfpp"
BAYS = (("Bay 0", "0"), ("Bay 1", "1"), ("Bay 2", "2"), ("Bay 10", "10"))
NETBOX_MOVES_COMPONENTS = importlib.util.find_spec("dcim.models.module_moves") is not None
REQUIRES_SUBTREE_MOVES = "requires a NetBox that moves a module's nested bays with it (4.7+)"
REQUIRES_DEVICE_MOVES = "requires a NetBox that moves a module's interfaces to its new device (4.7+)"
UNCLAIMED = "no single interface template claims"
FLAT = "a flat breakout family is not renamed after a move, a bay edit or a parent module type change"
NOT_RENAMED = "the module is not renamed while one of its interfaces is unclaimed"
NO_RULE = "no rule matches the module after the change"
ELSEWHERE = "the interface is not on the device of its module"
STALE_BAY = "the module bay still has the parent bay it had before its module moved"
TAKEN = "target name is already in use"
UNAVAILABLE = "{vc_position} is not available on this device"
WRITE = re.compile(r'\s*(INSERT INTO|UPDATE|DELETE FROM) "(\w+)"')
NAMING_READ = re.compile(r'SELECT .* FROM "dcim_module" .*"dcim_platform"')


def _writes(queries):
    """Return ``(statement, table)`` for each write among *queries*."""
    return [match.groups() for query in queries if (match := WRITE.match(query["sql"]))]


def _reject_interface_updates(execute, sql, params, many, context):
    if sql.lstrip().startswith('UPDATE "dcim_interface"'):
        raise IntegrityError("injected reapply failure")
    return execute(sql, params, many, context)


def _fail_the_naming_read(execute, sql, params, many, context):
    if NAMING_READ.match(sql):
        return execute("SELECT 1/0", None, many, context)
    return execute(sql, params, many, context)


def _journal(instance):
    """Return the journal entries on *instance*, oldest first."""
    return list(
        JournalEntry.objects.filter(
            assigned_object_type=ContentType.objects.get_for_model(instance), assigned_object_id=instance.pk
        ).order_by("pk")
    )


@contextmanager
def _module_reapplies():
    """Count the module reapplies; each call still runs the real function."""
    with patch.object(engine, "module_rule_outcomes", wraps=engine.module_rule_outcomes) as spy:
        yield spy


def _reapplied(spy):
    """Return the primary key of the module of each reapply that *spy* recorded, sorted."""
    return sorted(call.args[0].pk for call in spy.call_args_list)


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


@contextmanager
def _naming_reads():
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


class _MoveFixture:
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


class ModuleMoveTestCase(_MoveFixture, TestCase):
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

    def _change_the_chassis_position(self):
        self.device.vc_position = 3
        self.device.save()

    def _leave_the_chassis(self):
        self.device.virtual_chassis = None
        self.device.vc_position = None
        self.device.save()

    def _join_the_chassis(self, chassis):
        self.device.virtual_chassis = chassis
        self.device.vc_position = 3
        self.device.save()

    def _save_with_a_device_change(self, device_change, save, device_first):
        """Run *device_change* and *save* in one transaction, *device_change* first when *device_first*.

        Return the spy of the module reapplies.
        """
        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            if device_first:
                device_change()
            save()
            if not device_first:
                device_change()
        return reapplies


class ModuleMoveTest(ModuleMoveTestCase):
    """A moved module gets the names its rule gives at the new position, for every rule shape."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.plain_type = cls._module_type("Plain", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.plain_type, name_template="et-{vc_position}/0/{bay_position}")
        cls.base_type = cls._module_type("Base", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.base_type, name_template="p{base}-{vc_position}")

    def test_a_module_moved_to_another_bay_is_renamed_for_the_new_bay(self):
        module = self._install(self.plain_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["et-1/0/0"])

        self._move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(self._names(module), ["et-1/0/1"])
        self.assertEqual(_journal(module), [])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_module_moved_to_another_device_in_the_chassis_is_renamed_for_its_position(self):
        module = self._install(self.plain_type, self._bay(self.device))

        self._move(module, self._bay(self.peer, "Bay 2"))

        self.assertEqual(self._names(module), ["et-2/0/2"])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_module_moved_to_a_device_in_another_chassis_is_renamed_for_its_position(self):
        module = self._install(self.plain_type, self._bay(self.device))

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["et-5/0/1"])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_base_rule_is_renamed_from_the_raw_name_of_the_new_bay(self):
        module = self._install(self.base_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["p0-1"])

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["p1-5"])

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_a_channelized_family_is_renamed_for_the_new_bay(self):
        module_type = _channelized_module_type(
            self.manufacturer, f"{self.prefix} Channelized", channels=2, child_channel_ids=(1, 2)
        )
        InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="xe-{vc_position}/0/{bay_position}:{channel}",
            parent_name_template="et-{vc_position}/0/{bay_position}",
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_count=2,
            channel_start=0,
        )
        module = self._install(module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["et-1/0/0", "xe-1/0/0:0", "xe-1/0/0:1"])

        self._move(module, self._bay(self.peer, "Bay 1"))

        self.assertEqual(self._names(module), ["et-2/0/1", "xe-2/0/1:0", "xe-2/0/1:1"])

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_a_channelized_family_whose_parent_keeps_its_raw_name_is_renamed_for_the_new_bay(self):
        module_type = _channelized_module_type(
            self.manufacturer, f"{self.prefix} Kept Parent", channels=2, child_channel_ids=(1, 2)
        )
        InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="xe-{vc_position}/0/{bay_position}:{channel}",
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_count=2,
            channel_start=0,
        )
        module = self._install(module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["0", "xe-1/0/0:0", "xe-1/0/0:1"])

        self._move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(self._names(module), ["1", "xe-1/0/1:0", "xe-1/0/1:1"])

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_an_unclaimed_interface_beside_a_channelized_family_keeps_every_name(self):
        module_type = _channelized_module_type(
            self.manufacturer, f"{self.prefix} Beside", channels=2, child_channel_ids=(1, 2)
        )
        InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="xe-{vc_position}/0/{bay_position}:{channel}",
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_count=2,
            channel_start=0,
        )
        module = self._install(module_type, self._bay(self.device))
        Interface.objects.create(device=self.device, module=module, name="operator-name", type=PLAIN_TYPE)
        with self.captureOnCommitCallbacks() as callbacks:
            self._save_move(module, self._bay(self.device, "Bay 1"))
        saved = self._names(module)

        for callback in callbacks:
            callback()

        self.assertEqual(self._names(module), saved)
        (entry,) = _journal(module)
        self.assertIn(f"`operator-name`: {UNCLAIMED}", entry.comments)
        for name in saved:
            if name != "operator-name":
                self.assertIn(f"`{name}`: {NOT_RENAMED}", entry.comments)

    def test_a_subinterface_does_not_stop_the_rename(self):
        module = self._install(self.plain_type, self._bay(self.device))
        Interface.objects.create(
            device=self.device,
            module=module,
            name="et-1/0/0.100",
            type="virtual",
            parent=Interface.objects.get(module=module),
        )

        self._move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(self._names(module), ["et-1/0/0.100", "et-1/0/1"])
        self.assertEqual(_journal(module), [])


@skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_SUBTREE_MOVES)
class NestedModuleMoveTest(ModuleMoveTestCase):
    """The modules nested in a moved module are renamed for their new position too."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.card_type = cls._card_type("Card", "1")
        cls.token_card_type = cls._card_type("Token Card", "{module}")
        cls.optic_type = cls._module_type("Optic", "{module}")
        InterfaceNameRule.objects.create(
            module_type=cls.optic_type, name_template="et-{vc_position}/{slot}/{bay_position}"
        )

    def test_the_modules_nested_in_a_moved_module_are_renamed(self):
        card, port = self._install_card(self.card_type, self._bay(self.device))
        optic = self._install(self.optic_type, port)
        self.assertEqual(self._names(optic), ["et-1/0/1"])

        self._move(card, self._bay(self.peer, "Bay 2"))

        self.assertEqual(self._names(optic), ["et-2/2/1"])

    def test_a_nested_module_whose_bay_netbox_renumbers_in_the_move_is_renamed(self):
        card, port = self._install_card(self.token_card_type, self._bay(self.device))
        self.assertEqual(port.position, "0")
        optic = self._install(self.optic_type, port)
        self.assertEqual(self._names(optic), ["et-1/0/0"])

        self._move(card, self._bay(self.device, "Bay 2"))

        port.refresh_from_db()
        self.assertEqual(port.position, "2")
        self.assertEqual(self._names(optic), ["et-1/2/2"])

    def _base_card(self, model):
        """Return a card type with one `{base}` interface of its own and one nested bay."""
        card_type = self._card_type(model, "1")
        InterfaceTemplate.objects.create(module_type=card_type, name="{module}", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(module_type=card_type, name_template="p{base}-{vc_position}")
        return card_type

    def test_after_a_type_change_and_a_move_the_card_is_reapplied_as_a_type_change_and_its_optic_as_moved(self):
        card, port = self._install_card(self._base_card("First Card"), self._bay(self.device))
        optic = self._install(self.optic_type, port)
        self.assertEqual((self._names(card), self._names(optic)), (["p0-1"], ["et-1/0/1"]))
        second_card_type = self._base_card("Second Card")

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            card.module_type = second_card_type
            card.save()
            self._save_move(card, self._bay(self.device, "Bay 2"))

        self.assertEqual((self._names(card), self._names(optic)), (["p0-1"], ["et-1/2/1"]))
        (entry,) = _journal(card)
        self.assertIn(f"`p0-1`: {UNCLAIMED}", entry.comments)

    def test_a_move_rolled_back_in_a_savepoint_leaves_the_pending_reapply_as_it_was(self):
        card, port = self._install_card(self.card_type, self._bay(self.device))
        optic = self._install(self.optic_type, port)
        other_card_type = self._card_type("Other Card", "1")

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            card.module_type = other_card_type
            card.save()
            with self.assertRaises(RuntimeError), transaction.atomic():
                Module.objects.get(pk=optic.pk).delete()
                self._save_move(card, self._bay(self.device, "Bay 1"))
                raise RuntimeError("roll back the savepoint")
            card.refresh_from_db()
            self._save_move(card, self._bay(self.device, "Bay 2"))

        self.assertEqual(self._names(optic), ["et-1/2/1"])

    def test_a_card_under_a_flat_rule_keeps_its_names_and_its_nested_modules_are_renamed(self):
        card_type = self._card_type("Flat Card", "1")
        InterfaceTemplate.objects.create(module_type=card_type, name="{module}", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            module_type=card_type,
            name_template="a-{bay_position}:{channel}",
            breakout_mode=BreakoutModeChoices.FLAT,
            channel_count=2,
            channel_start=0,
        )
        card, port = self._install_card(card_type, self._bay(self.device))
        optic = self._install(self.optic_type, port)
        self.assertEqual(self._names(card), ["a-0:0", "a-0:1"])

        self._move(card, self._bay(self.remote, "Bay 1"))

        self.assertEqual((self._names(card), self._names(optic)), (["a-0:0", "a-0:1"], ["et-5/1/1"]))
        (entry,) = _journal(card)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        for name in ("a-0:0", "a-0:1"):
            self.assertIn(f"`{name}`: {FLAT}", entry.comments)
        self.assertNotIn("et-", entry.comments)

    def test_a_nested_module_whose_reapply_fails_is_reported_with_the_outcomes_before_it(self):
        card_type = self._card_type("Two Port Card", "1")
        ModuleBayTemplate.objects.create(module_type=card_type, name="Port 2", position="2")
        card = self._install(card_type, self._bay(self.device))
        blocked, failed = (
            self._install(self.optic_type, bay) for bay in ModuleBay.objects.filter(module=card).order_by("position")
        )
        Interface.objects.create(device=self.remote, name="et-5/1/1", type=PLAIN_TYPE)
        with self.captureOnCommitCallbacks() as callbacks:
            self._save_move(card, self._bay(self.remote, "Bay 1"))

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs("netbox_interface_name_rules"):
            for callback in callbacks:
                callback()

        (entry,) = _journal(card)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertIn(f"`et-1/0/1` to `et-5/1/1`: {TAKEN}", entry.comments)
        self.assertIn("injected reapply failure", entry.comments)
        self.assertEqual((self._names(blocked), self._names(failed)), (["et-1/0/1"], ["et-1/0/2"]))
        self.assertEqual((_journal(blocked), _journal(failed)), ([], []))

    def test_the_subtree_reports_in_one_journal_entry_on_the_moved_module(self):
        card, port = self._install_card(self.card_type, self._bay(self.device))
        optic = self._install(self.optic_type, port)
        Interface.objects.create(device=self.remote, name="et-5/1/1", type=PLAIN_TYPE)

        with self.captureOnCommitCallbacks() as callbacks:
            self._save_move(card, self._bay(self.remote, "Bay 1"))
        self.assertEqual(sum(isinstance(callback, PlanRunner) for callback in callbacks), 1)
        for callback in callbacks:
            callback()

        self.assertEqual(self._names(optic), ["et-1/0/1"])
        (entry,) = _journal(card)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn("`et-1/0/1` to `et-5/1/1`", entry.comments)
        self.assertIn("already in use", entry.comments)
        self.assertEqual(_journal(optic), [])


class RuleWinnerMoveTest(ModuleMoveTestCase):
    """The previous state selects the rule that recognises the names; the new position, the rule that renames them."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.module_type = cls._module_type("Scoped", "{module}")
        cls.fixed_type = cls._module_type("Fixed", "mgmt")

    def _rule(self, template, module_type=None, **scope):
        return InterfaceNameRule.objects.create(
            module_type=module_type or self.module_type, name_template=template, **scope
        )

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_move_to_another_device_type_renames_with_its_rule(self):
        self._rule("a-{bay_position}", device_type=self.device_type)
        self._rule("b-{bay_position}", device_type=self.other_device_type)
        module = self._install(self.module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["a-0"])

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["b-1"])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_move_to_another_platform_renames_with_its_rule(self):
        self._rule("a-{bay_position}", platform=self.platform)
        self._rule("b-{bay_position}", platform=self.other_platform)
        module = self._install(self.module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["a-0"])

        self._move(module, self._bay(self.peer, "Bay 1"))

        self.assertEqual(self._names(module), ["b-1"])

    def test_a_move_to_another_parent_module_type_renames_with_its_rule(self):
        first_card = self._card_type("First Card", "1")
        second_card = self._card_type("Second Card", "1")
        self._rule("a-{bay_position}", parent_module_type=first_card)
        self._rule("b-{bay_position}", parent_module_type=second_card)
        _, first_port = self._install_card(first_card, self._bay(self.device))
        _, second_port = self._install_card(second_card, self._bay(self.device, "Bay 1"))
        module = self._install(self.module_type, first_port)
        self.assertEqual(self._names(module), ["a-1"])

        self._move(module, second_port)

        self.assertEqual(self._names(module), ["b-1"])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_without_a_rule_before_the_move_the_raw_names_are_renamed_as_an_install_does(self):
        self._rule("b-{bay_position}", device_type=self.other_device_type)
        module = self._install(self.module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["0"])

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["b-1"])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_raw_name_matched_by_the_current_and_the_previous_form_of_one_template_is_renamed(self):
        self._rule("{base}-{bay_position}", module_type=self.fixed_type, device_type=self.other_device_type)
        module = self._install(self.fixed_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["mgmt"])

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["mgmt-1"])

    def test_without_a_rule_after_the_move_the_names_the_old_rule_gave_stay_and_are_reported(self):
        module_type = self._module_type("Pair", "{module}", "mgmt")
        self._rule("a{base}", module_type=module_type, device_type=self.device_type)
        module = self._install(module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["a0", "amgmt"])
        rename_out_of_band(Interface.objects.get(module=module, name="amgmt"), "operator-name")

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["a0", "operator-name"])
        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn(f"`a0`: {NO_RULE}", entry.comments)
        self.assertNotIn("operator-name", entry.comments)

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_move_from_a_plain_rule_to_a_flat_breakout_rule_builds_the_family(self):
        self._rule("a-{bay_position}", device_type=self.device_type)
        self._rule(
            "b-{bay_position}:{channel}",
            device_type=self.other_device_type,
            breakout_mode=BreakoutModeChoices.FLAT,
            channel_count=2,
            channel_start=0,
        )
        module = self._install(self.module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["a-0"])

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["b-1:0", "b-1:1"])
        self.assertEqual(_journal(module), [])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_move_into_a_flat_rule_builds_no_family_while_an_interface_is_unclaimed(self):
        self._rule("a-{bay_position}", device_type=self.device_type)
        self._rule(
            "b-{bay_position}:{channel}",
            device_type=self.other_device_type,
            breakout_mode=BreakoutModeChoices.FLAT,
            channel_count=2,
            channel_start=0,
        )
        module = self._install(self.module_type, self._bay(self.device))
        Interface.objects.create(device=self.device, module=module, name="a-0:1", type=PLAIN_TYPE)

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["a-0", "a-0:1"])
        (entry,) = _journal(module)
        self.assertIn(f"`a-0`: {NOT_RENAMED}", entry.comments)
        self.assertIn(f"`a-0:1`: {UNCLAIMED}", entry.comments)

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_without_a_rule_after_the_move_the_channels_renamed_with_their_parent_are_reported(self):
        module_type = _channelized_module_type(
            self.manufacturer, f"{self.prefix} Lockstep", channels=2, child_channel_ids=(1, 2)
        )
        self._rule("et-{bay_position}", module_type=module_type, device_type=self.device_type)
        module = self._install(module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["et-0", "et-0:1", "et-0:2"])

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["et-0", "et-0:1", "et-0:2"])
        (entry,) = _journal(module)
        for name in ("et-0", "et-0:1", "et-0:2"):
            self.assertIn(f"`{name}`: {NO_RULE}", entry.comments)

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_without_a_rule_after_the_move_the_family_a_channelized_rule_built_is_reported(self):
        module_type = self._module_type("Built", "{module}")
        self._rule(
            "et-{bay_position}:{channel}",
            module_type=module_type,
            device_type=self.device_type,
            parent_name_template="et-{bay_position}",
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_count=2,
            channel_start=1,
        )
        module = self._install(module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["et-0", "et-0:1", "et-0:2"])

        self._move(module, self._bay(self.remote, "Bay 1"))

        self.assertEqual(self._names(module), ["et-0", "et-0:1", "et-0:2"])
        (entry,) = _journal(module)
        for name in ("et-0", "et-0:1", "et-0:2"):
            self.assertIn(f"`{name}`: {NO_RULE}", entry.comments)


class MoveRecognitionTest(ModuleMoveTestCase):
    """Templates claim names through their current and previous forms, under the fail-closed claim rule."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.base_type = cls._module_type("Base", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.base_type, name_template="p{base}-{vc_position}")
        cls.pair_type = cls._module_type("Pair", "{module}", "{module}0")
        InterfaceNameRule.objects.create(module_type=cls.pair_type, name_template="x{base}")
        cls.plain_type = cls._module_type("Plain", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.plain_type, name_template="et-{vc_position}/0/{bay_position}")

    def test_one_template_that_matches_two_interfaces_renames_neither(self):
        module = self._install(self.base_type, self._bay(self.device))
        Interface.objects.create(device=self.device, module=module, name="p1-1", type=PLAIN_TYPE)

        self._move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(self._names(module), ["p0-1", "p1-1"])
        (entry,) = _journal(module)
        self.assertIn(f"`p0-1`: {UNCLAIMED}", entry.comments)
        self.assertIn(f"`p1-1`: {UNCLAIMED}", entry.comments)

    def test_two_templates_that_match_one_interface_rename_nothing_and_are_reported(self):
        module = self._install(self.pair_type, self._bay(self.device, "Bay 1"))
        self.assertEqual(self._names(module), ["x1", "x10"])
        rename_out_of_band(Interface.objects.get(module=module, name="x1"), "operator-name")

        self._move(module, self._bay(self.device, "Bay 10"))

        self.assertEqual(self._names(module), ["operator-name", "x10"])
        (entry,) = _journal(module)
        self.assertIn(f"`x10`: {UNCLAIMED}", entry.comments)
        self.assertIn(f"`operator-name`: {UNCLAIMED}", entry.comments)

    def test_an_interface_of_a_module_type_without_templates_is_reported_after_a_move(self):
        module_type = make_module_type(self.manufacturer, "Bare", model=f"{self.prefix} Bare")
        InterfaceNameRule.objects.create(module_type=module_type, name_template="et-{bay_position}")
        module = self._install(module_type, self._bay(self.device))
        Interface.objects.create(device=self.device, module=module, name="operator-name", type=PLAIN_TYPE)

        self._move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(self._names(module), ["operator-name"])
        (entry,) = _journal(module)
        self.assertIn(f"`operator-name`: {UNCLAIMED}", entry.comments)

    def test_an_interface_no_template_matches_keeps_its_name_and_is_reported(self):
        module = self._install(self.plain_type, self._bay(self.device))
        rename_out_of_band(Interface.objects.get(module=module), "operator-name")

        self._move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(self._names(module), ["operator-name"])
        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn(f"`operator-name`: {UNCLAIMED}", entry.comments)

    def test_a_move_writes_only_the_module_the_rename_and_the_search_cache(self):
        module = self._install(self.plain_type, self._bay(self.device))

        with _naming_reads() as reads, CaptureQueriesContext(connection) as queries:
            self._move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(self._names(module), ["et-1/0/1"])
        ((read, _naming),) = reads
        self.assertEqual(_writes(read), [])
        writes = _writes(queries.captured_queries)
        self.assertEqual(
            {table for _statement, table in writes}, {"dcim_module", "dcim_interface", "extras_cachedvalue"}
        )
        self.assertEqual(
            [write for write in writes if write[1] != "extras_cachedvalue"],
            [("UPDATE", "dcim_module"), ("UPDATE", "dcim_interface")],
        )

    def test_a_save_that_moves_nothing_reads_no_naming(self):
        module = self._install(self.plain_type, self._bay(self.device))
        other_type = self._module_type("Other", "{module}")

        with _naming_reads() as reads, self.captureOnCommitCallbacks(execute=True):
            module.description = "unrelated edit"
            module.save()
            module.module_type = other_type
            module.save()
            self._save_move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(len(reads), 1)

    def test_a_naming_read_that_fails_fails_the_move_with_its_error(self):
        module = self._install(self.plain_type, self._bay(self.device))

        with (
            connection.execute_wrapper(_fail_the_naming_read),
            self.assertRaisesMessage(DataError, "division by zero"),
            transaction.atomic(),
        ):
            self._save_move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(Module.objects.get(pk=module.pk).module_bay, self._bay(self.device))
        self.assertEqual(self._names(module), ["et-1/0/0"])


class FlatBreakoutMoveTest(ModuleMoveTestCase):
    """A module whose rule before the move is a flat breakout rule is not renamed, and its interfaces are reported.

    NetBox keeps no link from an interface to its template or its family, so a flat family could be
    recognised only by name, and a move keeps whatever names NetBox left.
    """

    def _flat_rule(self, module_type, name_template, **scope):
        return InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template=name_template,
            breakout_mode=BreakoutModeChoices.FLAT,
            channel_count=2,
            channel_start=0,
            **scope,
        )

    def _move_and_reapply(self, module, bay):
        """Move *module* to *bay*, and return its names after NetBox's save, before the reapply runs."""
        with self.captureOnCommitCallbacks() as callbacks:
            self._save_move(module, bay)
        saved = self._names(module)
        for callback in callbacks:
            callback()
        return saved

    def _assert_kept_and_reported(self, module, names):
        self.assertEqual(self._names(module), names)
        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        for name in names:
            self.assertIn(f"`{name}`: {FLAT}", entry.comments)

    def test_a_flat_breakout_family_keeps_its_names_and_is_reported(self):
        module_type = self._module_type("Flat", "{module}")
        self._flat_rule(module_type, "et-{vc_position}/{bay_position}:{channel}")
        module = self._install(module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["et-1/0:0", "et-1/0:1"])

        self._move(module, self._bay(self.device, "Bay 2"))

        self._assert_kept_and_reported(module, ["et-1/0:0", "et-1/0:1"])

    def test_a_family_netbox_renames_in_part_gets_no_second_family(self):
        module_type = self._module_type("Native Flat", "{module}:0")
        self._flat_rule(module_type, "{bay_position}:{channel}")
        module = self._install(module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["0:0", "0:1"])

        saved = self._move_and_reapply(module, self._bay(self.device, "Bay 1"))

        self._assert_kept_and_reported(module, saved)

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_second_move_under_a_simple_rule_does_not_split_the_family(self):
        module_type = self._module_type("Port", "port")
        self._flat_rule(module_type, "p{bay_position}:{channel}", device_type=self.device_type)
        InterfaceNameRule.objects.create(
            module_type=module_type, device_type=self.other_device_type, name_template="p{bay_position}:0"
        )
        module = self._install(module_type, self._bay(self.device, "Bay 1"))
        self._move(module, self._bay(self.remote, "Bay 1"))
        self.assertEqual(self._names(module), ["p1:0", "p1:1"])

        self._move(module, self._bay(self.remote, "Bay 2"))

        self.assertEqual(self._names(module), ["p1:0", "p1:1"])
        entry = _journal(module)[-1]
        self.assertIn(f"`p1:0`: {NOT_RENAMED}", entry.comments)
        self.assertIn(f"`p1:1`: {UNCLAIMED}", entry.comments)

    def test_an_install_and_a_move_in_one_transaction_build_the_family_of_a_flat_rule(self):
        module_type = self._module_type("Installed Flat", "{module}")
        self._flat_rule(module_type, "f-{bay_position}:{channel}")

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            module = Module.objects.create(
                device=self.device, module_bay=self._bay(self.device), module_type=module_type
            )
            self._save_move(module, self._bay(self.device, "Bay 2"))

        self.assertEqual(self._names(module), ["f-2:0", "f-2:1"])
        self.assertEqual(_journal(module), [])

    @skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)
    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_move_from_a_flat_rule_to_a_simple_rule_renames_no_member(self):
        module_type = self._module_type("Flat To Simple", "{vc_position}")
        self._flat_rule(module_type, "{base}:{channel}", platform=self.platform)
        InterfaceNameRule.objects.create(
            module_type=module_type, platform=self.other_platform, name_template="{base}:0"
        )
        module = self._install(module_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["1:0", "1:1"])

        self._move(module, self._bay(self.peer, "Bay 1"))

        self._assert_kept_and_reported(module, ["1:0", "1:1"])


class MoveTransactionTest(ModuleMoveTestCase):
    """Moves in one transaction coalesce into one reapply from the state before the transaction."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.plain_type = cls._module_type("Plain", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.plain_type, name_template="et-{vc_position}/0/{bay_position}")

    def test_a_move_in_a_rolled_back_savepoint_causes_no_reapply_and_a_later_move_reapplies_once(self):
        module = self._install(self.plain_type, self._bay(self.device))

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._save_move(module, self._bay(self.device, "Bay 1"))
                raise RuntimeError("roll back the savepoint")
            module.refresh_from_db()
            self._save_move(module, self._bay(self.device, "Bay 2"))

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-1/0/2"])

    def test_a_move_rolled_back_after_an_earlier_move_keeps_the_earlier_reapply(self):
        module = self._install(self.plain_type, self._bay(self.device))

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(module, self._bay(self.device, "Bay 2"))
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._save_move(module, self._bay(self.device, "Bay 1"))
                raise RuntimeError("roll back the savepoint")

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-1/0/2"])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_two_moves_in_one_transaction_reapply_once_from_the_state_before_it(self):
        module = self._install(self.plain_type, self._bay(self.device))

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(module, self._bay(self.device, "Bay 1"))
            self._save_move(module, self._bay(self.peer, "Bay 2"))

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-2/0/2"])

    def test_a_move_the_same_transaction_undoes_reapplies_nothing(self):
        returned = self._install(self.plain_type, self._bay(self.device))
        rename_out_of_band(Interface.objects.get(module=returned), "operator-name")
        moved = self._install(self.plain_type, self._bay(self.device, "Bay 1"))

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(returned, self._bay(self.device, "Bay 2"))
            self._save_move(returned, self._bay(self.device))
            self._save_move(moved, self._bay(self.device, "Bay 10"))

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual((self._names(returned), self._names(moved)), (["operator-name"], ["et-1/0/10"]))

    def test_a_bay_edited_before_the_move_in_one_transaction_is_renamed_from_the_state_before_the_edit(self):
        module = self._install(self.plain_type, self._bay(self.device))
        bay = self._bay(self.device)

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            bay.position = "5"
            bay.save()
            self._save_move(module, self._bay(self.device, "Bay 1"))

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-1/0/1"])
        self.assertEqual(_journal(module), [])


class ChassisPositionMoveTest(ModuleMoveTestCase):
    """Moves and a virtual-chassis change of the old device in one transaction, in either order.

    A move recognises the names from the device's position before the change, also after a move to
    another device, for each of ``CHASSIS_RULES``. The device reapply renames the modules that did not
    move. Each module is reapplied once.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.module_types = []
        for rule in CHASSIS_RULES:
            module_type = cls._module_type(rule.model, "{module}")
            InterfaceNameRule.objects.create(module_type=module_type, name_template=rule.name_template)
            cls.module_types.append(module_type)
        for position in ("5", "6", "7"):
            ModuleBay.objects.create(device=cls.device, name=f"Bay {position}", position=position)

    def _move_all(self, targets, device_change, device_first):
        """Install a module of each rule and a plain one in Bay 10, then run *device_change* and move each rule's module.

        Return the names of each moved module and the names of the module in Bay 10.
        """
        modules = [
            self._install(module_type, self._bay(self.device, f"Bay {index}"))
            for index, module_type in enumerate(self.module_types)
        ]
        self.assertEqual([self._names(module) for module in modules], [["et-1/0/0"], ["p1-1/1"], ["x12/2"]])
        other = self._install(self.module_types[0], self._bay(self.device, "Bay 10"))

        def move_all():
            for module, target in zip(modules, targets, strict=True):
                self._save_move(module, target)

        reapplies = self._save_with_a_device_change(device_change, move_all, device_first)

        self.assertEqual(_reapplied(reapplies), sorted(module.pk for module in (*modules, other)))
        self.assertEqual([_journal(module) for module in modules], [[], [], []])
        return [self._names(module) for module in modules], self._names(other)

    def _device_bays(self):
        return [self._bay(self.device, f"Bay {position}") for position in ("5", "6", "7")]

    def _peer_bays(self):
        return [self._bay(self.peer, f"Bay {position}") for position in ("0", "1", "2")]

    def _assert_moved_with_a_position_change(self, targets, chassis_first, names):
        moved, other = self._move_all(targets, self._change_the_chassis_position, chassis_first)

        self.assertEqual((moved, other), (names, ["et-3/10/10"]))
        self.assertEqual(_journal(self.device), [])

    def test_moves_then_a_chassis_position_change_rename_each_module_once(self):
        self._assert_moved_with_a_position_change(self._device_bays(), False, [["et-3/5/5"], ["p6-3/6"], ["x37/7"]])

    def test_a_chassis_position_change_then_moves_rename_each_module_once(self):
        self._assert_moved_with_a_position_change(self._device_bays(), True, [["et-3/5/5"], ["p6-3/6"], ["x37/7"]])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_moves_out_of_the_device_then_a_chassis_position_change_rename_each_module_once(self):
        self._assert_moved_with_a_position_change(self._peer_bays(), False, [["et-2/0/0"], ["p1-2/1"], ["x22/2"]])

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_a_chassis_position_change_then_moves_out_of_the_device_recognise_the_names_before_it(self):
        self._assert_moved_with_a_position_change(self._peer_bays(), True, [["et-2/0/0"], ["p1-2/1"], ["x22/2"]])

    def _assert_moved_out_with_a_leave(self, leave_first):
        moved, other = self._move_all(self._peer_bays(), self._leave_the_chassis, leave_first)

        self.assertEqual((moved, other), ([["et-2/0/0"], ["p1-2/1"], ["x22/2"]], ["et-1/10/10"]))
        (entry,) = _journal(self.device)
        self.assertIn(f"`et-1/10/10`: {UNAVAILABLE}", entry.comments)

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_moves_out_of_the_device_then_leaving_the_chassis_rename_each_module_once(self):
        self._assert_moved_out_with_a_leave(leave_first=False)

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_DEVICE_MOVES)
    def test_leaving_the_chassis_then_moves_out_of_the_device_recognise_the_names_before_it(self):
        self._assert_moved_out_with_a_leave(leave_first=True)

    def test_a_move_the_transaction_undoes_leaves_the_module_to_the_chassis_position_change(self):
        module = self._install(self.module_types[0], self._bay(self.device))

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(module, self._bay(self.device, "Bay 5"))
            self._change_the_chassis_position()
            self._save_move(module, self._bay(self.device))

        self.assertEqual((self._names(module), _reapplied(reapplies)), (["et-3/0/0"], [module.pk]))
        self.assertEqual((_journal(module), _journal(self.device)), ([], []))

    def _assert_a_collision_is_reported_once(self, chassis_first):
        module = self._install(self.module_types[0], self._bay(self.device))
        Interface.objects.create(device=self.device, name="et-3/5/5", type=PLAIN_TYPE)

        reapplies = self._save_with_a_device_change(
            self._change_the_chassis_position,
            functools.partial(self._save_move, module, self._bay(self.device, "Bay 5")),
            chassis_first,
        )

        self.assertEqual((self._names(module), _reapplied(reapplies)), (["et-1/0/0"], [module.pk]))
        (entry,) = _journal(module)
        self.assertEqual(entry.comments.count(f"`et-1/0/0` to `et-3/5/5`: {TAKEN}"), 1)
        self.assertEqual(_journal(self.device), [])

    def test_a_move_then_a_chassis_position_change_report_a_collision_once(self):
        self._assert_a_collision_is_reported_once(chassis_first=False)

    def test_a_chassis_position_change_then_a_move_report_a_collision_once(self):
        self._assert_a_collision_is_reported_once(chassis_first=True)


@skipIf(NETBOX_MOVES_COMPONENTS, "NetBox 4.7 moves a module's components and nested bays with it")
class ModuleRowMoveTest(ModuleMoveTestCase):
    """Before 4.7 NetBox writes a moved module's row alone.

    The interfaces keep their raw names from the old bay, and the nested bays keep their parent. The
    reapply recognises the moved module's raw names from its previous state; the nested modules keep
    the variables their names came from, but the old parent bay no longer holds their parent module, so
    nothing renames them. After a move to another device the interfaces stay on the old device, so
    nothing renames them either.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.plain_type = cls._module_type("Plain", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.plain_type, name_template="et-{vc_position}/0/{bay_position}")

    def test_a_move_renames_the_moved_module_from_its_old_raw_names_and_reports_the_nested_modules(self):
        card_type = self._card_type("Card", "1")
        InterfaceTemplate.objects.create(module_type=card_type, name="c{module}", type=PLAIN_TYPE)
        optic_type = self._module_type("Optic", "{module}")
        InterfaceNameRule.objects.create(module_type=optic_type, name_template="et-{vc_position}/{slot}/{bay_position}")
        card, port = self._install_card(card_type, self._bay(self.device))
        optic = self._install(optic_type, port)
        InterfaceNameRule.objects.create(module_type=card_type, name_template="ge-{vc_position}/{bay_position}")

        with self.captureOnCommitCallbacks() as callbacks:
            self._save_move(card, self._bay(self.device, "Bay 2"))
        port.refresh_from_db()
        self.assertEqual((self._names(card), port.parent_id), (["c0"], self._bay(self.device).pk))
        for callback in callbacks:
            callback()

        self.assertEqual((self._names(card), self._names(optic)), (["ge-1/2"], ["et-1/0/1"]))
        (entry,) = _journal(card)
        self.assertIn(f"`et-1/0/1`: {STALE_BAY}", entry.comments)
        self.assertNotIn("ge-1/2", entry.comments)

    def _scoped_optic(self):
        """Install a card in Bay 0 with an optic whose rule the card's type scopes; return both."""
        card_type = self._card_type("Scoping Card", "1")
        optic_type = self._module_type("Scoped Optic", "{module}")
        InterfaceNameRule.objects.create(
            module_type=optic_type, parent_module_type=card_type, name_template="a-{bay_position}"
        )
        InterfaceNameRule.objects.create(module_type=optic_type, name_template="b-{bay_position}")
        card, port = self._install_card(card_type, self._bay(self.device))
        optic = self._install(optic_type, port)
        self.assertEqual(self._names(optic), ["a-1"])
        return card, optic

    def test_a_nested_module_whose_bay_keeps_the_old_parent_is_not_renamed_by_another_rule(self):
        card, optic = self._scoped_optic()

        self._move(card, self._bay(self.device, "Bay 2"))

        self.assertEqual(self._names(optic), ["a-1"])
        (entry,) = _journal(card)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn(f"`a-1`: {STALE_BAY}", entry.comments)

    def test_a_position_change_after_a_move_does_not_rename_a_nested_module_whose_bay_keeps_the_old_parent(self):
        card, optic = self._scoped_optic()
        with self.captureOnCommitCallbacks():
            self._save_move(card, self._bay(self.device, "Bay 2"))

        with self.captureOnCommitCallbacks(execute=True):
            self.device.vc_position = 3
            self.device.save()

        self.assertEqual(self._names(optic), ["a-1"])
        (entry,) = _journal(self.device)
        self.assertIn(f"`a-1`: {STALE_BAY}", entry.comments)

    def test_a_move_to_another_device_renames_nothing_while_the_interfaces_stay_on_the_old_device(self):
        module = self._install(self.plain_type, self._bay(self.device))
        self.assertEqual(self._names(module), ["et-1/0/0"])

        self._move(module, self._bay(self.peer, "Bay 2"))

        interface = Interface.objects.get(module=module)
        self.assertEqual((interface.device, interface.name), (self.device, "et-1/0/0"))
        (entry,) = _journal(module)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn(f"`et-1/0/0`: {ELSEWHERE}", entry.comments)

    def test_a_position_change_of_the_new_device_renames_nothing_while_the_interfaces_stay_on_the_old_device(self):
        module = self._install(self.plain_type, self._bay(self.device))
        self._move(module, self._bay(self.peer, "Bay 2"))

        with self.captureOnCommitCallbacks(execute=True):
            self.peer.vc_position = 3
            self.peer.save()

        interface = Interface.objects.get(module=module)
        self.assertEqual((interface.device, interface.name), (self.device, "et-1/0/0"))
        (entry,) = _journal(self.peer)
        self.assertIn(f"`et-1/0/0`: {ELSEWHERE}", entry.comments)


class ModuleMoveAPITest(_MoveFixture, APITestCase):
    """A REST API move reaches the rename trigger through NetBox's own write path."""

    model = Module
    user_permissions = ("dcim.view_module", "dcim.change_module", "dcim.view_modulebay", "dcim.view_device")

    @classmethod
    def setUpTestData(cls):
        cls.build("MoveApi")
        cls.module_type = cls._module_type("Plain", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.module_type, name_template="et-{vc_position}/0/{bay_position}")

    def test_patching_the_module_bay_renames_the_interfaces_for_the_new_bay(self):
        with self.captureOnCommitCallbacks(execute=True):
            module = Module.objects.create(
                device=self.device, module_bay=self._bay(self.device), module_type=self.module_type
            )
        url = reverse("dcim-api:module-detail", kwargs={"pk": module.pk})

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                url, {"module_bay": self._bay(self.device, "Bay 1").pk}, format="json", **self.header
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._names(module), ["et-1/0/1"])
        self.assertEqual(_journal(module), [])
