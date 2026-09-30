# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Rename triggers in a real netbox-branching branch: the plan runs after the commit of the branch connection.

NetBox saves a new module before it creates the module's interfaces, in the transaction that it opens
on the branch connection. Each test builds its rows on main first, because a branch copies main when
it provisions. Requests carry netbox-branching's cookie or header, so its request processor, NetBox's
view transaction, the trigger and the plan run for real.
"""

import uuid
from contextlib import ExitStack

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange
from dcim.models import Device, Interface, InterfaceTemplate, Module
from django.contrib.contenttypes.models import ContentType
from django.db import connections, transaction
from django.db.models.signals import post_save
from django.test import RequestFactory
from django.urls import reverse
from extras.choices import JournalEntryKindChoices
from extras.jobs import ScriptJob
from extras.models import JournalEntry
from extras.scripts import Script
from netbox.registry import registry

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.rename_triggers import PlanRunner
from netbox_interface_name_rules.tests.helpers import (
    interface_signal,
    lock_timeout,
    make_device,
    make_device_type,
    make_job,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
    set_lock_timeout,
)
from netbox_interface_name_rules.tests.test_branch_transactions import BRANCH_BEFORE, DEFAULT_BEFORE, SERVER_DEFAULT
from netbox_interface_name_rules.tests.test_branch_writes import _BranchWriteCase, _ChannelCase
from netbox_interface_name_rules.tests.test_channelization import PLAIN_TYPE
from netbox_interface_name_rules.transactions import LOCK_TIMEOUT

# The caller's transactions at the save, outermost first, when the branch connection holds one.
NESTINGS = (("branch",), ("default", "branch"), ("branch", "default"))


def install_form(bay, module_type):
    """Return the form data of NetBox's module edit view that installs *module_type* in *bay*."""
    return {
        "device": bay.device_id,
        "module_bay": bay.pk,
        "module_type": module_type.pk,
        "status": "active",
        "replicate_components": "on",
    }


class _InstallCase(_BranchWriteCase):
    """A device with empty module bays, and a module type whose rule appends ``.br`` to NetBox's names.

    NetBox names the two interfaces of a module in the bay at position ``n`` ``an`` and ``bn``.
    """

    BAYS = 3

    def build(self):
        manufacturer = make_manufacturer(self.PREFIX)
        device_type = make_device_type(manufacturer, self.PREFIX)
        make_module_bay_templates(device_type, tuple(f"Bay {position}" for position in range(self.BAYS)))
        self.device = make_device(self.PREFIX, device_type)
        self.module_type = make_module_type(manufacturer, self.PREFIX)
        for template in ("a{module}", "b{module}"):
            InterfaceTemplate.objects.create(module_type=self.module_type, name=template, type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="{base}.br")

    def install(self, position):
        """Install a module in the bay at *position* through the ORM, on the alias that the router gives."""
        return Module.objects.create(device=self.device, module_bay=self.bay(position), module_type=self.module_type)

    def renames_in_branch(self, position):
        """Return ``(name before, name after)`` of each interface update in the branch at *position*, sorted."""
        changes = ObjectChange.objects.using(self.alias).filter(
            changed_object_type=ContentType.objects.get_for_model(Interface),
            changed_object_id__in=[pk for pk, _ in self.interfaces_in_branch(position)],
            action=ObjectChangeActionChoices.ACTION_UPDATE,
        )
        return sorted((change.prechange_data["name"], change.postchange_data["name"]) for change in changes)

    def assert_nothing_on_main(self):
        self.assertFalse(Interface.objects.filter(device=self.device).exists())


class InstallInABranchTest(_InstallCase):
    PREFIX = "BrInstall"

    def test_a_module_installed_through_the_ui_gets_its_names_in_the_branch(self):
        response = self.client.post(reverse("dcim:module_add"), install_form(self.bay(0), self.module_type))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.names_in_branch(0), ["a0.br", "b0.br"])
        self.assertEqual(self.renames_in_branch(0), [("a0", "a0.br"), ("b0", "b0.br")])
        self.assert_nothing_on_main()

    def test_a_module_installed_through_the_rest_api_gets_its_names_in_the_branch(self):
        del self.client.cookies["active_branch"]
        data = {"device": self.device.pk, "module_bay": self.bay(0).pk, "module_type": self.module_type.pk}

        response = self.client.post(
            reverse("dcim-api:module-list"),
            data,
            content_type="application/json",
            headers={"X-NetBox-Branch": self.branch.schema_id},
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.names_in_branch(0), ["a0.br", "b0.br"])
        self.assertEqual(self.renames_in_branch(0), [("a0", "a0.br"), ("b0", "b0.br")])
        self.assert_nothing_on_main()

    def test_modules_imported_in_bulk_get_their_names_in_the_branch(self):
        rows = [f"{self.device.name},Bay {position},{self.module_type.model},active" for position in (0, 1)]

        response = self.client.post(
            reverse("dcim:module_bulk_import"),
            {
                "data": "\n".join(["device,module_bay,module_type,status", *rows]),
                "format": "csv",
                "csv_delimiter": "auto",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            [self.names_in_branch(position) for position in (0, 1)], [["a0.br", "b0.br"], ["a1.br", "b1.br"]]
        )
        self.assertEqual(self.renames_in_branch(1), [("a1", "a1.br"), ("b1", "b1.br")])
        self.assert_nothing_on_main()

    def test_a_name_collision_on_install_skips_that_interface_and_keeps_the_rest_of_the_save(self):
        with self.in_branch():
            Interface.objects.create(device=self.device, name="b0.br", type=PLAIN_TYPE)

        response = self.client.post(reverse("dcim:module_add"), install_form(self.bay(0), self.module_type))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.names_in_branch(0), ["a0.br", "b0"])
        with self.in_branch():
            module = Module.objects.get(module_bay=self.bay(0))
            (entry,) = JournalEntry.objects.filter(
                assigned_object_type=ContentType.objects.get_for_model(Module), assigned_object_id=module.pk
            )
        self.assertEqual(entry.kind, JournalEntryKindChoices.KIND_WARNING)
        self.assertIn("`b0` to `b0.br`", entry.comments)
        self.assert_nothing_on_main()


