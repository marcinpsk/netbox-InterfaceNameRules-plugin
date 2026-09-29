# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""A module type change renames the modules nested in the module, driven through real saves.

A rule can be scoped to a parent module type, so a module nested in a module whose type changes can
get another rule. When an enabled rule is scoped to the old or the new type as a parent module type,
the type change reads before the save what named the interfaces of each nested module. After commit
each nested module whose rule changed is renamed from that naming, and reports on the changed module.
"""

import functools
from unittest import skipUnless

from dcim.models import Interface, Module, ModuleBay, ModuleBayTemplate
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from extras.choices import JournalEntryKindChoices
from rest_framework import status
from utilities.testing import APITestCase

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.committed_callbacks import run_the_reapply
from netbox_interface_name_rules.tests.test_bay_edit_trigger import BayEditTestCase, _flat_rule
from netbox_interface_name_rules.tests.test_module_move_trigger import (
    FLAT,
    NAMING_READ,
    NETBOX_MOVES_COMPONENTS,
    NO_RULE,
    PLAIN_TYPE,
    REQUIRES_SUBTREE_MOVES,
    TAKEN,
    _journal,
    _MoveFixture,
    _reapplied,
)
from netbox_interface_name_rules.tests.test_rename_triggers import _reject_reads_of


class _CardFixture(_MoveFixture):
    """Two card types with two ports each, and an optic whose rule each card type scopes as its parent."""

    @classmethod
    def build_cards(cls):
        """Create the card types, the optic type and the two scoped optic rules."""
        cls.first_card_type = cls._two_port_card("First Card")
        cls.second_card_type = cls._two_port_card("Second Card")
        cls.optic_type = cls._module_type("Optic", "{module}")
        for card_type, prefix in ((cls.first_card_type, "a"), (cls.second_card_type, "b")):
            InterfaceNameRule.objects.create(
                module_type=cls.optic_type,
                parent_module_type=card_type,
                name_template=f"{prefix}-{{slot}}/{{bay_position}}",
            )

    @classmethod
    def _two_port_card(cls, model):
        card_type = cls._card_type(model, "1")
        ModuleBayTemplate.objects.create(module_type=card_type, name="Port 2", position="2")
        return card_type

    @staticmethod
    def _ports(card):
        return ModuleBay.objects.filter(module=card).order_by("position")

    @staticmethod
    def _save_type(module, module_type):
        module.module_type = module_type
        module.save()


class TypeChangeTestCase(_CardFixture, BayEditTestCase):
    """Install cards with optics and change the card type through real saves, with the committed callbacks run."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_cards()

    def _card_with_optic(self, card_type=None):
        """Install a card in Bay 0 with an optic in its first port; return the card, its second port and the optic."""
        card = self._install(card_type or self.first_card_type, self._bay(self.device))
        port, second_port = self._ports(card)
        optic = self._install(self.optic_type, port)
        return card, second_port, optic

    def _change_type(self, module, module_type):
        with self.captureOnCommitCallbacks(execute=True):
            self._save_type(module, module_type)


