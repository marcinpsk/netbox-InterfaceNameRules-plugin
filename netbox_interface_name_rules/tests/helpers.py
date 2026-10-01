# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Builders for the objects the tests need, a runner for the background jobs, and a reader of their webhooks.

Every builder takes a *prefix* and derives names and slugs from it. Test classes share one database
per worker, so a class that names its objects after itself cannot collide with another class, and a
failure still names the class it came from.
"""

import contextlib
import uuid
from dataclasses import dataclass

import django_rq
from core.events import OBJECT_CREATED, OBJECT_DELETED, OBJECT_UPDATED
from core.models import Job, ObjectType
from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    ModuleBayTemplate,
    ModuleType,
    Site,
)
from django.contrib.auth import get_user_model
from django.db import connections
from extras.choices import EventRuleActionChoices
from extras.models import EventRule, Webhook

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.transactions import READ_LOCK_TIMEOUT, SET_LOCK_TIMEOUT


def slug_for(prefix: str, suffix: str = "") -> str:
    """Return a slug built from *prefix*, safe to use as a NetBox slug."""
    cleaned = "".join(character if character.isalnum() else "-" for character in prefix).strip("-").lower()
    return f"{cleaned}-{suffix}" if suffix else cleaned


def make_manufacturer(prefix: str) -> Manufacturer:
    """Return one manufacturer named after *prefix*."""
    return Manufacturer.objects.create(name=f"{prefix} Manufacturer", slug=slug_for(prefix, "mfg"))


def make_device_type(manufacturer: Manufacturer, prefix: str, model: str | None = None) -> DeviceType:
    """Return one device type named after *prefix*."""
    model = model or f"{prefix} Device Type"
    return DeviceType.objects.create(manufacturer=manufacturer, model=model, slug=slug_for(prefix, "type"))


def make_module_type(
    manufacturer: Manufacturer,
    prefix: str,
    model: str | None = None,
    part_number: str | None = None,
) -> ModuleType:
    """Return one module type named after *prefix*, or after an explicit *model*."""
    model = model or f"{prefix} Module Type"
    return ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=part_number or model)


def make_module_bay_templates(device_type: DeviceType, names: tuple[str, ...]) -> list[ModuleBayTemplate]:
    """Return module bay templates on *device_type*, positioned in the order given.

    NetBox instantiates the bays when a device is created, so these must exist before the device.
    """
    return [
        ModuleBayTemplate.objects.create(device_type=device_type, name=name, position=str(position))
        for position, name in enumerate(names)
    ]


@dataclass(frozen=True)
class DevicePlacement:
    """The role and site a device needs, kept together so a test can reuse them."""

    role: DeviceRole
    site: Site


def make_placement(prefix: str) -> DevicePlacement:
    """Return the role and site for devices named after *prefix*."""
    return DevicePlacement(
        role=DeviceRole.objects.create(name=f"{prefix} Role", slug=slug_for(prefix, "role")),
        site=Site.objects.create(name=f"{prefix} Site", slug=slug_for(prefix, "site")),
    )


def make_device(
    prefix: str,
    device_type: DeviceType,
    placement: DevicePlacement | None = None,
    name: str | None = None,
    **fields,
) -> Device:
    """Return one device on *device_type*, creating a role and site when none is given."""
    placement = placement or make_placement(prefix)
    return Device.objects.create(
        name=name or slug_for(prefix, "01"),
        device_type=device_type,
        role=placement.role,
        site=placement.site,
        **fields,
    )


def make_job(prefix: str, user=None) -> Job:
    """Return a job row that *user*, or a new user named after *prefix*, enqueued as the Apply page does."""
    user = user or get_user_model().objects.create_user(username=slug_for(prefix, "operator"))
    return Job.objects.create(name=f"{prefix} job", job_id=uuid.uuid4(), user=user)


def make_unrunnable_rule(prefix: str) -> InterfaceNameRule:
    """Return a regex rule whose stored pattern RE2 cannot compile, so a batch over it raises ValueError."""
    rule = InterfaceNameRule.objects.create(
        module_type_is_regex=True, module_type_pattern=f"{prefix}.*", name_template="et-0/0/{bay_position}"
    )
    # Only a queryset update stores a pattern that validation refuses.
    InterfaceNameRule.objects.filter(pk=rule.pk).update(module_type_pattern="(")
    return rule


def activate(branch):
    """Return netbox-branching's context manager that makes *branch* active, or main for None."""
    # Imported here, so that this module imports where netbox-branching is not installed.
    from netbox_branching.utilities import activate_branch

    return activate_branch(branch)


