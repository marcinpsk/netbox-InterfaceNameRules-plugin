# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The change-log snapshot guard refuses a plugin write without a current snapshot, and nothing else.

Each probe below runs the same code object under another module name, so the guard sees a frame of
plugin code, of NetBox code or of test code exactly as it sees the real ones.
"""

import functools
import types

from dcim.choices import InterfaceModeChoices
from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay, Site
from django.test import TestCase
from extras.models import Tag
from ipam.models import VLAN

from netbox_interface_name_rules.jobs import run_as_job_user
from netbox_interface_name_rules.tests import snapshot_guard
from netbox_interface_name_rules.tests.helpers import (
    make_device,
    make_device_type,
    make_job,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
)
from netbox_interface_name_rules.tests.snapshot_guard import EARLIER_SNAPSHOT, NO_SNAPSHOT, MissingSnapshotError


def _save(instance):
    instance.save()


def _add_tags(instance, *tags):
    instance.tags.add(*tags)


def _set_tags(instance, tags):
    instance.tags.set(tags)


def _install_adopting(device, module_type):
    module = Module(device=device, module_bay=ModuleBay.objects.get(device=device), module_type=module_type)
    module._adopt_components = True
    module.save()
    return module


def _call(function, *args):
    return function(*args)


def _swallow(function, *args):
    try:
        function(*args)
    except Exception:  # noqa: BLE001 - the probe stands for plugin code that swallows every error
        return


def _in_module(function, module):
    """Return *function* running in *module*, so the guard reads its frames as that module's."""
    return types.FunctionType(function.__code__, {**function.__globals__, "__name__": module}, function.__name__)


PLUGIN = "netbox_interface_name_rules.guard_probe"
plugin_save = _in_module(_save, PLUGIN)
plugin_add_tags = _in_module(_add_tags, PLUGIN)
plugin_set_tags = _in_module(_set_tags, PLUGIN)
plugin_call = _in_module(_call, PLUGIN)
plugin_install_adopting = _in_module(_install_adopting, PLUGIN)
plugin_swallow = _in_module(_swallow, PLUGIN)
netbox_save = _in_module(_save, "dcim.guard_probe")


class SnapshotGuardTestCase(TestCase):
    """One stored site, fetched fresh by each test."""

    @classmethod
    def setUpTestData(cls):
        cls.site_pk = Site.objects.create(name="Guard Site", slug="guard-site").pk
        cls.tag = Tag.objects.create(name="Guard Tag", slug="guard-tag")
        cls.other_tag = Tag.objects.create(name="Guard Other Tag", slug="guard-other-tag")

    def tearDown(self):
        snapshot_guard.take_violations()
        super().tearDown()

    def _site(self):
        return Site.objects.get(pk=self.site_pk)

    def assert_refused(self, problem, write, *args):
        """Assert that *write* is refused for *problem*, that the refusal is recorded, and return its message."""
        with self.assertRaisesMessage(MissingSnapshotError, problem) as raised:
            write(*args)
        message = str(raised.exception)
        self.assertIn(f"{__file__}:", message)
        self.assertEqual(snapshot_guard.take_violations(), [message])
        return message


class SnapshotGuardSaveTest(SnapshotGuardTestCase):
    """A plugin save of an existing row needs a snapshot that no earlier write used."""

    def test_a_plugin_save_without_a_snapshot_is_refused(self):
        site = self._site()

        message = self.assert_refused(NO_SNAPSHOT, plugin_save, site)

        self.assertTrue(message.startswith(f"dcim.Site pk={site.pk} "), message)

    def test_a_plugin_save_with_a_current_snapshot_passes(self):
        site = self._site()
        site.snapshot()
        site.description = "changed"

        plugin_save(site)

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_second_plugin_save_with_the_same_snapshot_is_refused(self):
        site = self._site()
        site.snapshot()
        plugin_save(site)
        site.description = "changed again"

        self.assert_refused(EARLIER_SNAPSHOT, plugin_save, site)

    def test_a_snapshot_taken_before_another_save_of_the_instance_is_refused(self):
        site = self._site()
        site.snapshot()
        site.save()

        self.assert_refused(EARLIER_SNAPSHOT, plugin_save, site)

    def test_a_refusal_that_plugin_code_swallows_is_still_recorded(self):
        site = self._site()

        plugin_swallow(plugin_save, site)

        self.assertEqual(len(snapshot_guard.take_violations()), 1)

    def test_test_code_and_netbox_code_may_save_without_a_snapshot(self):
        site = self._site()

        _save(site)
        netbox_save(site)

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_netbox_save_inside_a_plugin_call_is_netbox_s(self):
        plugin_call(netbox_save, self._site())

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_netbox_adopting_an_interface_for_a_plugin_install_is_netbox_s(self):
        manufacturer = make_manufacturer("GuardAdopt")
        device_type = make_device_type(manufacturer, "GuardAdopt")
        make_module_bay_templates(device_type, ("Bay 0",))
        device = make_device("GuardAdopt", device_type)
        module_type = make_module_type(manufacturer, "GuardAdopt")
        InterfaceTemplate.objects.create(module_type=module_type, name="eth0", type="1000base-t")
        interface = Interface.objects.create(device=device, name="eth0", type="1000base-t")

        module = plugin_install_adopting(device, module_type)

        self.assertEqual(snapshot_guard.take_violations(), [])
        interface.refresh_from_db()
        self.assertEqual(interface.module, module)

    def test_a_plugin_create_needs_no_snapshot(self):
        plugin_save(Site(name="Guard Created", slug="guard-created"))

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_plugin_save_after_its_create_needs_a_snapshot(self):
        site = Site(name="Guard Created", slug="guard-created")
        plugin_save(site)
        site.description = "changed"

        self.assert_refused(NO_SNAPSHOT, plugin_save, site)


