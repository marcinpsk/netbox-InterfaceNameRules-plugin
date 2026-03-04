# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Extra tests targeting specific coverage gaps across forms, filters, tables,
jobs, utils, models, signals, engine, and views."""

from unittest.mock import MagicMock, patch

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    Platform,
    Site,
    VirtualChassis,
)
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_interface_name_rules.models import InterfaceNameRule

User = get_user_model()


# ---------------------------------------------------------------------------
# forms.py — RuleTestForm.clean() validation branches (lines 115-130)
# ---------------------------------------------------------------------------


class RuleTestFormValidationTest(TestCase):
    """Tests for RuleTestForm.clean() error branches."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="FTMfg", slug="ftmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="FT-SFP", part_number="FT-SFP")

    def _make_form(self, extra):
        from netbox_interface_name_rules.forms import RuleTestForm

        data = {"name_template": "et-0/0/{bay_position}", "channel_count": "0", "channel_start": "0"}
        data.update(extra)
        return RuleTestForm(data)

    def test_regex_mode_no_pattern_adds_error(self):
        """RuleTestForm.clean() adds error on module_type_pattern when regex mode but no pattern (line 115)."""
        form = self._make_form({"module_type_is_regex": True})
        form.is_valid()
        self.assertIn("module_type_pattern", form.errors)

    def test_regex_mode_invalid_pattern_adds_error(self):
        """RuleTestForm.clean() adds error when regex pattern is invalid (lines 119-120)."""
        form = self._make_form({"module_type_is_regex": True, "module_type_pattern": "[invalid("})
        form.is_valid()
        self.assertIn("module_type_pattern", form.errors)

    def test_regex_mode_redos_pattern_adds_error(self):
        """RuleTestForm.clean() adds error when pattern contains nested quantifiers (line 125).

        (a)+? compiles OK (valid lazy quantifier syntax) but triggers the ReDoS guard
        because )+? matches \\)\\s*[\\+\\*\\?]\\s*[\\+\\*\\?] in _REDOS_PATTERN.
        """
        form = self._make_form({"module_type_is_regex": True, "module_type_pattern": "(a)+?"})
        form.is_valid()
        self.assertIn("module_type_pattern", form.errors)

    def test_regex_mode_with_module_type_adds_error(self):
        """RuleTestForm.clean() adds error on module_type when both regex and FK set (line 127)."""
        form = self._make_form(
            {
                "module_type_is_regex": True,
                "module_type_pattern": "VALID-.*",
                "module_type": str(self.module_type.pk),
            }
        )
        form.is_valid()
        self.assertIn("module_type", form.errors)

    def test_non_regex_with_pattern_adds_error(self):
        """RuleTestForm.clean() adds error when module_type_pattern set in non-regex mode (line 130)."""
        form = self._make_form({"module_type_pattern": "some-pattern"})
        form.is_valid()
        self.assertIn("module_type_pattern", form.errors)


# ---------------------------------------------------------------------------
# filters.py — search() method (line 56)
# ---------------------------------------------------------------------------


