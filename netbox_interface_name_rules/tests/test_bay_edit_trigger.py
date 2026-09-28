# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""An edit of an occupied module bay's position or name is a rename trigger, driven through real bay saves.

The previous state of a bay edit is read before the save: the bay's position and name, and what named
the interfaces of the module in the bay and of every module nested in it. After commit the reapply
recognises the names that state gave and renames them for the edited bay. NetBox itself renames no
interface when a bay's position or name changes.
"""

import re
from unittest import skipUnless

from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay, ModuleBayTemplate
from django.db import DataError, connection, transaction
from django.urls import reverse
from extras.choices import JournalEntryKindChoices
from rest_framework import status
from utilities.testing import APITestCase

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.rename_triggers import ModuleTrigger, ReapplyPlan
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_module_move_trigger import (
    NETBOX_MOVES_COMPONENTS,
    PLAIN_TYPE,
    REQUIRES_SUBTREE_MOVES,
    ModuleMoveTestCase,
    _fail_the_naming_read,
    _journal,
    _module_reapplies,
    _MoveFixture,
    _naming_reads,
    _reject_interface_updates,
)
from netbox_interface_name_rules.tests.test_rename_triggers import _previous_state_read_fails

BAY_STATE_READ = re.compile(r'SELECT "dcim_modulebay"\."position".* FROM "dcim_modulebay"')


def _flat_rule(module_type, name_template, **scope):
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


class BayEditTest(BayEditTestCase):
    """The module in an edited bay gets the names its rule gives for the bay's new position or name."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.base_type = cls._module_type("Base", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.base_type, name_template="p{base}-{vc_position}")
        cls.fixed_type = cls._module_type("Fixed", "port")
        InterfaceNameRule.objects.create(module_type=cls.fixed_type, name_template="ge-{vc_position}/0/{bay_position}")
        ModuleBay.objects.create(device=cls.device, name="Bay 3", position="{module}")

    def test_a_position_edit_renames_the_module_for_the_new_position(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)
        self.assertEqual(self._names(module), ["et-1/0/0"])

        self._edit(bay, position="5")

        self.assertEqual(self._names(module), ["et-1/0/5"])
        self.assertEqual(_journal(module), [])

    def test_a_base_rule_is_renamed_from_the_raw_name_the_old_position_gave(self):
        bay = self._bay(self.device)
        module = self._install(self.base_type, bay)
        self.assertEqual(self._names(module), ["p0-1"])

        self._edit(bay, position="5")

        self.assertEqual(self._names(module), ["p5-1"])

    def test_a_name_edit_renames_the_module_when_its_position_takes_the_number_from_the_name(self):
        bay = self._bay(self.device, "Bay 3")
        module = self._install(self.fixed_type, bay)
        self.assertEqual(self._names(module), ["ge-1/0/3"])

        self._edit(bay, name="Bay 7")

        self.assertEqual(self._names(module), ["ge-1/0/7"])
        self.assertEqual(_journal(module), [])

    def test_a_name_edit_that_no_variable_reads_changes_nothing(self):
        numbered = self._install(self.plain_type, self._bay(self.device))
        Interface.objects.create(device=self.device, module=numbered, name="operator-name", type=PLAIN_TYPE)
        named = self._install(self.fixed_type, self._bay(self.device, "Bay 3"))
        control = self._install(self.plain_type, self._bay(self.device, "Bay 1"))

        with _naming_reads() as reads, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(self._bay(self.device), name="Uplink 0")
            self._save_edit(self._bay(self.device, "Bay 3"), name="Slot 3")
            self._save_edit(self._bay(self.device, "Bay 1"), position="4")

        self.assertEqual([naming[0].module_pk for _queries, naming in reads], [control.pk])
        self.assertEqual(
            (self._names(numbered), self._names(named), self._names(control)),
            (["et-1/0/0", "operator-name"], ["ge-1/0/3"], ["et-1/0/4"]),
        )
        self.assertEqual((_journal(numbered), _journal(named)), ([], []))

    def test_only_an_occupied_bay_whose_position_or_name_changes_reads_the_naming(self):
        occupied = self._bay(self.device)
        module = self._install(self.plain_type, occupied)
        control = self._install(self.plain_type, self._bay(self.device, "Bay 1"))

        with (
            _naming_reads() as reads,
            _module_reapplies() as reapplies,
            self.captureOnCommitCallbacks(execute=True),
            transaction.atomic(),
        ):
            self._save_edit(occupied, label="Uplink", description="unrelated edit")
            self._save_edit(self._bay(self.device, "Bay 2"), position="7", name="Bay 7")
            unused_pk = ModuleBay.objects.latest("pk").pk + 1000
            ModuleBay.objects.create(pk=unused_pk, device=self.device, name="Bay 20", position="20")
            self._save_edit(self._bay(self.device, "Bay 1"), position="4")

        self.assertEqual([naming[0].module_pk for _queries, naming in reads], [control.pk])
        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual((self._names(module), self._names(control)), (["et-1/0/0"], ["et-1/0/4"]))

    def test_a_subinterface_does_not_stop_the_rename(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)
        Interface.objects.create(
            device=self.device,
            module=module,
            name="et-1/0/0.100",
            type="virtual",
            parent=Interface.objects.get(module=module),
        )

        self._edit(bay, position="5")

        self.assertEqual(self._names(module), ["et-1/0/0.100", "et-1/0/5"])
        self.assertEqual(_journal(module), [])