class SnapshotGuardM2MTest(SnapshotGuardTestCase):
    """A plugin change of a many-to-many field of an existing row needs a current snapshot too."""

    def test_a_plugin_tag_change_without_a_snapshot_is_refused(self):
        self.assert_refused(NO_SNAPSHOT, plugin_add_tags, self._site(), self.tag)

    def test_a_plugin_tag_change_with_a_current_snapshot_passes(self):
        site = self._site()
        site.snapshot()

        plugin_add_tags(site, self.tag)

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_tag_change_after_a_save_with_the_same_snapshot_is_refused(self):
        site = self._site()
        site.snapshot()
        plugin_save(site)

        self.assert_refused(EARLIER_SNAPSHOT, plugin_add_tags, site, self.tag)

    def test_one_set_that_removes_and_adds_is_one_write(self):
        site = self._site()
        site.tags.add(self.tag)
        site.snapshot()

        plugin_set_tags(site, [self.other_tag])

        self.assertEqual(snapshot_guard.take_violations(), [])
        self.assertEqual(list(site.tags.values_list("slug", flat=True)), ["guard-other-tag"])

    def test_adding_a_tag_the_row_already_has_changes_nothing(self):
        site = self._site()
        site.tags.add(self.tag)

        plugin_add_tags(site, self.tag)

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_tag_change_right_after_a_plugin_create_joins_the_create_record(self):
        site = Site(name="Guard Created", slug="guard-created")
        plugin_save(site)

        plugin_add_tags(site, self.tag)
        plugin_add_tags(site, self.other_tag)

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_change_that_the_row_s_own_save_makes_is_part_of_that_save(self):
        """NetBox's ``BaseInterface.save()`` clears the tagged VLANs of a row that no longer tags."""
        device = make_device("GuardIface", make_device_type(make_manufacturer("GuardIface"), "GuardIface"))
        vlan = VLAN.objects.create(vid=42, name="guard-vlan")
        interface = Interface.objects.create(device=device, name="eth0", type="1000base-t", mode="tagged")
        interface.tagged_vlans.add(vlan)
        interface.snapshot()
        interface.mode = InterfaceModeChoices.MODE_ACCESS

        plugin_save(interface)

        self.assertEqual(snapshot_guard.take_violations(), [])
        self.assertFalse(interface.tagged_vlans.exists())

    def test_a_tag_change_in_the_request_that_created_the_row_joins_the_create_record(self):
        site = Site(name="Guard Created", slug="guard-created")

        run_as_job_user(
            make_job("GuardOne"),
            lambda: (plugin_save(site), plugin_add_tags(site, self.tag)),
            branch_schema_id=None,
        )

        self.assertEqual(snapshot_guard.take_violations(), [])

    def test_a_tag_change_in_a_later_request_needs_a_snapshot(self):
        """NetBox merges an M2M change only into a record of the same request, so the later one needs a before-state."""
        site = Site(name="Guard Created", slug="guard-created")
        run_as_job_user(make_job("GuardFirst"), lambda: plugin_save(site), branch_schema_id=None)

        self.assert_refused(
            NO_SNAPSHOT,
            functools.partial(run_as_job_user, branch_schema_id=None),
            make_job("GuardSecond"),
            lambda: plugin_add_tags(site, self.tag),
        )

    def test_a_tag_change_of_a_row_that_a_test_created_needs_a_snapshot(self):
        site = Site(name="Guard Created", slug="guard-created")
        site.save()

        self.assert_refused(NO_SNAPSHOT, plugin_add_tags, site, self.tag)