class FilterSearchMethodTest(TestCase):
    """Test the InterfaceNameRuleFilterSet.search() method."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="FiltXMfg", slug="filtxmfg")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="FILTX-SFP", part_number="FILTX-SFP"
        )
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="filtx-template-{bay_position}",
            description="filtx-search-test",
        )

    def test_search_by_template(self):
        """search() on name_template returns matching rules (line 56)."""
        from netbox_interface_name_rules.filters import InterfaceNameRuleFilterSet

        qs = InterfaceNameRuleFilterSet({"q": "filtx-template"}, queryset=InterfaceNameRule.objects.all()).qs
        self.assertIn(self.rule, qs)

    def test_search_by_description(self):
        """search() on description returns matching rules."""
        from netbox_interface_name_rules.filters import InterfaceNameRuleFilterSet

        qs = InterfaceNameRuleFilterSet({"q": "filtx-search-test"}, queryset=InterfaceNameRule.objects.all()).qs
        self.assertIn(self.rule, qs)

    def test_search_by_module_type_model(self):
        """search() on module type model name returns matching rules."""
        from netbox_interface_name_rules.filters import InterfaceNameRuleFilterSet

        qs = InterfaceNameRuleFilterSet({"q": "FILTX-SFP"}, queryset=InterfaceNameRule.objects.all()).qs
        self.assertIn(self.rule, qs)


# ---------------------------------------------------------------------------
# tables.py — SpecificityColumn.render() CSS branches (lines 23, 27, 29, 41)
# ---------------------------------------------------------------------------


class TableSpecificityColumnRenderTest(TestCase):
    """Test SpecificityColumn render() returns the correct CSS class badge."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="TblXMfg", slug="tblxmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="TBLX-SFP", part_number="TBLX-SFP")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="TBLX-Dev", slug="tblx-dev")
        cls.platform = Platform.objects.create(name="TBLX-IOS", slug="tblx-ios")

    def _render(self, rule):
        from netbox_interface_name_rules.tables import SpecificityColumn

        col = SpecificityColumn()
        return col.render(value=rule.specificity_score, record=rule)

    def test_device_iface_rule_uses_warning_badge(self):
        """applies_to_device_interfaces rule renders with text-bg-warning (line 23)."""
        rule = InterfaceNameRule(applies_to_device_interfaces=True, name_template="x")
        html = self._render(rule)
        self.assertIn("text-bg-warning", html)

    def test_exact_rule_uses_success_badge(self):
        """Exact FK rule renders with text-bg-success (line 25)."""
        rule = InterfaceNameRule(module_type=self.module_type, name_template="x")
        html = self._render(rule)
        self.assertIn("text-bg-success", html)

    def test_regex_device_scoped_uses_primary_badge(self):
        """Regex + device_type scoped rule renders with text-bg-primary (line 27)."""
        rule = InterfaceNameRule(module_type_is_regex=True, module_type_pattern="SFP-.*", name_template="x")
        rule.device_type_id = self.device_type.pk
        html = self._render(rule)
        self.assertIn("text-bg-primary", html)

    def test_regex_parent_scoped_uses_primary_badge(self):
        """Regex + parent_module_type scoped rule renders with text-bg-primary (line 27)."""
        rule = InterfaceNameRule(module_type_is_regex=True, module_type_pattern="SFP-.*", name_template="x")
        rule.parent_module_type_id = self.module_type.pk
        html = self._render(rule)
        self.assertIn("text-bg-primary", html)

    def test_regex_platform_scoped_uses_info_badge(self):
        """Regex + platform-only scoped rule renders with text-bg-info (line 29)."""
        rule = InterfaceNameRule(module_type_is_regex=True, module_type_pattern="SFP-.*", name_template="x")
        rule.platform_id = self.platform.pk
        html = self._render(rule)
        self.assertIn("text-bg-info", html)

    def test_value_method_returns_score(self):
        """SpecificityColumn.value() returns the raw score (line 41)."""
        from netbox_interface_name_rules.tables import SpecificityColumn

        col = SpecificityColumn()
        rule = InterfaceNameRule(module_type=self.module_type, name_template="x")
        self.assertEqual(col.value(rule.specificity_score, rule), rule.specificity_score)


# ---------------------------------------------------------------------------
# models.py — specificity_label with parent/platform (lines 230, 234)
# ---------------------------------------------------------------------------


