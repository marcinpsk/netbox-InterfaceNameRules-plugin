# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for plugin views: list, detail, toggle, duplicate, test, apply."""

from dcim.models import DeviceType, Manufacturer, ModuleType
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_interface_name_rules.models import InterfaceNameRule

User = get_user_model()


class ViewTestBase(TestCase):
    """Base class that creates a superuser and logs in."""

    @classmethod
    def setUpTestData(cls):
        """Create superuser and basic InterfaceNameRule for view tests."""
        cls.superuser = User.objects.create_superuser(
            username="viewtestuser",
            password="testpass123",
            email="viewtest@example.com",
        )
        manufacturer = Manufacturer.objects.create(name="ViewMfg", slug="viewmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="VIEW-SFP", part_number="VIEW-SFP")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="VIEW-Dev", slug="view-dev")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            device_type=cls.device_type,
            name_template="et-0/0/{bay_position}",
            description="View test rule",
        )
        cls.rule_disabled = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="ge-0/0/{bay_position}",
            enabled=False,
        )
        cls.rule_regex = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-.*",
            name_template="port{bay_position}",
        )

    def setUp(self):
        """Log in the superuser before each test."""
        self.client.login(username="viewtestuser", password="testpass123")


class RuleListViewTest(ViewTestBase):
    """Test the InterfaceNameRule list view."""

    def test_list_view_200(self):
        """List view returns 200 OK."""
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_list_view_unauthenticated_redirects(self):
        """Unauthenticated access to list view redirects to login."""
        self.client.logout()
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_list")
        response = self.client.get(url)
        self.assertIn(response.status_code, [302, 403])


class RuleDetailViewTest(ViewTestBase):
    """Test the InterfaceNameRule detail view."""

    def test_detail_view_200(self):
        """Detail view returns 200 OK for a valid rule PK."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_detail",
            kwargs={"pk": self.rule.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_detail_view_404_for_missing(self):
        """Detail view returns 404 for non-existent rule PK."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_detail",
            kwargs={"pk": 999999},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


class RuleToggleViewTest(ViewTestBase):
    """Test RuleToggleView (POST /rules/<pk>/toggle/)."""

    def _toggle_url(self, pk):
        return reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_toggle",
            kwargs={"pk": pk},
        )

    def test_toggle_enables_disabled_rule(self):
        """POST to toggle endpoint enables a disabled rule."""
        url = self._toggle_url(self.rule_disabled.pk)
        response = self.client.post(
            url, HTTP_REFERER=reverse("plugins:netbox_interface_name_rules:interfacenamerule_list")
        )
        self.assertIn(response.status_code, [200, 302])
        self.rule_disabled.refresh_from_db()
        self.assertTrue(self.rule_disabled.enabled)

    def test_toggle_disables_enabled_rule(self):
        """POST to toggle endpoint disables an enabled rule."""
        url = self._toggle_url(self.rule.pk)
        self.client.post(url, HTTP_REFERER=reverse("plugins:netbox_interface_name_rules:interfacenamerule_list"))
        self.rule.refresh_from_db()
        self.assertFalse(self.rule.enabled)

    def test_toggle_ajax_returns_json(self):
        """AJAX POST to toggle returns JSON with new enabled state."""
        url = self._toggle_url(self.rule_regex.pk)
        response = self.client.post(
            url,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("enabled", data)

    def test_toggle_ajax_no_permission_returns_403(self):
        """Authenticated user without change permission gets 403 on AJAX toggle."""
        # Create a regular user without permissions
        User.objects.create_user(username="noperm_user", password="testpass123")
        self.client.login(username="noperm_user", password="testpass123")
        url = self._toggle_url(self.rule.pk)
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 403)

    def test_toggle_404_for_missing_rule(self):
        """Toggle view returns 404 for non-existent rule PK."""
        url = self._toggle_url(999999)
        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 404)

    def test_toggle_redirect_no_referer(self):
        """Without Referer header, toggle redirects to list view."""
        url = self._toggle_url(self.rule_disabled.pk)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("rules", response["Location"])


class RuleApplyListViewTest(ViewTestBase):
    """Test the Apply Rules list view."""

    def test_apply_list_view_200(self):
        """Apply rules list view returns 200 OK."""
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_apply")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class RuleApplyDetailViewTest(ViewTestBase):
    """Test the per-rule Apply detail view."""

    def test_apply_detail_view_200(self):
        """Apply detail view returns 200 OK for a valid rule."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class RuleApplicableViewTest(ViewTestBase):
    """Test the RuleApplicableView (AJAX applicable check)."""

    def test_applicable_view_returns_json(self):
        """Applicable view returns JSON with an 'applicable' key."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_applicable",
            kwargs={"pk": self.rule.pk},
        )
        response = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("applicable", data)


class RuleTestViewTest(ViewTestBase):
    """Test the RuleTestView (build-rule / preview)."""

    def test_test_view_get_200(self):
        """GET to rule test view returns 200."""
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_test_view_post_simple_template(self):
        """POST to rule test view with a simple template returns a result."""
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")
        data = {
            "name_template": "et-0/0/{bay_position}",
            "bay_position": "3",
            "channel_count": "0",
            "channel_start": "0",
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)


class RuleDuplicateViewTest(ViewTestBase):
    """Test the RuleDuplicateView."""

    def test_duplicate_redirects_to_add_with_params(self):
        """GET to duplicate view redirects to add view with cloned fields."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_duplicate",
            kwargs={"pk": self.rule.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("add", response["Location"])
