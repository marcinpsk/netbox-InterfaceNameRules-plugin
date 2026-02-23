# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the REST API endpoints."""

from dcim.models import DeviceType, Manufacturer, ModuleType
from rest_framework import status
from utilities.testing import APITestCase

from netbox_interface_name_rules.models import InterfaceNameRule


class InterfaceNameRuleAPITest(APITestCase):
    """Test CRUD operations on InterfaceNameRule via REST API."""

    model = InterfaceNameRule
    view_namespace = "plugins-api:netbox_interface_name_rules"
    user_permissions = (
        "netbox_interface_name_rules.view_interfacenamerule",
        "netbox_interface_name_rules.add_interfacenamerule",
        "netbox_interface_name_rules.change_interfacenamerule",
        "netbox_interface_name_rules.delete_interfacenamerule",
    )

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="APIMfg", slug="apimfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="API-SFP", part_number="API-SFP")
        cls.module_type2 = ModuleType.objects.create(
            manufacturer=manufacturer, model="API-QSFP", part_number="API-QSFP"
        )
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="API-DEV", slug="api-dev")

        cls.rule1 = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            device_type=cls.device_type,
            name_template="et-0/0/{bay_position}",
            description="Test rule 1",
        )
        cls.rule2 = InterfaceNameRule.objects.create(
            module_type=cls.module_type2,
            name_template="swp{bay_position_num}",
            description="Test rule 2",
        )
        cls.rule3 = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}",
            description="Test rule 3",
        )

    def test_list_rules(self):
        url = self._get_list_url()
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 3)

    def test_get_rule(self):
        url = self._get_detail_url(self.rule1)
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name_template"], "et-0/0/{bay_position}")

    def test_create_rule(self):
        url = self._get_list_url()
        data = {
            "module_type": self.module_type2.pk,
            "device_type": self.device_type.pk,
            "name_template": "port{bay_position}",
            "channel_count": 0,
            "channel_start": 0,
        }
        response = self.client.post(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            InterfaceNameRule.objects.filter(module_type=self.module_type2, device_type=self.device_type).exists()
        )

    def test_update_rule(self):
        url = self._get_detail_url(self.rule1)
        data = {"name_template": "ge-0/0/{bay_position}"}
        response = self.client.patch(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.rule1.refresh_from_db()
        self.assertEqual(self.rule1.name_template, "ge-0/0/{bay_position}")

    def test_delete_rule(self):
        url = self._get_detail_url(self.rule3)
        response = self.client.delete(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(InterfaceNameRule.objects.filter(pk=self.rule3.pk).exists())

    def test_create_regex_rule(self):
        url = self._get_list_url()
        data = {
            "module_type_is_regex": True,
            "module_type_pattern": "QSFP-100G-.*",
            "device_type": self.device_type.pk,
            "name_template": "et-0/0/{bay_position}",
            "channel_count": 0,
            "channel_start": 0,
        }
        response = self.client.post(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["module_type_is_regex"])
        self.assertEqual(response.data["module_type_pattern"], "QSFP-100G-.*")
        self.assertIsNone(response.data["module_type"])

    def test_create_regex_rule_without_pattern_fails(self):
        url = self._get_list_url()
        data = {
            "module_type_is_regex": True,
            "module_type_pattern": "",
            "name_template": "et-0/0/{bay_position}",
            "channel_count": 0,
            "channel_start": 0,
        }
        response = self.client.post(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_rule_to_regex(self):
        """PATCH a rule to switch it to regex mode."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type2,
            device_type=self.device_type,
            name_template="port{bay_position}",
        )
        url = self._get_detail_url(rule)
        data = {
            "module_type": None,
            "module_type_is_regex": True,
            "module_type_pattern": "QSFP-.*",
            "name_template": "port{bay_position}",
        }
        response = self.client.patch(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rule.refresh_from_db()
        self.assertTrue(rule.module_type_is_regex)
        self.assertEqual(rule.module_type_pattern, "QSFP-.*")