def lock_timeout(alias: str) -> str:
    """Return the ``lock_timeout`` of the session of *alias*, read as the write scope reads it."""
    with connections[alias].cursor() as cursor:
        cursor.execute(READ_LOCK_TIMEOUT)
        return cursor.fetchone()[0]


def set_lock_timeout(alias: str, value: str) -> None:
    """Set the ``lock_timeout`` of the session of *alias*, as the write scope sets it."""
    with connections[alias].cursor() as cursor:
        cursor.execute(SET_LOCK_TIMEOUT, [value])


def run_job_logged(test_case, runner, raises=None, **kwargs):
    """Run *runner* with *kwargs*, assert that it raises *raises* when given, and return the records it logs."""
    with test_case.assertLogs(f"netbox.jobs.{type(runner).__name__}", level="INFO") as logs:
        with test_case.assertRaises(raises) if raises else contextlib.nullcontext():
            runner.run(**kwargs)
    return logs.records


def make_interface_webhook_rule(prefix: str) -> EventRule:
    """Return a webhook event rule for every create, update and delete of an interface."""
    webhook = Webhook.objects.create(name=f"{prefix} hook", payload_url="http://localhost:9000/")
    event_rule = EventRule.objects.create(
        name=f"{prefix} interface events",
        event_types=[OBJECT_CREATED, OBJECT_UPDATED, OBJECT_DELETED],
        action_type=EventRuleActionChoices.WEBHOOK,
        action_object_type=ObjectType.objects.get_for_model(Webhook),
        action_object_id=webhook.pk,
    )
    event_rule.object_types.set([ObjectType.objects.get_for_model(Interface)])
    return event_rule


def empty_the_webhook_queue(test_case):
    """Empty the RQ queue that webhooks go to, now and when *test_case* ends."""
    queue = django_rq.get_queue("default")
    queue.empty()
    test_case.addCleanup(queue.empty)


def queued_webhook_jobs(event_rule) -> list:
    """Return the RQ job of each webhook that *event_rule* queued."""
    return [job for job in django_rq.get_queue("default").jobs if job.kwargs["event_rule"] == event_rule]


def queued_webhooks(event_rule) -> list[tuple[str, str]]:
    """Return the event type and object name of each webhook that *event_rule* queued, sorted."""
    return sorted((job.kwargs["event_type"], job.kwargs["data"]["name"]) for job in queued_webhook_jobs(event_rule))


@contextlib.contextmanager
def row_lock_in_another_session(alias):
    """Yield a function that locks one interface row on *alias* from a second session until the block ends."""
    other = connections.create_connection(alias)
    try:
        other.set_autocommit(False)

        def lock(pk):
            with other.cursor() as cursor:
                cursor.execute("SELECT id FROM dcim_interface WHERE id = %s FOR UPDATE", [pk])

        yield lock
    finally:
        other.rollback()
        other.close()


@contextlib.contextmanager
def interface_signal(signal, receiver):
    """Connect *receiver* to the model *signal* of Interface while the block runs."""
    signal.connect(receiver, sender=Interface, weak=False)
    try:
        yield
    finally:
        signal.disconnect(receiver, sender=Interface)