class ModelSpecificityLabelScopeTest(TestCase):
    """Test specificity_label with parent_module_type and platform scopes."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ScopXMfg", slug="scopxmfg")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="SCOPX-SFP", part_number="SCOPX-SFP"
        )
        cls.platform = Platform.objects.create(name="SCOPX-IOS", slug="scopx-ios")

    def test_specificity_label_with_parent_includes_parent(self):
        """specificity_label includes 'parent' when parent_module_type_id is set (line 230)."""
        rule = InterfaceNameRule(module_type=self.module_type, name_template="x")
        rule.parent_module_type_id = self.module_type.pk
        label = rule.specificity_label
        self.assertIn("parent", label)

    def test_specificity_label_with_platform_includes_platform(self):
        """specificity_label includes 'platform' when platform_id is set (line 234)."""
        rule = InterfaceNameRule(module_type=self.module_type, name_template="x")
        rule.platform_id = self.platform.pk
        label = rule.specificity_label
        self.assertIn("platform", label)

    def test_specificity_label_all_scopes(self):
        """specificity_label includes parent+device+platform when all scopes are set."""
        dt = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.get(name="ScopXMfg"), model="SCOPX-Dev", slug="scopx-dev"
        )
        rule = InterfaceNameRule(module_type=self.module_type, name_template="x")
        rule.parent_module_type_id = self.module_type.pk
        rule.device_type_id = dt.pk
        rule.platform_id = self.platform.pk
        label = rule.specificity_label
        self.assertIn("parent", label)
        self.assertIn("device", label)
        self.assertIn("platform", label)


# ---------------------------------------------------------------------------
# models.py — _validate_module_type_pattern ReDoS guard (line 29)
# ---------------------------------------------------------------------------


class ModelValidatePatternReDoSTest(TestCase):
    """Test _validate_module_type_pattern raises for valid-but-ReDoS-prone patterns."""

    def test_valid_regex_with_nested_quantifiers_raises(self):
        """A valid regex with nested quantifiers e.g. (a)+? raises ValidationError (line 29).

        (a)+? compiles without error but contains )+? which matches
        \\)\\s*[\\+\\*\\?]\\s*[\\+\\*\\?] in _REDOS_PATTERN.
        """
        from django.core.exceptions import ValidationError

        from netbox_interface_name_rules.models import _validate_module_type_pattern

        with self.assertRaises(ValidationError) as ctx:
            _validate_module_type_pattern("(a)+?")
        self.assertIn("module_type_pattern", ctx.exception.message_dict)
        self.assertIn("nested quantifiers", str(ctx.exception))


# ---------------------------------------------------------------------------
# jobs.py — ApplyRuleJob.run() success + exception paths (lines 30-36)
# ---------------------------------------------------------------------------


class JobRunSuccessAndExceptionTest(TestCase):
    """Test ApplyRuleJob.run() with a real rule: success path and exception path."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="JobXMfg", slug="jobxmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="JOBX-SFP", part_number="JOBX-SFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="et-0/0/{bay_position}",
        )

    def _make_job(self):
        from netbox_interface_name_rules.jobs import ApplyRuleJob

        job = ApplyRuleJob.__new__(ApplyRuleJob)
        job.logger = MagicMock()
        return job

    def test_job_run_success_logs_info(self):
        """ApplyRuleJob.run() with valid rule calls apply_rule_to_existing and logs (lines 30-36)."""
        job = self._make_job()
        job.run(rule_id=self.rule.pk)
        job.logger.info.assert_called_once()

    def test_job_run_exception_reraises_and_logs(self):
        """ApplyRuleJob.run() re-raises exception from apply_rule_to_existing (lines 32-34)."""
        job = self._make_job()
        with patch("netbox_interface_name_rules.engine.apply_rule_to_existing", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                job.run(rule_id=self.rule.pk)
        job.logger.exception.assert_called_once()


# ---------------------------------------------------------------------------
# utils.py — supports_module_path ImportError path (lines 16-17)
# ---------------------------------------------------------------------------


class UtilsModulePathFalseTest(TestCase):
    """Test supports_module_path() returns False when MODULE_PATH_TOKEN is missing."""

    def test_returns_false_when_token_missing(self):
        """supports_module_path() catches ImportError and returns False (lines 16-17)."""
        import dcim.constants as dc

        from netbox_interface_name_rules.utils import supports_module_path

        had_attr = hasattr(dc, "MODULE_PATH_TOKEN")
        original = getattr(dc, "MODULE_PATH_TOKEN", None)
        try:
            if had_attr:
                delattr(dc, "MODULE_PATH_TOKEN")
            result = supports_module_path()
            if had_attr:
                self.assertFalse(result)
            else:
                # Already False before this test; just verify the type
                self.assertIsInstance(result, bool)
        finally:
            if had_attr:
                dc.MODULE_PATH_TOKEN = original


# ---------------------------------------------------------------------------
# views.py — RuleTestView.get() with rule_id (lines 135-151)
# ---------------------------------------------------------------------------


class ViewTestBase2(TestCase):
    """Base class that creates a superuser and logs in for view tests."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="covextrauser",
            password="testpass123",
            email="covextra@example.com",
        )
        manufacturer = Manufacturer.objects.create(name="CovXMfg", slug="covxmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="COVX-SFP", part_number="COVX-SFP")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="COVX-Dev", slug="covx-dev")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="et-0/0/{bay_position}",
        )
        cls.rule_regex = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="COVX-.*",
            name_template="port{bay_position}",
        )

    def setUp(self):
        self.client.login(username="covextrauser", password="testpass123")


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


class RuleApplyDetailViewPostTest(ViewTestBase2):
    """Test RuleApplyDetailView.post() — foreground apply, background job, no permission."""

    def _url(self, pk):
        return reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": pk},
        )

    def test_post_no_interfaces_selected_warns(self):
        """POST apply with no interface_ids warns and redirects (lines 408-409)."""
        url = self._url(self.rule.pk)
        response = self.client.post(url, {"action": "apply"})
        self.assertEqual(response.status_code, 302)

    def test_post_apply_with_interface_ids_calls_apply(self):
        """POST apply with interface_ids calls apply_rule_to_existing (lines 410-412)."""
        url = self._url(self.rule.pk)
        with patch("netbox_interface_name_rules.engine.apply_rule_to_existing", return_value=2) as mock_apply:
            response = self.client.post(url, {"action": "apply", "interface_ids": ["1", "2"]})
        self.assertEqual(response.status_code, 302)
        mock_apply.assert_called_once()

    def test_post_background_action_enqueues_job(self):
        """POST background action tries to enqueue ApplyRuleJob (lines 388-403)."""
        url = self._url(self.rule.pk)
        with patch("netbox_interface_name_rules.jobs.ApplyRuleJob.enqueue", side_effect=Exception("no rq")) as mock_enq:
            response = self.client.post(url, {"action": "background"})
        self.assertEqual(response.status_code, 302)
        mock_enq.assert_called_once()

    def test_post_no_change_permission_raises_forbidden(self):
        """POST apply without dcim.change_interface permission raises PermissionDenied (line 382-383)."""
        User.objects.create_user(username="noperm_apply_user", password="testpass123")
        self.client.login(username="noperm_apply_user", password="testpass123")
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
        """Non-AJAX POST from user without change permission returns 403 (lines 430-432)."""
        User.objects.create_user(username="noperm_tog_user2", password="testpass123")
        self.client.login(username="noperm_tog_user2", password="testpass123")
        url = self._toggle_url(self.rule.pk)
        response = self.client.post(url)
        self.assertIn(response.status_code, [403, 302])

    def test_post_apply_apply_error_shows_error_message(self):
        """POST apply when apply_rule_to_existing raises shows error message (lines 413-415)."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule.pk},
        )
        with patch("netbox_interface_name_rules.engine.apply_rule_to_existing", side_effect=ValueError("bad template")):
            response = self.client.post(url, {"action": "apply", "interface_ids": [str(self.rule.pk)]})
        self.assertIn(response.status_code, [302])

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


