# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Builders, the values and test cases that several test modules share, a job runner and a webhook reader.

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
)
from django.contrib.auth import get_user_model
from django.db import connections
from django.http import HttpRequest
from django.test import TestCase
from extras.choices import EventRuleActionChoices
from extras.models import EventRule, Webhook
from netbox.context import current_request, events_queue
from rq import Worker
from rq.job import Job as RQJob

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.transactions import READ_LOCK_TIMEOUT, SET_LOCK_TIMEOUT

# Resolved defensively so this module still imports on NetBox releases without channelization.
CHANNEL_TYPE = getattr(InterfaceTypeChoices, "TYPE_CHANNEL", "channel")
PARENT_TYPE = InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS
PLAIN_TYPE = InterfaceTypeChoices.TYPE_10GE_SFP_PLUS
FLAT = BreakoutModeChoices.FLAT
CHANNELIZED = BreakoutModeChoices.CHANNELIZED
TEST_PASSWORD = "testpass123"  # noqa: S105 - Test credential only.
PLUGIN_LOGGER = "netbox_interface_name_rules"
REQUIRES_CHANNELIZATION = "requires a NetBox that models channelized interfaces (4.7+)"
REQUIRES_NO_CHANNELIZATION = "requires a NetBox that cannot model channelized interfaces (4.6 and older)"
# On NetBox 4.5 and older the token stays literal in the interface name.
REQUIRES_VC_POSITION_TOKEN = "requires a NetBox that resolves {vc_position} in template names (4.6+)"  # noqa: S105 - Skip reason, not a credential.


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


def branch_cookie() -> str:
    """Return the name of netbox-branching's branch cookie."""
    from netbox_branching.constants import COOKIE_NAME

    return COOKIE_NAME


def lock_timeout(alias: str) -> str:
    """Return the ``lock_timeout`` of the session of *alias*, read as the write scope reads it."""
    with connections[alias].cursor() as cursor:
        cursor.execute(READ_LOCK_TIMEOUT)
        return cursor.fetchone()[0]


def set_lock_timeout(alias: str, value: str) -> None:
    """Set the ``lock_timeout`` of the session of *alias*, as the write scope sets it."""
    with connections[alias].cursor() as cursor:
        cursor.execute(SET_LOCK_TIMEOUT, [value])


def register_a_worker(test_case):
    """Register an RQ worker of the default queue until *test_case* ends, so NetBox accepts work; no process runs it."""
    worker = Worker(["default"], connection=django_rq.get_connection())
    worker.register_birth()
    test_case.addCleanup(worker.register_death)


def queued_job(test_case, job):
    """Return the queue's record of *job*, which a worker reads from Redis, and delete it when *test_case* ends."""
    queued = RQJob.fetch(str(job.job_id), connection=django_rq.get_connection())
    test_case.addCleanup(queued.delete)
    return queued


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


def build_device(prefix, bay_positions=(), **device_kwargs):
    """Create a manufacturer, a device type with module bays at *bay_positions*, and one device."""
    slug = prefix.lower()
    manufacturer = Manufacturer.objects.create(name=f"{prefix}Mfg", slug=f"{slug}-mfg")
    device_type = DeviceType.objects.create(manufacturer=manufacturer, model=f"{prefix}-Dev", slug=f"{slug}-dev")
    for position in bay_positions:
        ModuleBayTemplate.objects.create(device_type=device_type, name=f"Bay {position}", position=position)
    role = DeviceRole.objects.create(name=f"{prefix}Role", slug=f"{slug}-role")
    site = Site.objects.create(name=f"{prefix}Site", slug=f"{slug}-site")
    device = Device.objects.create(name=f"{slug}-sw1", device_type=device_type, role=role, site=site, **device_kwargs)
    return manufacturer, device


def channelized_family(module_type, parent_name, child_names, channels=4):
    """Add one channelized parent template plus its channel templates to *module_type*.

    *child_names* maps a channel_id to the template name that channel takes.
    """
    parent = InterfaceTemplate.objects.create(
        module_type=module_type, name=parent_name, type=PARENT_TYPE, channels=channels
    )
    for channel_id, name in child_names.items():
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name=name,
            type=CHANNEL_TYPE,
            parent=parent,
            channel_id=channel_id,
        )
    return parent


