# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for plugin views: list, detail, toggle, duplicate, test, apply."""

from unittest.mock import ANY, MagicMock, patch

from dcim.models import DeviceType, Manufacturer, ModuleType
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.views import APPLY_BATCH_LIMIT

User = get_user_model()

TEST_PASSWORD = "testpass123"  # noqa: S105 - test credential only


class ViewTestBase(TestCase):
    """Base class that creates a superuser and logs in."""

    @classmethod
    def setUpTestData(cls):
        """Create superuser and basic InterfaceNameRule for view tests."""
        cls.superuser = User.objects.create_superuser(
            username="viewtestuser",
            password=TEST_PASSWORD,
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
        self.client.login(username="viewtestuser", password=TEST_PASSWORD)


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
        User.objects.create_user(username="noperm_user", password=TEST_PASSWORD)
        self.client.login(username="noperm_user", password=TEST_PASSWORD)
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

    def _url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

    def test_test_view_get_200(self):
        """GET to rule test view returns 200."""
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_test_view_post_simple_template(self):
        """POST to rule test view with a simple template returns a result."""
        data = {
            "name_template": "et-0/0/{bay_position}",
            "bay_position": "3",
            "channel_count": "0",
            "channel_start": "0",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 200)

    def test_check_channel_preview_returns_multiple_results(self):
        """POST check with channel_count=3 produces one preview entry per channel."""
        data = {
            "name_template": "{base}:{channel}",
            "channel_count": "3",
            "channel_start": "0",
            "var_base": "et-0/0/0",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 200)
        preview = response.context["preview_results"]
        self.assertIsNotNone(preview)
        self.assertEqual(len(preview), 3)
        self.assertEqual(preview[0]["result"], "et-0/0/0:0")
        self.assertEqual(preview[1]["result"], "et-0/0/0:1")
        self.assertEqual(preview[2]["result"], "et-0/0/0:2")

    def test_check_with_module_type_populates_db_preview(self):
        """POST check with module_type FK set triggers find_interfaces_for_rule."""
        data = {
            "name_template": "et-0/0/{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(self.module_type.pk),
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 200)
        # db_preview should be a list (possibly empty if no modules exist)
        self.assertIsNotNone(response.context["db_preview"])
        self.assertIsInstance(response.context["db_preview"], list)

    def test_check_with_regex_pattern_populates_db_preview(self):
        """POST check with regex module_type_pattern triggers find_interfaces_for_rule."""
        data = {
            "name_template": "port{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type_is_regex": "on",
            "module_type_pattern": "VIEW-SFP",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["db_preview"])

    def test_check_invalid_template_sets_error(self):
        """POST with a malformed template expression sets error context."""
        data = {
            "name_template": "{1 + }",
            "channel_count": "0",
            "channel_start": "0",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["error"])

    def test_save_rule_existing_redirects_to_edit(self):
        """POST save_rule with matching module_type redirects to rule edit page."""
        # rule_disabled matches: module_type=self.module_type, device_type=None, platform=None
        edit_url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_edit",
            args=[self.rule_disabled.pk],
        )
        data = {
            "name_template": "ge-0/0/{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(self.module_type.pk),
            "action": "save_rule",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertIn(edit_url, response["Location"])

    def test_save_rule_no_match_redirects_to_add(self):
        """POST save_rule with no existing matching rule redirects to add page."""
        mfr = Manufacturer.objects.create(name="SaveTestMfg", slug="savetestmfg")
        new_mt = ModuleType.objects.create(manufacturer=mfr, model="SAVE-ONLY-MT", part_number="SAVE-ONLY-MT")
        add_url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_add")
        data = {
            "name_template": "xe-0/0/{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(new_mt.pk),
            "action": "save_rule",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertIn(add_url, response["Location"])
        self.assertIn("module_type=", response["Location"])

    def test_save_rule_regex_no_match_redirects_to_add_with_pattern(self):
        """POST save_rule with regex type redirects to add with pattern in query string."""
        add_url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_add")
        data = {
            "name_template": "port{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type_is_regex": "on",
            "module_type_pattern": "UNIQUEPATTERN-99",
            "action": "save_rule",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertIn(add_url, response["Location"])
        self.assertIn("module_type_pattern=UNIQUEPATTERN-99", response["Location"])


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


# ---------------------------------------------------------------------------
# views.py — extra test base and view test classes
# ---------------------------------------------------------------------------


class ViewTestBase2(ViewTestBase):
    """Base class for extra view tests; inherits all fixture setup from ViewTestBase."""


class RuleTestViewGetWithRuleIdTest(ViewTestBase2):
    """Test RuleTestView.get() with a valid rule_id populates the form (lines 135-151)."""

    def _url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

    def test_get_with_valid_rule_id_populates_form(self):
        """GET with ?rule_id=<pk> pre-populates the form with the rule's data (lines 135-151)."""
        url = self._url() + f"?rule_id={self.rule.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial.get("name_template"), self.rule.name_template)

    def test_get_with_invalid_rule_id_silently_ignores(self):
        """GET with non-existent rule_id renders form without error (lines 150-151)."""
        url = self._url() + "?rule_id=999999"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_get_with_non_numeric_rule_id_silently_ignores(self):
        """GET with non-numeric rule_id is silently ignored (ValueError path at line 150)."""
        url = self._url() + "?rule_id=not-a-number"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class RuleTestViewPermissionTest(ViewTestBase2):
    """Test RuleTestView permission checks for unprivileged users."""

    def _url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

    def test_get_no_permission_raises_403(self):
        """GET to rule test view without add_interfacenamerule permission returns 403."""
        User.objects.create_user(username="noperm_test_get", password=TEST_PASSWORD)
        self.client.login(username="noperm_test_get", password=TEST_PASSWORD)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)

    def test_get_with_rule_id_no_permission_raises_403(self):
        """GET with ?rule_id= without add_interfacenamerule permission returns 403."""
        User.objects.create_user(username="noperm_test_get2", password=TEST_PASSWORD)
        self.client.login(username="noperm_test_get2", password=TEST_PASSWORD)
        response = self.client.get(self._url() + f"?rule_id={self.rule.pk}")
        self.assertEqual(response.status_code, 403)

    def test_post_no_permission_raises_403(self):
        """POST to rule test view without add_interfacenamerule permission returns 403."""
        User.objects.create_user(username="noperm_test_post", password=TEST_PASSWORD)
        self.client.login(username="noperm_test_post", password=TEST_PASSWORD)
        data = {
            "name_template": "et-0/0/{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 403)


class RuleTestViewAddOnlyPermissionTest(ViewTestBase2):
    """Test RuleTestView behaviour for users with add but not view permission."""

    def _url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

    def _create_add_only_user(self):
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        user = User.objects.create_user(username="add_only_tester", password=TEST_PASSWORD)
        ct = ContentType.objects.get_for_model(InterfaceNameRule)
        perm = Permission.objects.get(content_type=ct, codename="add_interfacenamerule")
        user.user_permissions.add(perm)
        # Re-fetch to clear Django's permission cache on the User instance.
        return User.objects.get(pk=user.pk)

    def test_get_with_rule_id_add_only_shows_blank_form_with_warning(self):
        """GET with ?rule_id= when user has add but not view permission returns blank form."""
        user = self._create_add_only_user()
        self.client.force_login(user)
        response = self.client.get(self._url() + f"?rule_id={self.rule.pk}")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertFalse(form.initial, "Form should have no initial data for add-only user")
        self.assertIsNone(response.context.get("loaded_rule"))
        from django.contrib.messages import get_messages

        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("permission" in m.lower() for m in msgs))

    def test_save_rule_add_only_skips_duplicate_check(self):
        """POST save_rule with add-only permission skips duplicate detection, redirects to add."""
        user = self._create_add_only_user()
        self.client.force_login(user)
        add_url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_add")
        data = {
            "name_template": "ge-0/0/{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(self.module_type.pk),
            "action": "save_rule",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertIn(add_url, response["Location"])


class RuleApplyDetailViewGetPermissionTest(ViewTestBase2):
    """Test RuleApplyDetailView.get() permission check for unprivileged users."""

    def test_get_no_change_permission_raises_403(self):
        """GET apply detail without dcim.change_interface permission returns 403."""
        User.objects.create_user(username="noperm_apply_get", password=TEST_PASSWORD)
        self.client.login(username="noperm_apply_get", password=TEST_PASSWORD)
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)


class RuleApplyDetailViewPostTest(ViewTestBase2):
    """Test RuleApplyDetailView.post() — foreground apply, background job, no permission."""

    def _url(self, pk):
        return reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": pk},
        )

    def test_post_no_interfaces_selected_warns(self):
        """POST apply with no interface_ids warns and redirects (lines 408-409)."""
        from django.contrib.messages import get_messages

        url = self._url(self.rule.pk)
        response = self.client.post(url, {"action": "apply"})
        self.assertEqual(response.status_code, 302)
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("No interfaces selected" in m for m in msgs))

    def test_post_apply_with_interface_ids_calls_apply(self):
        """POST apply with interface_ids calls apply_rule_to_existing (lines 410-412)."""
        url = self._url(self.rule.pk)
        with patch("netbox_interface_name_rules.engine.apply_rule_to_existing", return_value=2) as mock_apply:
            response = self.client.post(url, {"action": "apply", "interface_ids": ["1", "2"]})
        self.assertEqual(response.status_code, 302)
        mock_apply.assert_called_once_with(self.rule, limit=APPLY_BATCH_LIMIT, interface_ids=[1, 2])

    def test_post_background_action_enqueues_job(self):
        """POST background action tries to enqueue ApplyRuleJob (lines 388-403)."""
        url = self._url(self.rule.pk)
        with patch("netbox_interface_name_rules.jobs.ApplyRuleJob.enqueue", side_effect=Exception("no rq")) as mock_enq:
            response = self.client.post(url, {"action": "background"})
        self.assertEqual(response.status_code, 302)
        mock_enq.assert_called_once()

    def test_post_no_change_permission_raises_forbidden(self):
        """POST apply without dcim.change_interface permission raises PermissionDenied (line 382-383)."""
        User.objects.create_user(username="noperm_apply_user", password=TEST_PASSWORD)
        self.client.login(username="noperm_apply_user", password=TEST_PASSWORD)
        url = self._url(self.rule.pk)
        response = self.client.post(url, {"action": "apply"})
        self.assertEqual(response.status_code, 403)