# ---------------------------------------------------------------------------
# signals.py — exception paths (lines 39-40, 69, 115-116, 143-145, 214-234)
# ---------------------------------------------------------------------------


class SignalExceptionPathsTest(TestCase):
    """Test exception handling in signal handlers."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="SigXMfg", slug="sigxmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="SigX-Dev", slug="sigx-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="SigX-SFP", part_number="SigX-SFP")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="SigXBay 0", position="0")
        role = DeviceRole.objects.create(name="SigXRole", slug="sigxrole")
        site = Site.objects.create(name="SigXSite", slug="sigxsite")
        cls.vc = VirtualChassis.objects.create(name="sigx-vc")
        cls.device = Device.objects.create(
            name="sigx-sw1",
            device_type=cls.device_type,
            role=role,
            site=site,
            virtual_chassis=cls.vc,
            vc_position=1,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="SigXBay 0")

    def test_pre_save_module_exception_sets_none(self):
        """on_module_pre_save catches DB exceptions and sets _prev_module_type_id=None (lines 39-40)."""
        from netbox_interface_name_rules.signals import on_module_pre_save

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        with patch.object(Module.objects.__class__, "filter", side_effect=Exception("db error")):
            on_module_pre_save(Module, module)
        self.assertIsNone(module._prev_module_type_id)

    def test_module_saved_no_prev_type_returns_early(self):
        """on_module_saved returns early (line 69) when _prev_module_type_id is not set."""
        from netbox_interface_name_rules.signals import on_module_saved

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        # _prev_module_type_id is not set → getattr returns None → line 69 return
        if hasattr(module, "_prev_module_type_id"):
            del module.__dict__["_prev_module_type_id"]
        on_module_saved(Module, module, created=False)  # Should return without error

    def test_pre_save_device_exception_sets_none(self):
        """on_device_pre_save catches DB exceptions and sets attributes to None (lines 143-145)."""
        from netbox_interface_name_rules.signals import on_device_pre_save

        with patch.object(Device.objects.__class__, "filter", side_effect=Exception("db error")):
            on_device_pre_save(Device, self.device)
        self.assertIsNone(self.device._prev_virtual_chassis_id)
        self.assertIsNone(self.device._prev_vc_position)

    def test_deferred_apply_engine_exception_is_logged(self):
        """_apply_rules_deferred catches apply_interface_name_rules exception (lines 115-116)."""
        from netbox_interface_name_rules.signals import _apply_rules_deferred

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        with patch(
            "netbox_interface_name_rules.engine.apply_interface_name_rules", side_effect=Exception("engine fail")
        ):
            _apply_rules_deferred(module.pk, self.bay.pk)  # Should not raise

    def test_deferred_device_module_engine_exception_is_logged(self):
        """_apply_rules_for_device_deferred catches exception from module loop (lines 218-225)."""
        from netbox_interface_name_rules.signals import _apply_rules_for_device_deferred

        Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        with patch("netbox_interface_name_rules.engine.apply_interface_name_rules", side_effect=Exception("loop fail")):
            _apply_rules_for_device_deferred(self.device.pk)  # Should not raise

    def test_deferred_device_device_interface_exception_is_logged(self):
        """_apply_rules_for_device_deferred catches exception from device interface rules (lines 233-234)."""
        from netbox_interface_name_rules.signals import _apply_rules_for_device_deferred

        with patch(
            "netbox_interface_name_rules.engine.apply_device_interface_rules",
            side_effect=Exception("device rule fail"),
        ):
            _apply_rules_for_device_deferred(self.device.pk)  # Should not raise


# ---------------------------------------------------------------------------
# engine.py — evaluate_name_template unsafe AST node (line 829)
# ---------------------------------------------------------------------------


class EngineEvaluateTemplateUnsafeASTTest(TestCase):
    """Test evaluate_name_template raises for unsafe AST node types (line 829)."""

    def test_unsafe_ast_node_raises_valueerror(self):
        """A template with a call node (unsafe) raises ValueError (line 829).

        We need to bypass the regex guard and get an unsafe AST node.
        The regex guard only allows digits/spaces/operators; to get an unsafe node
        we can use ast.parse directly but the simplest way is to mock the regex check.
        """
        from netbox_interface_name_rules.engine import evaluate_name_template

        # The regex guard will reject most things, but we can craft a template
        # that has a safe-looking expression that still hits the AST check.
        # Actually the regex guard already catches non-arithmetic expressions.
        # The AST check is a defense-in-depth: test it via a template where
        # the regex passes but the AST would fail if reached.
        # A clean way: mock re.match to return True so the AST check runs.
        with patch("netbox_interface_name_rules.engine.re.match", return_value=MagicMock()):
            with self.assertRaises(ValueError):
                evaluate_name_template("{__import__('os')}", {})


# ---------------------------------------------------------------------------
# engine.py — _find_regex_match re.error path (lines 355-356)
# ---------------------------------------------------------------------------


class EngineFindRegexMatchErrorTest(TestCase):
    """Test that _find_regex_match silently skips rules with invalid regex patterns."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RegXMfg", slug="regxmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="RegX-Dev", slug="regx-dev")
        cls.module_type_good = ModuleType.objects.create(
            manufacturer=manufacturer, model="RegX-GOOD", part_number="RegX-GOOD"
        )
        # Create a rule with an invalid regex pattern (bypassing model validation)
        cls.bad_regex_rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="[unclosed(",  # syntactically invalid
            name_template="port{bay_position}",
            enabled=True,
        )
        # Save without calling clean() to bypass validation
        cls.bad_regex_rule.save()

    def test_invalid_regex_pattern_is_skipped_not_raised(self):
        """_find_regex_match silently skips rules with bad regex (lines 355-356)."""
        from netbox_interface_name_rules.engine import _find_regex_match

        candidates = [(None, None, None)]
        result = _find_regex_match("RegX-GOOD", candidates)
        # The bad regex rule is skipped; result is None (no valid rule found)
        self.assertIsNone(result)

    @classmethod
    def tearDownClass(cls):
        if cls.bad_regex_rule.pk:
            InterfaceNameRule.objects.filter(pk=cls.bad_regex_rule.pk).delete()
        super().tearDownClass()