class NestedBayEditTest(BayEditTestCase):
    """The modules nested below an edited bay are renamed too, and report on the module in the bay."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.optic_type = cls._module_type("Optic", "{module}")
        InterfaceNameRule.objects.create(
            module_type=cls.optic_type, name_template="et-{vc_position}/{slot}/{bay_position}"
        )

    def test_the_module_in_the_bay_and_the_modules_nested_in_it_are_renamed_for_the_new_position(self):
        card_type = self._card_type("Card", "1")
        InterfaceTemplate.objects.create(module_type=card_type, name="{module}", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(module_type=card_type, name_template="p{base}-{vc_position}")
        bay = self._bay(self.device)
        card, port = self._install_card(card_type, bay)
        optic = self._install(self.optic_type, port)
        self.assertEqual((self._names(card), self._names(optic)), (["p0-1"], ["et-1/0/1"]))

        self._edit(bay, position="2")

        self.assertEqual((self._names(card), self._names(optic)), (["p2-1"], ["et-1/2/1"]))
        self.assertEqual(_journal(card), [])

    def test_the_subtree_reports_in_one_journal_entry_on_the_module_in_the_edited_bay(self):
        card_type = self._card_type("Two Port Card", "1")
        ModuleBayTemplate.objects.create(module_type=card_type, name="Port 2", position="2")
        bay = self._bay(self.device)
        card = self._install(card_type, bay)
        blocked, failed = (
            self._install(self.optic_type, port) for port in ModuleBay.objects.filter(module=card).order_by("position")
        )
        Interface.objects.create(device=self.device, name="et-1/2/1", type=PLAIN_TYPE)
        with self.captureOnCommitCallbacks() as callbacks:
            self._save_edit(bay, position="2")
        (plan,) = [callback for callback in callbacks if isinstance(callback, ReapplyPlan)]
        self.assertEqual([trigger.pk for trigger in plan.triggers], [card.pk])

        with connection.execute_wrapper(_reject_interface_updates), self.assertLogs("netbox_interface_name_rules"):
            for callback in callbacks:
                callback()

        (entry,) = _journal(card)
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_DANGER)
        self.assertIn("`et-1/0/1` to `et-1/2/1`: target name is already in use", entry.comments)
        self.assertIn("injected reapply failure", entry.comments)
        self.assertEqual((self._names(blocked), self._names(failed)), (["et-1/0/1"], ["et-1/0/2"]))
        self.assertEqual((_journal(blocked), _journal(failed)), ([], []))

    def test_a_type_change_and_a_bay_edit_rename_the_nested_modules_from_the_naming_before_the_edit(self):
        bay = self._bay(self.device)
        card, port = self._install_card(self._card_type("Card", "1"), bay)
        optic = self._install(self.optic_type, port)
        other_card_type = self._card_type("Other Card", "1")

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            card.module_type = other_card_type
            card.save()
            self._save_edit(bay, position="2")

        self.assertEqual(self._names(optic), ["et-1/2/1"])

    def _card_with_optic(self):
        """Install a card in Bay 0 with an optic in its port; return the bay, the card, the port and the optic."""
        bay = self._bay(self.device)
        card, port = self._install_card(self._card_type("Card", "1"), bay)
        optic = self._install(self.optic_type, port)
        self.assertEqual(self._names(optic), ["et-1/0/1"])
        return bay, card, port, optic

    def test_an_outer_edit_undone_around_a_nested_edit_renames_the_nested_module_for_its_own_edit(self):
        bay, card, port, optic = self._card_with_optic()

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(bay, position="2")
            self._save_edit(port, position="3")
            self._save_edit(bay, position="0")

        self.assertEqual(self._names(optic), ["et-1/0/3"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_a_nested_edit_before_an_outer_edit_renames_the_nested_module_without_a_report(self):
        bay, card, port, optic = self._card_with_optic()

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(port, position="3")
            self._save_edit(bay, position="2")

        self.assertEqual(self._names(optic), ["et-1/2/3"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_an_outer_reapply_that_runs_first_renames_the_nested_module_from_its_earliest_naming(self):
        bay, card, port, optic = self._card_with_optic()
        other_card_type = self._card_type("Other Card", "1")

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            card.module_type = other_card_type
            card.save()
            self._save_edit(port, position="3")
            self._save_edit(bay, position="2")

        self.assertEqual(self._names(optic), ["et-1/2/3"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_a_card_and_an_optic_installed_before_an_edit_of_the_card_bay_build_the_optic_family(self):
        flat_optic_type = self._module_type("Flat Optic", "{module}")
        _flat_rule(flat_optic_type, "x-{slot}/{bay_position}:{channel}")
        bay = self._bay(self.device)

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            card = Module.objects.create(device=self.device, module_bay=bay, module_type=self._card_type("Card", "1"))
            port = ModuleBay.objects.get(module=card)
            optic = Module.objects.create(device=self.device, module_bay=port, module_type=flat_optic_type)
            self._save_edit(bay, position="2")

        self.assertEqual(self._names(optic), ["x-2/1:0", "x-2/1:1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_an_optic_installed_in_a_card_before_an_edit_of_the_card_bay_builds_its_family_once(self):
        flat_optic_type = self._module_type("Flat Optic", "{module}")
        _flat_rule(flat_optic_type, "x-{slot}/{bay_position}:{channel}")
        bay = self._bay(self.device)
        card, port = self._install_card(self._card_type("Card", "1"), bay)

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            optic = Module.objects.create(device=self.device, module_bay=port, module_type=flat_optic_type)
            self._save_edit(bay, position="2")

        self.assertEqual(self._names(optic), ["x-2/1:0", "x-2/1:1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_an_optic_installed_before_edits_of_the_card_bay_and_its_own_bay_builds_its_family_once(self):
        flat_optic_type = self._module_type("Flat Optic", "{module}")
        _flat_rule(flat_optic_type, "x-{slot}/{bay_position}:{channel}")
        bay = self._bay(self.device)
        card, port = self._install_card(self._card_type("Card", "1"), bay)

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            optic = Module.objects.create(device=self.device, module_bay=port, module_type=flat_optic_type)
            self._save_edit(bay, position="2")
            self._save_edit(port, position="3")

        self.assertEqual(self._names(optic), ["x-2/3:0", "x-2/3:1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_an_optic_moved_into_a_card_before_an_edit_of_the_card_bay_builds_its_family_once(self):
        card_type = self._card_type("Card", "1")
        optic_type = self._module_type("Scoped Optic", "{module}")
        InterfaceNameRule.objects.create(module_type=optic_type, name_template="p{bay_position}")
        _flat_rule(optic_type, "x-{slot}/{bay_position}:{channel}", parent_module_type=card_type)
        bay = self._bay(self.device)
        card, port = self._install_card(card_type, bay)
        optic = self._install(optic_type, self._bay(self.device, "Bay 1"))
        self.assertEqual(self._names(optic), ["p1"])

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(optic, port)
            self._save_edit(bay, position="2")

        self.assertEqual(self._names(optic), ["x-2/1:0", "x-2/1:1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_an_optic_whose_raw_name_reads_the_card_bay_is_renamed_after_an_edit_of_that_bay(self):
        chained_optic_type = self._module_type("Chained Optic", "{module}/{module}")
        InterfaceNameRule.objects.create(
            module_type=chained_optic_type, name_template="et-{vc_position}/{slot}/{bay_position}"
        )
        bay = self._bay(self.device)
        card, port = self._install_card(self._card_type("Card", "1"), bay)

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            optic = Module.objects.create(device=self.device, module_bay=port, module_type=chained_optic_type)
            self._save_edit(bay, position="2")

        self.assertEqual(self._names(optic), ["et-1/2/1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_SUBTREE_MOVES)
    def test_the_bay_post_saves_netbox_sends_in_a_move_are_not_bay_triggers(self):
        card, port = self._install_card(self._card_type("Token Card", "{module}"), self._bay(self.device))
        optic = self._install(self.optic_type, port)
        self.assertEqual(self._names(optic), ["et-1/0/0"])

        with _naming_reads() as reads, self.captureOnCommitCallbacks() as callbacks:
            self._save_move(card, self._bay(self.device, "Bay 2"))
        (plan,) = [callback for callback in callbacks if isinstance(callback, ReapplyPlan)]
        for callback in callbacks:
            callback()

        port.refresh_from_db()
        self.assertEqual(port.position, "2")
        self.assertEqual(([trigger.pk for trigger in plan.triggers], len(reads)), ([card.pk], 1))
        self.assertEqual(self._names(optic), ["et-1/2/2"])


class SubtreeTriggerMixTest(BayEditTestCase):
    """Triggers of a module and of a module nested in it, in one transaction, reapply each module once.

    Each module reapplies from its earliest naming. A nested module reports in the journal entry of
    the outermost moved or edited module whose naming read it.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.card_type = cls._card_type("Card", "1")
        cls.optic_type = cls._module_type("Optic", "{module}")
        InterfaceNameRule.objects.create(
            module_type=cls.optic_type, name_template="et-{vc_position}/{slot}/{bay_position}"
        )

    def _card_with_optic(self):
        """Install a card in Bay 0 with an optic in its port; return the bay, the card, the port and the optic."""
        bay = self._bay(self.device)
        card, port = self._install_card(self.card_type, bay)
        optic = self._install(self.optic_type, port)
        self.assertEqual(self._names(optic), ["et-1/0/1"])
        return bay, card, port, optic

    def test_a_nested_module_with_its_own_trigger_reports_on_the_module_in_the_outer_edited_bay(self):
        bay, card, port, optic = self._card_with_optic()
        Interface.objects.create(device=self.device, name="et-1/2/3", type=PLAIN_TYPE)

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(bay, position="2")
            self._save_edit(port, position="3")

        self.assertEqual((self._names(optic), reapplies.call_count), (["et-1/0/1"], 2))
        (entry,) = _journal(card)
        self.assertIn("`et-1/0/1` to `et-1/2/3`: target name is already in use", entry.comments)
        self.assertEqual(_journal(optic), [])

    def test_a_nested_type_change_and_an_outer_edit_reapply_the_nested_module_once_as_a_type_change(self):
        bay, card, _port, optic = self._card_with_optic()
        other_optic_type = self._module_type("Other Optic", "{module}")
        InterfaceNameRule.objects.create(
            module_type=other_optic_type, name_template="ge-{vc_position}/{slot}/{bay_position}"
        )
        Interface.objects.create(device=self.device, name="ge-1/2/1", type=PLAIN_TYPE)

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            optic.module_type = other_optic_type
            optic.save()
            self._save_edit(bay, position="2")

        self.assertEqual((self._names(optic), reapplies.call_count), (["et-1/0/1"], 2))
        (entry,) = _journal(card)
        self.assertIn("`et-1/0/1` to `ge-1/2/1`: target name is already in use", entry.comments)
        self.assertEqual(_journal(optic), [])

    def test_an_outer_edit_and_a_move_of_the_nested_module_out_reapply_it_once(self):
        bay, _card, _port, optic = self._card_with_optic()

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(bay, position="2")
            self._save_move(optic, self._bay(self.device, "Bay 1"))

        self.assertEqual((self._names(optic), reapplies.call_count), (["et-1/1/1"], 2))

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_SUBTREE_MOVES)
    def test_an_outer_move_and_a_nested_bay_edit_reapply_each_module_once(self):
        _bay, card, port, optic = self._card_with_optic()

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(card, self._bay(self.device, "Bay 2"))
            port.refresh_from_db()
            self._save_edit(port, position="3")

        self.assertEqual((self._names(optic), reapplies.call_count), (["et-1/2/3"], 2))
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    @skipUnless(NETBOX_MOVES_COMPONENTS, REQUIRES_SUBTREE_MOVES)
    def test_a_nested_bay_edit_and_an_outer_move_reapply_each_module_once(self):
        _bay, card, port, optic = self._card_with_optic()

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(port, position="3")
            self._save_move(card, self._bay(self.device, "Bay 2"))

        port.refresh_from_db()
        self.assertEqual((self._names(optic), reapplies.call_count), ([f"et-1/2/{port.position}"], 2))
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_a_rolled_back_nested_edit_leaves_the_outer_edit_to_rename_the_nested_module(self):
        bay, card, port, optic = self._card_with_optic()

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(bay, position="2")
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._save_edit(port, position="3")
                raise RuntimeError("roll back the savepoint")

        self.assertEqual(self._names(optic), ["et-1/2/1"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_a_rolled_back_outer_edit_leaves_the_nested_edit_to_rename_its_module(self):
        bay, card, port, optic = self._card_with_optic()

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(port, position="3")
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._save_edit(bay, position="2")
                raise RuntimeError("roll back the savepoint")

        self.assertEqual(self._names(optic), ["et-1/0/3"])
        self.assertEqual((_journal(card), _journal(optic)), ([], []))

    def test_a_naming_read_in_a_rolled_back_savepoint_is_not_used(self):
        other_card_type = self._card_type("Other Card", "1")
        scoped_optic_type = self._module_type("Scoped Optic", "{module}")
        InterfaceNameRule.objects.create(
            module_type=scoped_optic_type, parent_module_type=self.card_type, name_template="a-{slot}/{bay_position}"
        )
        InterfaceNameRule.objects.create(
            module_type=scoped_optic_type, parent_module_type=other_card_type, name_template="b-{slot}/{bay_position}"
        )
        bay = self._bay(self.device)
        card, port = self._install_card(self.card_type, bay)
        optic = self._install(scoped_optic_type, port)
        self.assertEqual(self._names(optic), ["a-0/1"])

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            other = Module.objects.create(
                device=self.device, module_bay=self._bay(self.device, "Bay 1"), module_type=self.plain_type
            )
            with self.assertRaises(RuntimeError), transaction.atomic():
                card.module_type = other_card_type
                card.save()
                self._save_edit(bay, position="2")
                raise RuntimeError("roll back the savepoint")
            bay.refresh_from_db()
            self._save_edit(bay, position="3")

        self.assertEqual((self._names(optic), self._names(other)), (["a-3/1"], ["et-1/0/1"]))
        self.assertEqual((_journal(card), _journal(optic)), ([], []))


class BayEditTransactionTest(BayEditTestCase):
    """Bay edits in one transaction coalesce with each other and with moves into one reapply per module."""

    def test_several_edits_of_one_bay_reapply_once_from_its_earliest_state(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(bay, position="5")
            self._save_edit(bay, position="7")

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-1/0/7"])

    def test_an_edit_the_same_transaction_undoes_reapplies_nothing(self):
        returned_bay = self._bay(self.device)
        returned = self._install(self.plain_type, returned_bay)
        rename_out_of_band(Interface.objects.get(module=returned), "operator-name")
        edited_bay = self._bay(self.device, "Bay 1")
        edited = self._install(self.plain_type, edited_bay)

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(returned_bay, position="5")
            self._save_edit(returned_bay, position="0")
            self._save_edit(edited_bay, position="4")

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual((self._names(returned), self._names(edited)), (["operator-name"], ["et-1/0/4"]))
        self.assertEqual(_journal(returned), [])

    def test_an_edit_in_a_rolled_back_savepoint_causes_no_reapply_and_a_later_edit_reapplies_once(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._save_edit(bay, position="9")
                raise RuntimeError("roll back the savepoint")
            bay.refresh_from_db()
            self._save_edit(bay, position="5")

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-1/0/5"])

    def test_an_edit_in_a_rolled_back_savepoint_schedules_no_reapply(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)

        with self.captureOnCommitCallbacks() as callbacks, transaction.atomic():
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._save_edit(bay, position="9")
                raise RuntimeError("roll back the savepoint")

        self.assertEqual([callback for callback in callbacks if isinstance(callback, (ReapplyPlan, ModuleTrigger))], [])
        self.assertEqual(self._names(module), ["et-1/0/0"])

    def test_an_edit_rolled_back_after_an_earlier_edit_keeps_the_earlier_reapply(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_edit(bay, position="5")
            with self.assertRaises(RuntimeError), transaction.atomic():
                self._save_edit(bay, position="7")
                raise RuntimeError("roll back the savepoint")

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-1/0/5"])

    def test_an_install_and_an_edit_of_its_bay_name_the_module_for_the_edited_bay(self):
        bay = self._bay(self.device)

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.plain_type)
            self._save_edit(bay, position="5")

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-1/0/5"])

    def test_an_install_and_an_edit_of_its_bay_under_a_flat_rule_build_the_family(self):
        flat_type = self._module_type("Flat", "{module}")
        _flat_rule(flat_type, "f-{bay_position}:{channel}")
        bay = self._bay(self.device)

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            module = Module.objects.create(device=self.device, module_bay=bay, module_type=flat_type)
            self._save_edit(bay, position="5")

        self.assertEqual(self._names(module), ["f-5:0", "f-5:1"])
        self.assertEqual(_journal(module), [])

    def test_a_module_moved_into_a_bay_that_is_then_edited_reapplies_once_for_the_edited_bay(self):
        module = self._install(self.plain_type, self._bay(self.device))
        bay = self._bay(self.device, "Bay 1")

        with _module_reapplies() as reapplies, self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(module, bay)
            self._save_edit(bay, position="4")

        self.assertEqual(reapplies.call_count, 1)
        self.assertEqual(self._names(module), ["et-1/0/4"])

    def test_a_module_moved_out_and_back_is_renamed_after_an_edit_of_its_bay(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(module, self._bay(self.device, "Bay 1"))
            self._save_move(module, bay)
            self._save_edit(bay, position="5")

        self.assertEqual(self._names(module), ["et-1/0/5"])

    def test_a_bay_edited_while_its_module_is_out_is_renamed_when_the_module_returns(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)

        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self._save_move(module, self._bay(self.device, "Bay 1"))
            self._save_edit(bay, position="5")
            self._save_move(module, bay)

        self.assertEqual(self._names(module), ["et-1/0/5"])


class BayEditPreviousStateTest(BayEditTestCase):
    """A bay save whose previous state cannot be read fails with the read error."""

    def test_a_bay_save_fails_with_the_previous_state_read_error(self):
        bay = self._bay(self.device)
        self._install(self.plain_type, bay)

        with (
            _previous_state_read_fails(BAY_STATE_READ) as replaced,
            self.assertRaisesMessage(DataError, "division by zero"),
            transaction.atomic(),
        ):
            self._save_edit(bay, position="5")

        self.assertEqual(len(replaced), 1)
        self.assertEqual(ModuleBay.objects.get(pk=bay.pk).position, "0")

    def test_a_naming_read_that_fails_fails_the_bay_edit_with_its_error(self):
        bay = self._bay(self.device)
        module = self._install(self.plain_type, bay)

        with (
            connection.execute_wrapper(_fail_the_naming_read),
            self.assertRaisesMessage(DataError, "division by zero"),
            transaction.atomic(),
        ):
            self._save_edit(bay, position="5")

        self.assertEqual(ModuleBay.objects.get(pk=bay.pk).position, "0")
        self.assertEqual(self._names(module), ["et-1/0/0"])


class BayEditAPITest(_MoveFixture, APITestCase):
    """A REST API bay edit reaches the rename trigger through NetBox's own write path."""

    model = ModuleBay
    user_permissions = ("dcim.view_modulebay", "dcim.change_modulebay", "dcim.view_module", "dcim.view_device")

    @classmethod
    def setUpTestData(cls):
        cls.build("BayEditApi")
        cls.module_type = cls._module_type("Plain", "{module}")
        InterfaceNameRule.objects.create(module_type=cls.module_type, name_template="et-{vc_position}/0/{bay_position}")

    def test_patching_the_bay_position_renames_the_installed_module(self):
        bay = self._bay(self.device)
        with self.captureOnCommitCallbacks(execute=True):
            module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        url = reverse("dcim-api:modulebay-detail", kwargs={"pk": bay.pk})

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(url, {"position": "5"}, format="json", **self.header)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._names(module), ["et-1/0/5"])
        self.assertEqual(_journal(module), [])
