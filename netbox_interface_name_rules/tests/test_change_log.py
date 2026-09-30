# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Every row the plugin writes gets a complete NetBox change-log record.

NetBox copies the before-state of a change from the snapshot that ``snapshot()`` takes, and it writes
a record only while a request is current. Each test drives a real plugin write and reads back the
``ObjectChange`` that NetBox recorded for it.
"""

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange
from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import (
    make_device,
    make_device_type,
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

    def test_the_toggle_records_the_flag_before_and_after(self):
        self.client.force_login(self.user)

        self.client.post(reverse("plugins:netbox_interface_name_rules:interfacenamerule_toggle", args=[self.rule.pk]))

        change = updates_of(self.rule).get()
        self.assertEqual((change.prechange_data["enabled"], change.postchange_data["enabled"]), (True, False))
