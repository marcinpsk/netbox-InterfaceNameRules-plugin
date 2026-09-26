# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Every write path must refuse a parent module type on a device rule."""

import csv
import io

from dcim.models import Manufacturer, ModuleType
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from netbox_interface_name_rules.models import DEVICE_RULE_PARENT_MODULE_TYPE_ERROR, InterfaceNameRule


def _scope_fixtures(cls, prefix):
    manufacturer = Manufacturer.objects.create(name=f"{prefix}Mfg", slug=f"{prefix.lower()}mfg")
    cls.parent = ModuleType.objects.create(manufacturer=manufacturer, model=f"{prefix}-CVR")
    cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model=f"{prefix}-SFP")
    cls.device_rule = InterfaceNameRule.objects.create(
        applies_to_device_interfaces=True, module_type_pattern="Gi.*", name_template="{base}"
    )
    cls.module_rule = InterfaceNameRule.objects.create(module_type=cls.module_type, name_template="{base}")


class DeviceRuleScopeViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        _scope_fixtures(cls, "ScopeView")
        cls.user = get_user_model().objects.create_superuser(username="scope-editor", password=None)

    def setUp(self):
        self.client.force_login(self.user)

    def test_the_add_form_refuses_it_on_the_field(self):
        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_add"),
            {
                "applies_to_device_interfaces": "on",
                "module_type_pattern": "Te.*",
                "parent_module_type": self.parent.pk,
                "name_template": "{base}",
                "breakout_mode": "flat",
                "channel_count": 0,
                "channel_start": 0,
                "enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["parent_module_type"], [DEVICE_RULE_PARENT_MODULE_TYPE_ERROR])
        self.assertFalse(InterfaceNameRule.objects.filter(module_type_pattern="Te.*").exists())

    def test_the_csv_import_refuses_it(self):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["module_type_pattern", "parent_module_type", "name_template", "applies_to_device_interfaces"])
        writer.writerow(["Te.*", self.parent.model, "{base}", True])
        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_bulk_import"),
            {"data": buf.getvalue(), "format": "csv", "csv_delimiter": "auto"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, DEVICE_RULE_PARENT_MODULE_TYPE_ERROR)
        self.assertFalse(InterfaceNameRule.objects.filter(module_type_pattern="Te.*").exists())

    def test_the_bulk_edit_refuses_it_and_changes_no_rule(self):
        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_bulk_edit"),
            {"_apply": "1", "pk": [self.module_rule.pk, self.device_rule.pk], "parent_module_type": self.parent.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, f"Rule {self.device_rule.pk} ({self.device_rule}): {DEVICE_RULE_PARENT_MODULE_TYPE_ERROR}"
        )
        for rule in (self.module_rule, self.device_rule):
            rule.refresh_from_db()
            self.assertIsNone(rule.parent_module_type)

    def test_the_bulk_edit_still_sets_it_on_module_rules(self):
        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_bulk_edit"),
            {"_apply": "1", "pk": [self.module_rule.pk], "parent_module_type": self.parent.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.module_rule.refresh_from_db()
        self.assertEqual(self.module_rule.parent_module_type, self.parent)


class DeviceRuleScopeAPITest(APITestCase):
    model = InterfaceNameRule
    view_namespace = "plugins-api:netbox_interface_name_rules"
    user_permissions = (
        "netbox_interface_name_rules.view_interfacenamerule",
        "netbox_interface_name_rules.add_interfacenamerule",
        "netbox_interface_name_rules.change_interfacenamerule",
    )

    @classmethod
    def setUpTestData(cls):
        _scope_fixtures(cls, "ScopeAPI")
        cls.regex_rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="SFP.*",
            parent_module_type=cls.parent,
            name_template="{base}",
        )

    def _assert_refused(self, response):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["parent_module_type"], [DEVICE_RULE_PARENT_MODULE_TYPE_ERROR])

    def test_post_refuses_it(self):
        response = self.client.post(
            self._get_list_url(),
            {
                "applies_to_device_interfaces": True,
                "module_type_pattern": "Te.*",
                "parent_module_type": self.parent.pk,
                "name_template": "{base}",
            },
            format="json",
            **self.header,
        )
        self._assert_refused(response)
        self.assertFalse(InterfaceNameRule.objects.filter(module_type_pattern="Te.*").exists())

    def test_patch_refuses_setting_it_on_a_device_rule(self):
        response = self.client.patch(
            self._get_detail_url(self.device_rule), {"parent_module_type": self.parent.pk}, format="json", **self.header
        )
        self._assert_refused(response)
        self.device_rule.refresh_from_db()
        self.assertIsNone(self.device_rule.parent_module_type)

    def test_patch_refuses_turning_a_scoped_rule_into_a_device_rule(self):
        response = self.client.patch(
            self._get_detail_url(self.regex_rule), {"applies_to_device_interfaces": True}, format="json", **self.header
        )
        self._assert_refused(response)
        self.regex_rule.refresh_from_db()
        self.assertFalse(self.regex_rule.applies_to_device_interfaces)