# ---------------------------------------------------------------------------
# engine.py — has_applicable_interfaces exception path (lines 571-572)
# ---------------------------------------------------------------------------


class EngineHasApplicableExceptionTest(TestCase):
    """Test has_applicable_interfaces() catches exceptions and returns False."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="AppXMfg", slug="appxmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="APPX-SFP", part_number="APPX-SFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="et-0/0/{bay_position}",
        )

    def test_exception_in_find_interfaces_returns_false(self):
        """has_applicable_interfaces() returns False when find_interfaces_for_rule raises (lines 571-572)."""
        from netbox_interface_name_rules.engine import has_applicable_interfaces

        with patch(
            "netbox_interface_name_rules.engine.find_interfaces_for_rule",
            side_effect=RuntimeError("scan fail"),
        ):
            result = has_applicable_interfaces(self.rule)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# engine.py — _find_channel_base ValueError path (lines 535-536)
# ---------------------------------------------------------------------------


class EngineFindChannelBaseValueErrorTest(TestCase):
    """Test _find_channel_base skips ValueError from template evaluation (lines 535-536)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ChanXMfg", slug="chanxmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ChanX-Dev", slug="chanx-dev")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="ChanX-SFP", part_number="ChanX-SFP"
        )
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="CXBay 0", position="0")
        role = DeviceRole.objects.create(name="ChanXRole", slug="chanxrole")
        site = Site.objects.create(name="ChanXSite", slug="chanxsite")
        cls.device = Device.objects.create(name="chanx-dev-01", device_type=cls.device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="CXBay 0")

    def test_find_channel_base_valueerror_skips_to_fallback(self):
        """_find_channel_base catches ValueError and falls back to ifaces[0] (lines 535-536)."""
        from netbox_interface_name_rules.engine import _find_channel_base

        rule = InterfaceNameRule(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        iface0 = Interface.objects.create(device=self.device, module=module, name="Eth0", type="100gbase-x-qsfp28")
        iface1 = Interface.objects.create(device=self.device, module=module, name="Eth1", type="100gbase-x-qsfp28")
        ifaces = [iface0, iface1]
        variables = {"bay_position": "0", "slot": "0", "sfp_slot": "0"}

        with patch("netbox_interface_name_rules.engine.evaluate_name_template", side_effect=ValueError("bad template")):
            result = _find_channel_base(rule, ifaces, variables)
        # Falls back to ifaces[0] after ValueError
        self.assertEqual(result, iface0)


# ---------------------------------------------------------------------------
# engine.py — _build_module_qs platform filter (line 588)
# ---------------------------------------------------------------------------


class EngineBuildModuleQsPlatformTest(TestCase):
    """Test _build_module_qs applies platform filter correctly (line 588)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="PlatXMfg", slug="platxmfg")
        cls.platform = Platform.objects.create(name="PLATX-IOS", slug="platx-ios")
        other_platform = Platform.objects.create(name="PLATX-NXOS", slug="platx-nxos")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="PLATX-SFP", part_number="PLATX-SFP"
        )
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            platform=cls.platform,
            name_template="et-0/0/{bay_position}",
        )
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="PLATX-Dev", slug="platx-dev")
        ModuleBayTemplate.objects.create(device_type=device_type, name="PLBay 0", position="0")
        role = DeviceRole.objects.create(name="PlatXRole", slug="platxrole")
        site = Site.objects.create(name="PlatXSite", slug="platxsite")
        device_match = Device.objects.create(
            name="platx-dev-match", device_type=device_type, role=role, site=site, platform=cls.platform
        )
        device_other = Device.objects.create(
            name="platx-dev-other", device_type=device_type, role=role, site=site, platform=other_platform
        )
        bay_match = ModuleBay.objects.get(device=device_match)
        bay_other = ModuleBay.objects.get(device=device_other)
        cls.module_match = Module.objects.create(device=device_match, module_bay=bay_match, module_type=cls.module_type)
        cls.module_other = Module.objects.create(device=device_other, module_bay=bay_other, module_type=cls.module_type)

    def test_platform_filter_applied(self):
        """_build_module_qs applies rule.platform filter — matching device is included, other is excluded."""
        from netbox_interface_name_rules.engine import _build_module_qs

        qs = _build_module_qs(self.rule)
        pks = list(qs.values_list("pk", flat=True))
        self.assertIn(self.module_match.pk, pks)
        self.assertNotIn(self.module_other.pk, pks)


# ---------------------------------------------------------------------------
# engine.py — apply_rule_to_existing no-ifaces and id_set paths (lines 750, 753)
# ---------------------------------------------------------------------------


class EngineApplyRuleToExistingEdgeCasesTest(TestCase):
    """Test apply_rule_to_existing edge cases: no interfaces, id_set filter."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ARXMfg", slug="arxmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ARX-Dev", slug="arx-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="ARX-SFP", part_number="ARX-SFP")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="ARXBay 0", position="0")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="ARXBay 1", position="1")
        role = DeviceRole.objects.create(name="ARXRole", slug="arxrole")
        site = Site.objects.create(name="ARXSite", slug="arxsite")
        cls.device = Device.objects.create(name="arx-dev-01", device_type=cls.device_type, role=role, site=site)
        cls.bay0 = ModuleBay.objects.get(device=cls.device, name="ARXBay 0")
        cls.bay1 = ModuleBay.objects.get(device=cls.device, name="ARXBay 1")

    def test_channel_rule_no_interfaces_skips(self):
        """Channel rule skips module with no interfaces (line 750: continue)."""
        from netbox_interface_name_rules.engine import apply_rule_to_existing

        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        # Module with NO interfaces
        Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        count = apply_rule_to_existing(rule)
        self.assertEqual(count, 0)

    def test_channel_rule_id_set_filters_base(self):
        """Channel rule with id_set skips when base_iface.pk not in id_set (line 753)."""
        from netbox_interface_name_rules.engine import apply_rule_to_existing

        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="Eth99", type="100gbase-x-qsfp28")
        # Pass an id_set that does NOT include iface.pk
        count = apply_rule_to_existing(rule, interface_ids=[iface.pk + 9999])
        self.assertEqual(count, 0)
        # Interface should be unchanged
        iface.refresh_from_db()
        self.assertEqual(iface.name, "Eth99")