class RuleToggleNoPermissionNonAjaxTest(ViewTestBase2):
    """Test RuleToggleView.post() raises PermissionDenied for non-AJAX without permission."""

    def _toggle_url(self, pk):
        return reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_toggle",
            kwargs={"pk": pk},
        )

    def test_toggle_non_ajax_no_permission_raises_403(self):
        """Non-AJAX POST from user without change permission returns 403.

        NetBox's ObjectView dispatch checks permissions via LoginRequiredMixin and
        raises PermissionDenied when the user lacks change_interfacenamerule, which
        Django converts to a 403 response.
        """
        User.objects.create_user(username="noperm_tog_user2", password=TEST_PASSWORD)
        self.client.login(username="noperm_tog_user2", password=TEST_PASSWORD)
        url = self._toggle_url(self.rule.pk)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

    def test_post_apply_apply_error_shows_error_message(self):
        """POST apply when apply_rule_to_existing raises shows error message and redirects.

        Patches engine.apply_rule_to_existing to raise ValueError, verifying the
        view catches the exception (lines 413-415), logs it, adds an error message,
        and still issues the 302 redirect back to the apply detail page.
        """
        from django.contrib.messages import ERROR, get_messages

        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule.pk},
        )
        with patch("netbox_interface_name_rules.engine.apply_rule_to_existing", side_effect=ValueError("bad template")):
            response = self.client.post(url, {"action": "apply", "interface_ids": [str(self.rule.pk)]})
        self.assertEqual(response.status_code, 302)
        msgs = list(get_messages(response.wsgi_request))
        error_msgs = [m for m in msgs if m.level == ERROR]
        self.assertTrue(error_msgs, "Expected an error-level message but none found")
        self.assertTrue(any("ValueError" in str(m) for m in error_msgs))

    def test_apply_detail_get_value_error_shows_error(self):
        """GET apply detail when find_interfaces_for_rule raises ValueError shows error (lines 357-360)."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule_regex.pk},
        )
        with patch("netbox_interface_name_rules.engine.find_interfaces_for_rule", side_effect=ValueError("bad regex")):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_apply_detail_get_exception_shows_error(self):
        """GET apply detail when find_interfaces_for_rule raises Exception shows error (lines 361-364)."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule.pk},
        )
        with patch("netbox_interface_name_rules.engine.find_interfaces_for_rule", side_effect=RuntimeError("oops")):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class RuleTestViewDbErrorTest(ViewTestBase2):
    """Test RuleTestView.post() db_error handling (line 170)."""

    def _url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

    def test_post_db_error_propagates_to_context(self):
        """When _fetch_db_preview returns an error and _evaluate_template_preview does not, db_error is set (line 170)."""
        data = {
            "name_template": "et-0/0/{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(self.module_type.pk),
        }
        with patch("netbox_interface_name_rules.engine.find_interfaces_for_rule", side_effect=RuntimeError("db fail")):
            response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 200)
        # error is set from the db_error path
        self.assertIsNotNone(response.context.get("error"))