class NestedTypeChangeTest(TypeChangeTestCase):
    """A type change renames each nested module whose rule the new parent module type changes."""

    def test_a_nested_module_whose_rule_changes_is_renamed_from_the_names_the_old_rule_gave(self):
        card, _second_port, optic = self._card_with_optic()
        self.assertEqual(self._names(optic), ["a-0/1"])

        self._change_type(card, self.second_card_type)

        self.assertEqual(self._names(optic), ["b-0/1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_a_nested_module_whose_rule_does_not_change_keeps_its_names_and_is_not_reported(self):
        card, second_port, optic = self._card_with_optic()
        fixed = self._install(self.plain_type, second_port)
        Interface.objects.create(device=self.device, module=fixed, name="operator-name", type=PLAIN_TYPE)
        self.assertEqual(self._names(fixed), ["et-1/0/2", "operator-name"])

        self._change_type(card, self.second_card_type)

        self.assertEqual((self._names(optic), self._names(fixed)), (["b-0/1"], ["et-1/0/2", "operator-name"]))
        self.assertEqual((_journal(card), _journal(optic), _journal(fixed)), ([], [], []))

    def test_a_module_two_levels_down_keeps_its_names_and_is_not_reported(self):
        card, second_port, optic = self._card_with_optic()
        sub_card = self._install(self._card_type("Sub Card", "3"), second_port)
        deep_optic_type = self._module_type("Deep Optic", "{module}")
        InterfaceNameRule.objects.create(
            module_type=deep_optic_type, parent_module_type=sub_card.module_type, name_template="g-{bay_position}"
        )
        deep = self._install(deep_optic_type, ModuleBay.objects.get(module=sub_card))
        Interface.objects.create(device=self.device, module=deep, name="operator-name", type=PLAIN_TYPE)
        self.assertEqual(self._names(deep), ["g-3", "operator-name"])

        self._change_type(card, self.second_card_type)

        self.assertEqual((self._names(optic), self._names(deep)), (["b-0/1"], ["g-3", "operator-name"]))
        self.assertEqual((_journal(card), _journal(sub_card), _journal(deep)), ([], [], []))

    def test_a_nested_module_left_without_a_rule_keeps_the_names_the_old_rule_gave_and_is_reported(self):
        card, _second_port, optic = self._card_with_optic()

        self._change_type(card, self._two_port_card("Bare Card"))

        self.assertEqual(self._names(optic), ["a-0/1"])
        (entry,) = _journal(card)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn(f"`a-0/1`: {NO_RULE}", entry.comments)
        self.assertEqual(_journal(optic), [])

    def test_a_nested_flat_breakout_family_keeps_its_names_and_is_reported(self):
        flat_optic_type = self._module_type("Flat Optic", "{module}")
        _flat_rule(flat_optic_type, "x-{slot}/{bay_position}:{channel}", parent_module_type=self.first_card_type)
        InterfaceNameRule.objects.create(
            module_type=flat_optic_type,
            parent_module_type=self.second_card_type,
            name_template="y-{slot}/{bay_position}",
        )
        card = self._install(self.first_card_type, self._bay(self.device))
        optic = self._install(flat_optic_type, self._ports(card)[0])
        self.assertEqual(self._names(optic), ["x-0/1:0", "x-0/1:1"])

        self._change_type(card, self.second_card_type)

        self.assertEqual(self._names(optic), ["x-0/1:0", "x-0/1:1"])
        (entry,) = _journal(card)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        for name in ("x-0/1:0", "x-0/1:1"):
            self.assertIn(f"`{name}`: {FLAT}", entry.comments)
        self.assertEqual(_journal(optic), [])

    def test_the_subtree_reports_in_one_journal_entry_on_the_module_whose_type_changed(self):
        card, second_port, optic = self._card_with_optic()
        other_optic = self._install(self.optic_type, second_port)
        for name in ("b-0/1", "b-0/2"):
            Interface.objects.create(device=self.device, name=name, type=PLAIN_TYPE)

        self._change_type(card, self.second_card_type)

        self.assertEqual((self._names(optic), self._names(other_optic)), (["a-0/1"], ["a-0/2"]))
        (entry,) = _journal(card)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn(f"`a-0/1` to `b-0/1`: {TAKEN}", entry.comments)
        self.assertIn(f"`a-0/2` to `b-0/2`: {TAKEN}", entry.comments)
        self.assertEqual((_journal(optic), _journal(other_optic)), ([], []))


class TypeChangeTransactionTest(TypeChangeTestCase):
    """A bay edit or a move after a type change in one transaction recognises the names from before the type change."""

    def test_a_type_change_then_a_bay_edit_rename_the_nested_module_from_the_names_before_the_type_change(self):
        card, _second_port, optic = self._card_with_optic()
        bay = self._bay(self.device)

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_type(card, self.second_card_type)
            self._save_edit(bay, position="2")

        self.assertEqual(self._names(optic), ["b-2/1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_SUBTREE_MOVES)
    def test_a_type_change_then_a_move_rename_the_nested_module_from_the_names_before_the_type_change(self):
        card, _second_port, optic = self._card_with_optic()

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_type(card, self.second_card_type)
            self._save_move(card, self._bay(self.device, "Bay 2"))

        self.assertEqual(self._names(optic), ["b-2/1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))


class ChassisPositionTypeChangeTest(TypeChangeTestCase):
    """A type change and a virtual-chassis position change of the device in one transaction, in either order.

    The type change renames a nested module from a naming that holds the device's position before the
    change, also for a rule with the position in arithmetic. Each module is reapplied once and reports once.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.arithmetic_optic_type = cls._module_type("Arithmetic Optic", "{module}")
        for card_type, prefix in ((cls.first_card_type, "x"), (cls.second_card_type, "y")):
            InterfaceNameRule.objects.create(
                module_type=cls.arithmetic_optic_type,
                parent_module_type=card_type,
                name_template=prefix + "{{vc_position} * 10 + {slot_num}}/{bay_position}",
            )
        cls.other_plain_type = cls._module_type("Other Plain", "{module}")
        InterfaceNameRule.objects.create(
            module_type=cls.other_plain_type, name_template="ge-{vc_position}/0/{bay_position}"
        )

    def _retype_with_the_chassis_change(self, module, module_type, chassis_first):
        """Change the type of *module* and the device's position in one transaction; return the reapply spy."""
        return self._save_with_a_device_change(
            self._change_the_chassis_position, functools.partial(self._save_type, module, module_type), chassis_first
        )

    def _assert_the_nested_module_is_renamed_once(self, chassis_first):
        card = self._install(self.first_card_type, self._bay(self.device))
        optic = self._install(self.arithmetic_optic_type, self._ports(card)[0])
        other = self._install(self.plain_type, self._bay(self.device, "Bay 10"))
        self.assertEqual(self._names(optic), ["x10/1"])

        reapplies = self._retype_with_the_chassis_change(card, self.second_card_type, chassis_first)

        self.assertEqual((self._names(optic), self._names(other)), (["y30/1"], ["et-3/0/10"]))
        self.assertEqual(_reapplied(reapplies), sorted((card.pk, optic.pk, other.pk)))
        self.assertEqual((_journal(card), _journal(optic), _journal(self.device)), ([], [], []))

    def test_a_type_change_then_a_chassis_position_change_rename_the_nested_module_once(self):
        self._assert_the_nested_module_is_renamed_once(chassis_first=False)

    def test_a_chassis_position_change_then_a_type_change_rename_the_nested_module_once(self):
        self._assert_the_nested_module_is_renamed_once(chassis_first=True)

    def test_a_nested_module_whose_rule_does_not_change_is_renamed_for_the_chassis_position_change(self):
        card, second_port, optic = self._card_with_optic()
        fixed = self._install(self.plain_type, second_port)
        self.assertEqual((self._names(optic), self._names(fixed)), (["a-0/1"], ["et-1/0/2"]))

        reapplies = self._retype_with_the_chassis_change(card, self.second_card_type, chassis_first=True)

        self.assertEqual((self._names(optic), self._names(fixed)), (["b-0/1"], ["et-3/0/2"]))
        self.assertEqual(_reapplied(reapplies), sorted((card.pk, optic.pk, fixed.pk)))
        self.assertEqual((_journal(card), _journal(fixed), _journal(self.device)), ([], [], []))

    def _assert_a_collision_is_reported_once(self, chassis_first):
        module = self._install(self.plain_type, self._bay(self.device))
        Interface.objects.create(device=self.device, name="ge-3/0/0", type=PLAIN_TYPE)

        reapplies = self._retype_with_the_chassis_change(module, self.other_plain_type, chassis_first)

        self.assertEqual((self._names(module), _reapplied(reapplies)), (["et-1/0/0"], [module.pk]))
        (entry,) = _journal(module)
        self.assertEqual(entry.comments.count(f"`et-1/0/0` to `ge-3/0/0`: {TAKEN}"), 1)
        self.assertEqual(_journal(self.device), [])

    def test_a_type_change_then_a_chassis_position_change_report_a_collision_once(self):
        self._assert_a_collision_is_reported_once(chassis_first=False)

    def test_a_chassis_position_change_then_a_type_change_report_a_collision_once(self):
        self._assert_a_collision_is_reported_once(chassis_first=True)


class TypeChangeFailureTest(TypeChangeTestCase):
    """A reapply that cannot read the rules reports one failure per module, and the later modules still reapply."""

    def test_a_rule_read_failure_is_reported_for_each_module_and_the_later_modules_still_report(self):
        card, _second_port, optic = self._card_with_optic()
        other = self._install(self.plain_type, self._bay(self.device, "Bay 1"))
        with self.captureOnCommitCallbacks() as callbacks, transaction.atomic():
            self._save_type(card, self.second_card_type)
            self._save_move(other, self._bay(self.device, "Bay 2"))
        failure = f"injected {InterfaceNameRule._meta.db_table} read failure"

        with (
            connection.execute_wrapper(_reject_reads_of(InterfaceNameRule._meta.db_table)),
            self.assertLogs("netbox_interface_name_rules", "ERROR"),
        ):
            run_the_reapply(callbacks)

        (card_entry,) = _journal(card)
        self.assertEqual(card_entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertEqual(card_entry.comments.count(failure), 2)
        (other_entry,) = _journal(other)
        self.assertEqual(other_entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertEqual(other_entry.comments.count(failure), 1)
        self.assertEqual((self._names(optic), self._names(other), _journal(optic)), (["a-0/1"], ["et-1/0/1"], []))


class TypeChangeCostTest(TypeChangeTestCase):
    """A type change reads the nested naming only when an enabled rule is scoped to the old or the new parent type."""

    def _type_change_reads(self, card, module_type):
        """Change the type of *card*; return the naming reads and the rule reads of the save, not of the reapply."""
        with self.captureOnCommitCallbacks(execute=True), CaptureQueriesContext(connection) as queries:
            self._save_type(card, module_type)
        statements = [query["sql"] for query in queries.captured_queries]
        rule_table = f'FROM "{InterfaceNameRule._meta.db_table}"'
        return sum(bool(NAMING_READ.match(sql)) for sql in statements), sum(rule_table in sql for sql in statements)

    def test_a_type_change_reads_no_nested_naming_without_an_enabled_rule_scoped_to_either_type(self):
        old_type, new_type = self._two_port_card("Old Card"), self._two_port_card("New Card")
        InterfaceNameRule.objects.create(module_type=self.optic_type, name_template="u-{slot}/{bay_position}")
        InterfaceNameRule.objects.create(
            module_type=self.optic_type, parent_module_type=old_type, name_template="d-{bay_position}", enabled=False
        )
        card, _second_port, optic = self._card_with_optic(old_type)
        self.assertEqual(self._names(optic), ["u-0/1"])

        reads = self._type_change_reads(card, new_type)

        self.assertEqual(reads, (0, 1))
        self.assertEqual(self._names(optic), ["u-0/1"])
        self.assertEqual(_journal(card), [])

    def test_a_type_change_reads_the_nested_naming_when_an_enabled_rule_is_scoped_to_the_new_type(self):
        card, _second_port, optic = self._card_with_optic()

        naming_reads, rule_reads = self._type_change_reads(card, self.second_card_type)

        self.assertGreater(naming_reads, 0)
        self.assertEqual(rule_reads, 1)
        self.assertEqual(self._names(optic), ["b-0/1"])


class TypeChangeAPITest(_CardFixture, APITestCase):
    """A REST API type change reaches the rename trigger through NetBox's own write path."""

    model = Module
    user_permissions = ("dcim.view_module", "dcim.change_module", "dcim.view_moduletype", "dcim.view_device")

    @classmethod
    def setUpTestData(cls):
        cls.build("TypeChangeApi")
        cls.build_cards()

    def test_patching_the_module_type_renames_the_nested_module(self):
        with self.captureOnCommitCallbacks(execute=True):
            card = Module.objects.create(
                device=self.device, module_bay=self._bay(self.device), module_type=self.first_card_type
            )
        with self.captureOnCommitCallbacks(execute=True):
            optic = Module.objects.create(
                device=self.device, module_bay=self._ports(card)[0], module_type=self.optic_type
            )
        self.assertEqual(self._names(optic), ["a-0/1"])
        url = reverse("dcim-api:module-detail", kwargs={"pk": card.pk})

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, {"module_type": self.second_card_type.pk}, format="json", **self.header)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._names(optic), ["b-0/1"])
        self.assertEqual(_journal(card), [])