class TriggerTransactionsInABranchTest(_InstallCase):
    PREFIX = "BrTrigger"

    def test_trigger_savepoint_rollback_filters_only_rolled_back_triggers(self):
        """A rollback of a branch savepoint drops its trigger; a rollback on ``default`` drops no branch save."""
        with self.in_branch(), transaction.atomic(using="default"), transaction.atomic(using=self.alias):
            self.install(0)
            with transaction.atomic(using=self.alias):
                self.install(1)
                transaction.set_rollback(True, using=self.alias)
            with transaction.atomic(using="default"):
                self.install(2)
                transaction.set_rollback(True, using="default")
            runners = [entry for _, entry, _ in connections[self.alias].run_on_commit if isinstance(entry, PlanRunner)]

        self.assertEqual(
            [self.names_in_branch(position) for position in (0, 1, 2)], [["a0.br", "b0.br"], [], ["a2.br", "b2.br"]]
        )
        self.assertEqual({id(runner.plan) for runner in runners}, {id(runners[-1].plan)})
        self.assertEqual([trigger.kept for trigger in runners[-1].plan.triggers], [True, False, True])

    def test_a_plan_that_commits_after_its_branch_was_left_raises(self):
        with self.assertRaisesMessage(RuntimeError, f"'default', but the operation expects '{self.alias}'"):
            with transaction.atomic(using=self.alias), self.in_branch():
                self.install(0)

        self.assertEqual(self.names_in_branch(0), ["a0", "b0"])

    def test_a_save_through_default_in_a_branch_raises_before_the_row_is_written(self):
        with self.in_branch():
            device = Device.objects.get(pk=self.device.pk)
        device.name = "brtrigger-renamed"

        with self.in_branch(), self.assertRaisesMessage(RuntimeError, f"but the write alias is '{self.alias}'"):
            device.save(using="default")

        self.assertEqual(Device.objects.get(pk=self.device.pk).name, self.device.name)


class _RuledChannelCase(_ChannelCase):
    """The channelized case with its rule on main, so an install in the branch is a rename trigger."""

    def build(self):
        super().build()
        self.add_rule()


class ChannelizedInstallInABranchTest(_RuledChannelCase):
    PREFIX = "BrChanInstall"
    POSITIONS = ("1", "2", "3", "4")

    def test_trigger_runner_reconciliation_during_branch_callback_drain(self):
        """The plan runs in the commit callbacks of the branch; the reconciliation still follows NetBox's cascade."""
        response = self.client.post(reverse("dcim:module_add"), install_form(self.bay("1"), self.module_type))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.names_in_branch("1"), self.kept("1"))
        for position, nesting in zip(("2", "3", "4"), NESTINGS, strict=True):
            with self.subTest(nesting=nesting), self.in_branch():
                with ExitStack() as caller:
                    for name in nesting:
                        caller.enter_context(transaction.atomic(using=self.alias if name == "branch" else "default"))
                    Module.objects.create(
                        device=self.device, module_bay=self.bay(position), module_type=self.module_type
                    )
                self.assertEqual(self.names_in_branch(position), self.kept(position))


class ScriptTriggerInABranchTest(_RuledChannelCase):
    """A NetBox script installs a module in a branch; each connection starts from its own ``lock_timeout``."""

    PREFIX = "BrScript"

    def setUp(self):
        super().setUp()
        self.addCleanup(set_lock_timeout, "default", SERVER_DEFAULT)
        set_lock_timeout("default", DEFAULT_BEFORE)
        set_lock_timeout(self.alias, BRANCH_BEFORE)

    def timeouts(self):
        return lock_timeout("default"), lock_timeout(self.alias)

    def run_script(self, script):
        """Run *script* as NetBox's script job does, in the request processors of a request in the branch."""
        request = RequestFactory().get("/")
        request.user = self.user
        request.id = uuid.uuid4()
        request.COOKIES["active_branch"] = self.branch.schema_id
        with ExitStack() as processors:
            for processor in registry["request_processors"]:
                processors.enter_context(processor(request))
            ScriptJob(make_job(self.PREFIX, self.user)).run_script(script, request, {}, commit=True)

    def test_script_trigger_restores_timeouts_before_caller_default_callbacks(self):
        seen = {}
        case = self
        bay = self.bay("1")

        class InstallModule(Script):
            def run(self, data, commit):
                transaction.on_commit(
                    lambda: seen.setdefault("script callback", (case.timeouts(), case.names_in_branch("1"))),
                    using="default",
                )
                Module.objects.create(device_id=bay.device_id, module_bay_id=bay.pk, module_type=case.module_type)

        def at_the_cascade(sender, instance, **kwargs):
            if instance.name == "et-0/0/1:2":
                seen.setdefault("cascade", self.timeouts())

        with interface_signal(post_save, at_the_cascade):
            self.run_script(InstallModule())

        self.assertEqual(
            seen,
            {
                "cascade": (LOCK_TIMEOUT, LOCK_TIMEOUT),
                "script callback": ((DEFAULT_BEFORE, BRANCH_BEFORE), self.cascaded("1")),
            },
        )
        self.assertEqual(self.names_in_branch("1"), self.kept("1"))