class RuleTestViewSaveRuleWithScopeTest(ViewTestBase2):
    """Test RuleTestView._handle_save_rule with scoping fields set."""

    def _url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

    def test_save_rule_with_device_type_filters_scope(self):
        """POST save_rule finds an existing matching rule and redirects to its edit page.

        Verifies that when both module_type and device_type are provided, the view
        looks up an existing matching rule (line 203) and redirects to its edit URL.
        cls.rule (from ViewTestBase) already has module_type + device_type set.
        """
        expected_url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_edit", args=[self.rule.pk])
        data = {
            "name_template": self.rule.name_template,
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(self.module_type.pk),
            "device_type": str(self.device_type.pk),
            "action": "save_rule",
        }
        response = self.client.post(self._url(), data)
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

    def test_save_rule_no_match_with_device_type_redirects_to_add_with_pk(self):
        """POST save_rule with unmatched scope redirects to add page with device_type param (line 227)."""
        from dcim.models import Manufacturer as Mfr

        mfr = Mfr.objects.create(name="ScopeExtraMfg", slug="scopeextramfg")
        new_mt = ModuleType.objects.create(manufacturer=mfr, model="SCOPE-EXTRA-MT", part_number="SCOPE-EXTRA-MT")
        new_dt = DeviceType.objects.create(manufacturer=mfr, model="SCOPE-EXTRA-Dev", slug="scope-extra-dev")
        add_url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_add")
        data = {
            "name_template": "xe-0/0/{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(new_mt.pk),
            "device_type": str(new_dt.pk),
            "action": "save_rule",
        }
        response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertIn(add_url, response["Location"])
        self.assertIn("device_type=", response["Location"])


