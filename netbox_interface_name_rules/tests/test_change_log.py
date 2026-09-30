# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Every row the plugin writes gets a complete NetBox change-log record.

NetBox copies the before-state of a change from the snapshot that ``snapshot()`` takes, and it writes
a record only while a request is current. Each test drives a real plugin write and reads back the
``ObjectChange`` that NetBox recorded for it.
"""

import contextvars
import uuid
from contextlib import contextmanager

from core.choices import JobStatusChoices, ObjectChangeActionChoices
from core.models import Job, ObjectChange
from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from netbox import context as netbox_context
from netbox.registry import registry

from netbox_interface_name_rules.jobs import ApplyRuleJob
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import (
    make_device,
    make_device_type,
    make_job,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
)

User = get_user_model()
PLAIN_TYPE = "10gbase-x-sfpp"
UPDATE = ObjectChangeActionChoices.ACTION_UPDATE


def updates_of(instance):
    """Return the update records NetBox wrote for *instance*, oldest first."""
    return ObjectChange.objects.filter(
        changed_object_type=ContentType.objects.get_for_model(instance),
        changed_object_id=instance.pk,
        action=UPDATE,
    ).order_by("time", "pk")


def names_before_and_after(change):
    """Return the interface name *change* records before and after the write."""
    return change.prechange_data["name"], change.postchange_data["name"]


class _ModuleFixture:
    """One device with one module bay, and a module type whose one interface NetBox names ``{module}``."""

    @classmethod
    def build(cls, prefix):
        """Create the fixture objects as class attributes of *cls*."""
        manufacturer = make_manufacturer(prefix)
        device_type = make_device_type(manufacturer, prefix)
        make_module_bay_templates(device_type, ("Bay 0",))
        cls.device = make_device(prefix, device_type)
        cls.module_type = make_module_type(manufacturer, prefix)
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="{module}", type=PLAIN_TYPE)
        cls.user = User.objects.create_user(username=f"{prefix.lower()}-operator", is_superuser=True)

    @classmethod
    def _bay(cls):
        return ModuleBay.objects.get(device=cls.device, name="Bay 0")


class RenameTriggerChangeLogTest(_ModuleFixture, TransactionTestCase):
    """A module installed through the REST API commits, and the rename runs inside that request."""

    def setUp(self):
        self.build("ChgLogTrig")
        self.client.force_login(self.user)

    def _install_through_the_api(self):
        response = self.client.post(
            reverse("dcim-api:module-list"),
            {"device": self.device.pk, "module_bay": self._bay().pk, "module_type": self.module_type.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Module.objects.get(pk=response.json()["id"])

    def test_the_rename_after_an_install_records_the_name_before_and_after(self):
        InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        module = self._install_through_the_api()

        interface = Interface.objects.get(module=module)
        change = updates_of(interface).get()
        self.assertEqual(names_before_and_after(change), ("0", "et-0/0/0"))
        self.assertEqual(change.user, self.user)

    def test_flagging_a_rule_records_its_tags_before_and_after(self):
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="{bay_position}")

        self._install_through_the_api()

        change = updates_of(rule).get()
        self.assertEqual(
            (change.prechange_data["tags"], change.postchange_data["tags"]), ([], ["potentially-deprecated"])
        )


class RuleToggleChangeLogTest(TestCase):
    """The enable toggle on the rule list saves the rule inside the operator's request."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = make_manufacturer("ChgLogToggle")
        module_type = make_module_type(manufacturer, "ChgLogToggle")
        cls.rule = InterfaceNameRule.objects.create(module_type=module_type, name_template="et-0/0/{bay_position}")
        cls.user = User.objects.create_user(username="chglogtoggle-operator", is_superuser=True)

    def _toggle(self):
        self.client.post(reverse("plugins:netbox_interface_name_rules:interfacenamerule_toggle", args=[self.rule.pk]))

    def test_the_toggle_records_the_flag_before_and_after(self):
        self.client.force_login(self.user)

        self._toggle()

        change = updates_of(self.rule).get()
        self.assertEqual((change.prechange_data["enabled"], change.postchange_data["enabled"]), (True, False))

    def test_each_toggle_records_the_flag_that_the_one_before_it_wrote(self):
        self.client.force_login(self.user)

        self._toggle()
        self._toggle()

        flags = [
            (change.prechange_data["enabled"], change.postchange_data["enabled"]) for change in updates_of(self.rule)
        ]
        self.assertEqual(flags, [(True, False), (False, True)])

    def test_the_toggle_locks_the_rule_before_it_writes_the_flag(self):
        """A concurrent toggle waits for the lock, so it reads and flips the flag that this one wrote."""
        self.client.force_login(self.user)
        table = f'"{InterfaceNameRule._meta.db_table}"'

        with CaptureQueriesContext(connection) as queries:
            self._toggle()

        statements = [query["sql"] for query in queries.captured_queries if table in query["sql"]]
        writes = [
            "lock" if sql.endswith("FOR UPDATE") else "update"
            for sql in statements
            if sql.endswith("FOR UPDATE") or sql.startswith("UPDATE")
        ]
        self.assertEqual(writes, ["lock", "update"])