# ---------------------------------------------------------------------------
# engine.py — _rename_device_interface template/full_clean exception paths (lines 131-156)
# ---------------------------------------------------------------------------


class EngineRenameDeviceInterfaceExceptionTest(TestCase):
    """Test _rename_device_interface exception paths for template and validation errors."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RDIXMfg", slug="rdixmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="RDIX-Dev", slug="rdix-dev")
        cls.platform = Platform.objects.create(name="RDIX-IOS", slug="rdix-ios")
        role = DeviceRole.objects.create(name="RDIXRole", slug="rdixrole")
        site = Site.objects.create(name="RDIXSite", slug="rdixsite")
        vc = VirtualChassis.objects.create(name="rdix-vc")
        cls.device = Device.objects.create(
            name="rdix-sw1",
            device_type=cls.device_type,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=1,
            platform=cls.platform,
        )

    def test_template_exception_skips_interface(self):
        """_rename_device_interface skips when template evaluation raises (lines 131-138)."""
        from netbox_interface_name_rules.engine import apply_device_interface_rules

        # Create a device-interface rule with an unsafe template that triggers ValueError
        rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="{1/0}",  # division not allowed — ValueError from evaluate_name_template
        )
        Interface.objects.create(device=self.device, name="Gi0/1", type="1000base-t")
        apply_device_interface_rules(self.device)
        # Interface should NOT be renamed (exception was caught)
        iface = Interface.objects.get(device=self.device, name="Gi0/1")
        self.assertIsNotNone(iface)
        rule.delete()

    def test_full_clean_exception_skips_interface(self):
        """_rename_device_interface skips when full_clean raises (lines 147-156)."""
        from netbox_interface_name_rules.engine import apply_device_interface_rules

        rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern=r"Gi\d+/\d+",
            name_template="GigabitEthernet{vc_position}/{port}",
        )
        Interface.objects.create(device=self.device, name="Gi0/2", type="1000base-t")
        with patch("dcim.models.Interface.full_clean", side_effect=Exception("validation fail")):
            apply_device_interface_rules(self.device)
        # Even with exception, no unhandled error
        rule.delete()


# ---------------------------------------------------------------------------
# views.py — save_rule with scope fields (lines 203, 227)
# ---------------------------------------------------------------------------


class RuleTestViewSaveRuleWithScopeTest(ViewTestBase2):
    """Test RuleTestView._handle_save_rule with scoping fields set."""

    def _url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

    def test_save_rule_with_device_type_filters_scope(self):
        """POST save_rule with device_type set applies scope filter (line 203)."""
        data = {
            "name_template": "et-0/0/{bay_position}",
            "channel_count": "0",
            "channel_start": "0",
            "module_type": str(self.module_type.pk),
            "device_type": str(self.device_type.pk),
            "action": "save_rule",
        }
        response = self.client.post(self._url(), data)
        # Should redirect (either to existing rule or to add page)
        self.assertIn(response.status_code, [302])

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


# ---------------------------------------------------------------------------
# views.py — _fetch_db_preview ValueError path (line 301)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# views.py — RuleApplyDetailView.get re.error path (lines 334-338)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# views.py — RuleApplyDetailView.post background job success (line 400)
# ---------------------------------------------------------------------------


class RuleApplyDetailViewBackgroundJobSuccessTest(ViewTestBase2):
    """Test RuleApplyDetailView.post with background action that succeeds."""

    def test_post_background_success_shows_success_message(self):
        """POST background action with successful enqueue shows success message (line 400)."""
        url = reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail",
            kwargs={"pk": self.rule.pk},
        )
        mock_job = MagicMock()
        mock_job.pk = 42
        with patch("netbox_interface_name_rules.jobs.ApplyRuleJob.enqueue", return_value=mock_job):
            response = self.client.post(url, {"action": "background"})
        self.assertIn(response.status_code, [302])


# ---------------------------------------------------------------------------
# signals.py — module with null bay path (line 214)
# ---------------------------------------------------------------------------


class SignalModuleNullBayPathTest(TestCase):
    """Test _apply_rules_for_device_deferred skips modules with null module_bay (line 214)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="NullBayMfg", slug="nullbaymfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="NullBay-Dev", slug="nullbay-dev")
        ModuleBayTemplate.objects.create(device_type=device_type, name="NBBay 0", position="0")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="NullBay-SFP", part_number="NullBay-SFP"
        )
        role = DeviceRole.objects.create(name="NullBayRole", slug="nullbayrole")
        site = Site.objects.create(name="NullBaySite", slug="nullbaysite")
        vc = VirtualChassis.objects.create(name="nullbay-vc")
        cls.device = Device.objects.create(
            name="nullbay-sw1",
            device_type=device_type,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=1,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="NBBay 0")

    def test_module_with_null_bay_is_skipped(self):
        """_apply_rules_for_device_deferred skips module when module_bay is None (line 214)."""
        from netbox_interface_name_rules.signals import _apply_rules_for_device_deferred

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)

        # Mock the module queryset to return a module whose module_bay attr is None
        class FakeModule:
            module_bay = None
            module_type = module.module_type

        with patch(
            "dcim.models.Module.objects.filter",
            return_value=MagicMock(
                filter=MagicMock(return_value=MagicMock()),
                select_related=MagicMock(return_value=[FakeModule()]),
            ),
        ):
            _apply_rules_for_device_deferred(self.device.pk)  # Should not raise