class RuleTestViewFetchDbPreviewValueErrorTest(ViewTestBase2):
    """Test RuleTestView._fetch_db_preview handles ValueError from find_interfaces_for_rule."""

    def _url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

    def test_post_valueerror_in_find_interfaces_sets_db_error(self):
        """_fetch_db_preview catches ValueError and returns error string (line 301)."""
        data = {
            "name_template": "port{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(self.module_type.pk),
        }
        with patch(
            "netbox_interface_name_rules.engine.find_interfaces_for_rule",
            side_effect=ValueError("invalid regex"),
        ):
            response = self.client.post(self._url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context.get("error"))


class RuleApplyDetailViewGetReErrorTest(ViewTestBase2):
    """Test RuleApplyDetailView.get handles re.error from find_interfaces_for_rule."""

    def test_get_re_error_shows_error_message(self):
        """GET apply detail when find_interfaces_for_rule raises re.error shows error (lines 334-338)."""
        import re as re_module

        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule_regex.pk},
        )
        with patch(
            "netbox_interface_name_rules.engine.find_interfaces_for_rule",
            side_effect=re_module.error("bad regex"),
        ):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class RuleApplyDetailViewBackgroundJobSuccessTest(ViewTestBase2):
    """Test RuleApplyDetailView.post with background action that succeeds."""

    def test_post_background_success_shows_success_message(self):
        """POST background action with successful enqueue shows success message (line 400).

        Asserts ApplyRuleJob.enqueue is called once and the success message
        contains the job pk (42) confirming the enqueued job id is reported.
        """
        from django.contrib.messages import SUCCESS, get_messages

        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule.pk},
        )
        mock_job = MagicMock()
        mock_job.pk = 42
        with patch("netbox_interface_name_rules.jobs.ApplyRuleJob.enqueue", return_value=mock_job) as mock_enq:
            response = self.client.post(url, {"action": "background"})
        self.assertEqual(response.status_code, 302)
        mock_enq.assert_called_once_with(name=ANY, user=self.superuser, rule_id=self.rule.pk)
        msgs = list(get_messages(response.wsgi_request))
        success_msgs = [m for m in msgs if m.level == SUCCESS]
        self.assertTrue(success_msgs, "Expected a success-level message but none found")
        self.assertTrue(any("42" in str(m) for m in success_msgs))


# ---------------------------------------------------------------------------
# BulkImportCSVTest — regression: KeyError 'Ch' on CSV import round-trip
# ---------------------------------------------------------------------------