def channelized_module_type(manufacturer, model, channels=4, child_channel_ids=(1, 2, 3, 4), child_names=None):
    """Create a ModuleType whose interface templates form a channelized family.

    The parent template is ``{module}`` with *channels* set; each entry in *child_channel_ids* adds a
    channel-type template bound to it.  *child_names* maps a channel_id to a template name, defaulting
    to the upstream ``<parent>:<channel_id>`` convention.
    """
    module_type = ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=model)
    names = child_names or {channel_id: f"{{module}}:{channel_id}" for channel_id in child_channel_ids}
    channelized_family(
        module_type, "{module}", {channel_id: names[channel_id] for channel_id in child_channel_ids}, channels=channels
    )
    return module_type


def plain_module_type(manufacturer, model, iface_type=PARENT_TYPE):
    """Create a ModuleType with a single plain (non-channelized) port template."""
    module_type = ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=model)
    InterfaceTemplate.objects.create(module_type=module_type, name="{module}", type=iface_type)
    return module_type


def token_module_type(manufacturer, model, *template_names, iface_type=PLAIN_TYPE):
    """Create a ModuleType whose interface templates are named *template_names*, in order."""
    module_type = ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=model)
    for name in template_names:
        InterfaceTemplate.objects.create(module_type=module_type, name=name, type=iface_type)
    return module_type


def install_form(bay, module_type):
    """Return the form data of NetBox's module edit view that installs *module_type* in *bay*."""
    return {
        "device": bay.device_id,
        "module_bay": bay.pk,
        "module_type": module_type.pk,
        "status": "active",
        "replicate_components": "on",
    }


def names_of(module):
    """Return the sorted interface names of *module* on the active branch."""
    return sorted(Interface.objects.filter(module=module).values_list("name", flat=True))


@contextlib.contextmanager
def request_context(user):
    """Set the request and the event queue as NetBox's event_tracking does, without its flush."""
    request = HttpRequest()
    request.user = user
    request.id = uuid.uuid4()
    request_token = current_request.set(request)
    queue_token = events_queue.set({})
    try:
        yield
    finally:
        events_queue.reset(queue_token)
        current_request.reset(request_token)


class WriteInterfacesTo:
    """A database router that sends each write of an interface to one alias."""

    def __init__(self, alias):
        self.alias = alias

    def db_for_write(self, model, **hints):
        return self.alias if model is Interface else None


class ChannelizationTestCase(TestCase):
    """Install helpers shared by the channelized module-install test cases."""

    def _install(self, module_type, position, run_rules=True):
        """Install a module into the bay at *position*; run the post-commit rename unless told not to.

        ``run_rules=False`` leaves the freshly instantiated (raw-named) family in place so a test can
        call the engine directly and assert its return value.
        """
        bay = ModuleBay.objects.get(device=self.device, name=f"Bay {position}")
        if run_rules:
            with self.captureOnCommitCallbacks(execute=True):
                module = Module.objects.create(device=self.device, module_bay=bay, module_type=module_type)
        else:
            module = Module.objects.create(device=self.device, module_bay=bay, module_type=module_type)
        return module, bay

    @staticmethod
    def _names(module):
        """Return the sorted interface names of *module*."""
        return sorted(Interface.objects.filter(module=module).values_list("name", flat=True))

    @staticmethod
    def _parent(module):
        """Return the channelized parent interface of *module*."""
        return Interface.objects.get(module=module, channels__isnull=False)

    @staticmethod
    def _child(module, channel_id):
        """Return the channel subinterface of *module* bound to *channel_id*."""
        return Interface.objects.get(module=module, channel_id=channel_id)


class VcDriftTestCase(ChannelizationTestCase):
    """VC transitions go through a real ``Device.save()`` so the plugin's signals do the scheduling."""

    def _save_vc_state(self, device, virtual_chassis, position):
        with self.captureOnCommitCallbacks(execute=True):
            device.virtual_chassis = virtual_chassis
            device.vc_position = position
            device.save()

    def _join(self, vc, position, device=None):
        """Add *device* to *vc* at *position* — the join direction (fallback → position)."""
        self._save_vc_state(device or self.device, vc, position)

    def _renumber(self, position, device=None):
        """Move *device* to another position inside its VC — the renumber direction (P → Q)."""
        device = device or self.device
        self._save_vc_state(device, device.virtual_chassis, position)

    def _leave(self, device=None):
        """Remove *device* from its VC — the leave direction (position → fallback)."""
        self._save_vc_state(device or self.device, None, None)

    def _install_on(self, device, module_type, position):
        """Install a module of *module_type* into *device*'s bay at *position*, rules and all."""
        bay = ModuleBay.objects.get(device=device, name=f"Bay {position}")
        with self.captureOnCommitCallbacks(execute=True):
            module = Module.objects.create(device=device, module_bay=bay, module_type=module_type)
        return module, bay