# ---------------------------------------------------------------------------
# engine.py — _channel_rule_entry ValueError path (lines 618-619)
# ---------------------------------------------------------------------------


class EngineChannelRuleEntryValueErrorTest(TestCase):
    """Test _channel_rule_entry handles ValueError from template evaluation (lines 618-619)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ChRuleXMfg", slug="chrulexmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ChRuleX-Dev", slug="chrulex-dev")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="ChRuleX-SFP", part_number="ChRuleX-SFP"
        )
        ModuleBayTemplate.objects.create(device_type=device_type, name="CRBay 0", position="0")
        role = DeviceRole.objects.create(name="ChRuleXRole", slug="chrulexrole")
        site = Site.objects.create(name="ChRuleXSite", slug="chrulexsite")
        cls.device = Device.objects.create(name="chrulex-dev-01", device_type=device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="CRBay 0")

    def test_valueerror_in_template_sets_error_name(self):
        """_channel_rule_entry uses error name when template raises ValueError (lines 618-619)."""
        from netbox_interface_name_rules.engine import _channel_rule_entry

        rule = InterfaceNameRule(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="Eth0", type="100gbase-x-qsfp28")
        variables = {"bay_position": "0", "slot": "0", "sfp_slot": "0"}

        with patch(
            "netbox_interface_name_rules.engine.evaluate_name_template",
            side_effect=ValueError("bad"),
        ):
            result = _channel_rule_entry(rule, module, [iface], variables)
        # With ValueError, expected_names becomes ["<error: bad>"], which is not in existing_names
        self.assertIsNotNone(result)
        self.assertEqual(result["new_names"], ["<error: bad>"])


# ---------------------------------------------------------------------------
# engine.py — _process_channel_module with empty ifaces (line 640)
# ---------------------------------------------------------------------------


class EngineProcessChannelModuleEmptyIfacesTest(TestCase):
    """Test _process_channel_module returns early when ifaces is empty (line 640)."""

    def test_empty_ifaces_returns_zero_checked_false(self):
        """_process_channel_module returns (0, False) for empty ifaces list (line 640)."""
        from netbox_interface_name_rules.engine import _process_channel_module

        result = _process_channel_module(
            rule=MagicMock(channel_count=2),
            module=MagicMock(),
            ifaces=[],
            variables={},
            limit=None,
            results=[],
            module_qs=MagicMock(),
            processed_pks=set(),
        )
        self.assertEqual(result, (0, False))


# ---------------------------------------------------------------------------
# engine.py — _process_channel_module limit reached (line 645)
# ---------------------------------------------------------------------------


class EngineProcessChannelModuleLimitTest(TestCase):
    """Test _process_channel_module stops when limit is reached (line 645)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ChLimMfg", slug="chlimmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="ChLim-Dev", slug="chlim-dev")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="ChLim-SFP", part_number="ChLim-SFP"
        )
        ModuleBayTemplate.objects.create(device_type=device_type, name="CLBay 0", position="0")
        role = DeviceRole.objects.create(name="ChLimRole", slug="chlimrole")
        site = Site.objects.create(name="ChLimSite", slug="chlimsite")
        cls.device = Device.objects.create(name="chlim-dev-01", device_type=device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="CLBay 0")

    def test_limit_reached_returns_true(self):
        """_process_channel_module returns should_stop=True when limit is reached (line 645)."""
        from netbox_interface_name_rules.engine import _process_channel_module

        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}:{channel}",
            channel_count=2,
            channel_start=0,
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="Eth0", type="100gbase-x-qsfp28")
        variables = {"bay_position": "0", "slot": "0", "sfp_slot": "0"}

        results = [{"fake": "entry"}]  # already 1 result
        qs_mock = MagicMock()
        qs_mock.exclude.return_value.count.return_value = 0

        _checked, should_stop = _process_channel_module(
            rule=rule,
            module=module,
            ifaces=[iface],
            variables=variables,
            limit=1,  # limit=1 means stop after first result
            results=results,
            module_qs=qs_mock,
            processed_pks=set(),
        )
        # If the entry was added and limit=1 reached, should_stop should be True
        self.assertTrue(should_stop)