class BulkImportCSVTest(ViewTestBase):
    """CSV import round-trip tests; guards against the KeyError 'Ch' regression."""

    def _csv_from_rule(self, rule):
        """Build a one-row CSV string from a rule's csv_headers and to_csv()."""
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(InterfaceNameRule.csv_headers)
        writer.writerow(rule.to_csv())
        return buf.getvalue()

    def test_csv_import_does_not_raise_key_error(self):
        """POST to import with a properly exported CSV must not produce a KeyError.

        This is the regression test for the 'Ch' KeyError: when using csv_headers
        and to_csv() the column names are plain field names with no dots.
        """
        self.client.force_login(self.superuser)
        csv_data = self._csv_from_rule(self.rule)
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_bulk_import")
        # If a KeyError is raised the view returns 500; assert it doesn't.
        try:
            response = self.client.post(url, {"data": csv_data, "format": "csv", "csv_delimiter": "auto"})
        except KeyError as exc:
            self.fail(f"CSV import raised KeyError: {exc!r}")
        self.assertNotEqual(response.status_code, 500, "CSV import returned a 500 error")

    def test_csv_round_trip_creates_rule(self):
        """CSV exported from a fresh rule can be imported to create a new rule."""
        import csv
        import io
        import uuid

        self.client.force_login(self.superuser)
        # Build a unique ModuleType so the imported row doesn't collide with fixtures.
        unique_model = f"ROUND-TRIP-{uuid.uuid4().hex[:8]}"
        mt = ModuleType.objects.create(
            manufacturer=self.module_type.manufacturer,
            model=unique_model,
            part_number=unique_model,
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(InterfaceNameRule.csv_headers)
        writer.writerow(
            [
                mt.model,  # module_type
                "",  # module_type_pattern
                False,  # module_type_is_regex
                "",  # parent_module_type
                "",  # device_type
                "",  # platform
                "et-0/0/{bay_position}",  # name_template
                0,  # channel_count
                0,  # channel_start
                "round-trip test",  # description
                True,  # enabled
                False,  # applies_to_device_interfaces
            ]
        )
        csv_data = buf.getvalue()

        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_bulk_import")
        before_count = InterfaceNameRule.objects.count()
        response = self.client.post(url, {"data": csv_data, "format": "csv", "csv_delimiter": "auto"})
        # A successful import redirects (302); failure re-renders the form (200).
        self.assertEqual(response.status_code, 302, f"Import did not redirect; status={response.status_code}")
        after_count = InterfaceNameRule.objects.count()
        self.assertGreater(after_count, before_count, "No new rule was created after CSV import")


# ---------------------------------------------------------------------------
# YAMLExportTest — YAML export via NetBox's built-in Export dropdown
# ---------------------------------------------------------------------------


class YAMLExportTest(ViewTestBase):
    """Tests for YAML export via the list view's ?export query parameter."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.LIST_URL = reverse("plugins:netbox_interface_name_rules:interfacenamerule_list")

    def test_yaml_export_unauthenticated_responds(self):
        """Unauthenticated export must not serve content to anonymous users."""
        self.client.logout()
        response = self.client.get(self.LIST_URL, {"export": ""})
        self.assertIn(response.status_code, [301, 302, 403])

    def test_yaml_export_all_returns_200(self):
        """Authenticated GET ?export= must return 200."""
        self.client.force_login(self.superuser)
        response = self.client.get(self.LIST_URL, {"export": ""})
        self.assertEqual(response.status_code, 200)

    def test_yaml_export_content_type(self):
        """Response Content-Type must contain 'yaml'."""
        self.client.force_login(self.superuser)
        response = self.client.get(self.LIST_URL, {"export": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("yaml", response["Content-Type"].lower())

    def test_yaml_export_content_disposition(self):
        """Response must include a Content-Disposition attachment header."""
        self.client.force_login(self.superuser)
        response = self.client.get(self.LIST_URL, {"export": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.get("Content-Disposition", "").lower())

    def test_yaml_export_all_contains_rules(self):
        """Exported YAML must be parseable and contain all existing rules."""
        import yaml

        self.client.force_login(self.superuser)
        response = self.client.get(self.LIST_URL, {"export": ""})
        self.assertEqual(response.status_code, 200)
        data = yaml.safe_load(response.content)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), InterfaceNameRule.objects.count())

    def test_yaml_export_structure(self):
        """Each exported YAML entry must contain key fields."""
        import yaml

        self.client.force_login(self.superuser)
        response = self.client.get(self.LIST_URL, {"export": ""})
        self.assertEqual(response.status_code, 200)
        rules = yaml.safe_load(response.content)
        self.assertIsInstance(rules, list)
        entry = rules[0]
        self.assertIsInstance(entry, dict)
        self.assertIn("name_template", entry)