class ApplyRuleJobChangeLogTest(_ModuleFixture, TestCase):
    """The background Apply Rules job renames interfaces that a module carries from before the rule."""

    @classmethod
    def setUpTestData(cls):
        cls.build("ChgLogJob")
        # The test transaction never commits, so the install runs no rename trigger.
        cls.module = Module.objects.create(device=cls.device, module_bay=cls._bay(), module_type=cls.module_type)
        cls.rule = InterfaceNameRule.objects.create(module_type=cls.module_type, name_template="et-0/0/{bay_position}")

    def _run_the_job(self):
        job = make_job("ChgLogJob", self.user)
        ApplyRuleJob.handle(job, rule_id=self.rule.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        return job

    def test_the_job_records_the_name_before_and_after(self):
        self._run_the_job()

        change = updates_of(Interface.objects.get(module=self.module)).get()
        self.assertEqual(names_before_and_after(change), ("0", "et-0/0/0"))

    def test_the_job_records_its_changes_as_its_user_under_its_id(self):
        job = self._run_the_job()

        change = updates_of(Interface.objects.get(module=self.module)).get()
        self.assertEqual((change.user, change.request_id), (self.user, job.job_id))

    def test_a_job_without_a_user_fails_before_it_renames_anything(self):
        """The change log cannot name who made a change, so the job makes none."""
        job = Job.objects.create(name="Apply rule (test)", job_id=uuid.uuid4())

        ApplyRuleJob.handle(job, rule_id=self.rule.pk)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_ERRORED)
        self.assertIn("has no user", job.error)
        self.assertEqual(Interface.objects.get(module=self.module).name, "0")


def request_context_left_set():
    """Return the NetBox request context variables that are set in the current context."""
    current = contextvars.copy_context()
    return {
        name
        for name, value in vars(netbox_context).items()
        if isinstance(value, contextvars.ContextVar) and value in current
    }


def run_in_a_fresh_context(function, *args, **kwargs):
    """Run *function* in a context where no variable is set, and return what it leaves set there."""

    def run():
        function(*args, **kwargs)
        return request_context_left_set()

    return contextvars.Context().run(run)


@contextmanager
def event_tracking_without_finally(request):
    """NetBox 4.3.7's event_tracking, which keeps its context set when the body raises."""
    netbox_context.current_request.set(request)
    netbox_context.events_queue.set({})
    yield
    netbox_context.current_request.set(None)
    netbox_context.events_queue.set({})


@contextmanager
def request_processors(*processors):
    """Register only *processors* while the block runs."""
    registered = registry["request_processors"]
    saved = list(registered)
    registered[:] = processors
    try:
        yield
    finally:
        registered[:] = saved


class JobRequestContextTest(TestCase):
    """A job leaves no request context in the worker, whether its body returns or raises."""

    @classmethod
    def setUpTestData(cls):
        module_type = make_module_type(make_manufacturer("ChgLogCtx"), "ChgLogCtx")
        cls.rule = InterfaceNameRule.objects.create(module_type=module_type, name_template="et-0/0/{bay_position}")
        cls.broken_rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True, module_type_pattern="ChgLogCtx.*", name_template="et-0/0/{bay_position}"
        )
        # Only a queryset update stores a pattern that RE2 cannot compile, and the batch raises on it.
        InterfaceNameRule.objects.filter(pk=cls.broken_rule.pk).update(module_type_pattern="(")

    def _handle(self, rule):
        job = make_job("ChgLogCtx")
        left_set = run_in_a_fresh_context(ApplyRuleJob.handle, job, rule_id=rule.pk)
        job.refresh_from_db()
        return job.status, left_set

    def test_a_job_that_returns_leaves_no_request_context(self):
        self.assertEqual(self._handle(self.rule), (JobStatusChoices.STATUS_COMPLETED, set()))

    def test_a_job_that_raises_leaves_no_request_context(self):
        self.assertEqual(self._handle(self.broken_rule), (JobStatusChoices.STATUS_ERRORED, set()))

    def test_a_job_that_raises_leaves_no_request_context_of_a_processor_without_finally(self):
        with request_processors(event_tracking_without_finally):
            self.assertEqual(self._handle(self.broken_rule), (JobStatusChoices.STATUS_ERRORED, set()))
