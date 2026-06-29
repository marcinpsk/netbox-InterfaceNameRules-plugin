# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for jobs, model properties, and API serializer edge-cases."""

from unittest.mock import MagicMock, patch

from dcim.models import DeviceType, Manufacturer, ModuleType, Platform
from django.core.exceptions import ValidationError
from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule

# ---------------------------------------------------------------------------
# jobs.py
# ---------------------------------------------------------------------------


class ApplyRuleJobMetaTest(TestCase):
    """Test ApplyRuleJob class structure and meta attributes."""

    def test_job_meta_name(self):
        """ApplyRuleJob.Meta.name has the expected display name."""
        from netbox_interface_name_rules.jobs import ApplyRuleJob

        self.assertEqual(ApplyRuleJob.Meta.name, "Apply Interface Name Rule")

    def test_job_run_missing_rule_id(self):
        """ApplyRuleJob.run logs warning and returns without error when rule_id is missing."""
        from unittest.mock import MagicMock

        from netbox_interface_name_rules.jobs import ApplyRuleJob

        job = ApplyRuleJob.__new__(ApplyRuleJob)
        job.logger = MagicMock()
        job.run()  # No rule_id kwarg
        job.logger.warning.assert_called_once()

    def test_job_run_nonexistent_rule_id(self):
        """ApplyRuleJob.run logs warning when rule_id doesn't correspond to a rule."""
        from unittest.mock import MagicMock

        from netbox_interface_name_rules.jobs import ApplyRuleJob

        job = ApplyRuleJob.__new__(ApplyRuleJob)
        job.logger = MagicMock()
        job.run(rule_id=999999)
        job.logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# models.py — specificity_score and specificity_label
# ---------------------------------------------------------------------------


class ModelSpecificityTest(TestCase):
    """Test InterfaceNameRule.specificity_score and specificity_label."""

    @classmethod
    def setUpTestData(cls):
        """Create reusable FK objects for specificity tests."""
        manufacturer = Manufacturer.objects.create(name="SpecMfg", slug="specmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="SPEC-SFP", part_number="SPEC-SFP")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="SPEC-Dev", slug="spec-dev")
        cls.platform = Platform.objects.create(name="SPEC-IOS", slug="spec-ios")

    def test_exact_global_score(self):
        """Exact FK rule with no scoping: score=1000."""
        rule = InterfaceNameRule(
            module_type=self.module_type,
            name_template="port{bay_position}",
        )
        self.assertEqual(rule.specificity_score, 1000)

    def test_exact_with_device_type_score(self):
        """Exact FK rule with device_type: score=1002."""
        rule = InterfaceNameRule(
            module_type=self.module_type,
            device_type=self.device_type,
            name_template="port{bay_position}",
        )
        self.assertEqual(rule.specificity_score, 1002)

    def test_exact_with_platform_score(self):
        """Exact FK rule with platform only: score=1001."""
        rule = InterfaceNameRule(
            module_type=self.module_type,
            platform=self.platform,
            name_template="port{bay_position}",
        )
        self.assertEqual(rule.specificity_score, 1001)

    def test_exact_full_specificity_score(self):
        """Exact FK rule with all scoping fields: score=1007."""
        rule = InterfaceNameRule(
            module_type=self.module_type,
            device_type=self.device_type,
            platform=self.platform,
            name_template="port{bay_position}",
        )
        rule.parent_module_type_id = self.module_type.pk  # set FK id directly
        self.assertEqual(rule.specificity_score, 1007)

    def test_regex_global_score(self):
        """Regex rule with no scoping: score = 0 * 100 + len(pattern)."""
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="QSFP-.*",
            name_template="port{bay_position}",
        )
        self.assertEqual(rule.specificity_score, len("QSFP-.*"))

    def test_regex_with_device_scope_score(self):
        """Regex rule with device_type: score = 2 * 100 + len(pattern)."""
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="SFP-10G-.*",
            name_template="port{bay_position}",
        )
        rule.device_type_id = self.device_type.pk
        self.assertEqual(rule.specificity_score, 200 + len("SFP-10G-.*"))

    def test_specificity_label_exact_global(self):
        """specificity_label for exact global rule is 'exact / global'."""
        rule = InterfaceNameRule(
            module_type=self.module_type,
            name_template="port{bay_position}",
        )
        label = rule.specificity_label
        self.assertIn("exact", label)
        self.assertIn("global", label)

    def test_specificity_label_regex_with_device(self):
        """specificity_label for regex device-scoped rule includes 'regex' and 'device'."""
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="QSFP-.*",
            name_template="port{bay_position}",
        )
        rule.device_type_id = self.device_type.pk
        label = rule.specificity_label
        self.assertIn("regex", label)
        self.assertIn("device", label)

    def test_specificity_label_device_iface_with_pattern(self):
        """specificity_label for device-interface rule with pattern includes iface-filter."""
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            module_type_pattern="Gi.*",
            name_template="Gi{vc_position}/{port}",
        )
        label = rule.specificity_label
        self.assertIn("iface-filter", label)

    def test_specificity_label_device_iface_no_pattern(self):
        """specificity_label for device-interface rule without pattern shows wildcard."""
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            name_template="Gi{vc_position}/{port}",
        )
        label = rule.specificity_label
        self.assertIn("iface-filter(*)", label)


# ---------------------------------------------------------------------------
# models.py — __str__ edge cases
# ---------------------------------------------------------------------------


class ModelStrTest(TestCase):
    """Test __str__ representation of InterfaceNameRule."""

    @classmethod
    def setUpTestData(cls):
        """Create objects for __str__ tests."""
        manufacturer = Manufacturer.objects.create(name="StrMfg", slug="strmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="STR-SFP", part_number="STR-SFP")

    def test_str_exact_rule(self):
        """__str__ for exact rule shows module type model → template."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        s = str(rule)
        self.assertIn("STR-SFP", s)
        self.assertIn("et-0/0/{bay_position}", s)

    def test_str_regex_rule(self):
        """__str__ for regex rule wraps pattern in slashes."""
        rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-.*",
            name_template="port{bay_position}",
        )
        s = str(rule)
        self.assertIn("/QSFP-.*/", s)

    def test_str_device_iface_rule_no_module(self):
        """__str__ for device-interface rule uses '?' for module when module_type is None."""
        rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="Gi{vc_position}/{port}",
        )
        s = str(rule)
        self.assertIn("?", s)


# ---------------------------------------------------------------------------
# models.py — clean() edge cases for applies_to_device_interfaces
# ---------------------------------------------------------------------------


class ModelCleanDeviceIfaceTest(TestCase):
    """Test clean() validation specific to applies_to_device_interfaces=True rules."""

    @classmethod
    def setUpTestData(cls):
        """Create module type for FK validation tests."""
        manufacturer = Manufacturer.objects.create(name="CleanMfg", slug="cleanmfg")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="CLEAN-SFP", part_number="CLEAN-SFP"
        )

    def test_device_iface_rule_with_module_type_fails(self):
        """applies_to_device_interfaces=True + module_type set → ValidationError."""
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            module_type=self.module_type,
            name_template="Gi{vc_position}/{port}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type", ctx.exception.message_dict)

    def test_device_iface_rule_invalid_pattern_fails(self):
        """applies_to_device_interfaces=True + invalid regex pattern → ValidationError."""
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            module_type_pattern="[invalid(",
            name_template="Gi{vc_position}/{port}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type_pattern", ctx.exception.message_dict)

    def test_device_iface_rule_valid_passes(self):
        """applies_to_device_interfaces=True with valid optional pattern passes clean()."""
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            module_type_pattern=r"Gi\d+/\d+",
            name_template="Gi{vc_position}/{port}",
        )
        rule.clean()  # Should not raise

    def test_device_iface_rule_forces_regex_false(self):
        """clean() sets module_type_is_regex=False for device-interface rules."""
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            module_type_is_regex=True,  # Will be forced to False
            name_template="Gi{vc_position}/{port}",
        )
        rule.clean()
        self.assertFalse(rule.module_type_is_regex)

    def test_exact_rule_with_redos_pattern_fails(self):
        """Regex rule with ReDoS-prone pattern (nested quantifiers) fails validation."""
        # "(ab)+*" has ")+*" which triggers the nested-quantifier guard
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="(ab)+*",
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type_pattern", ctx.exception.message_dict)


class ModelCleanRegexModeTest(TestCase):
    """Test clean() validation for regex mode (module_type_is_regex=True)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RegexCleanMfg", slug="regexcleanmfg")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="RXCLN-SFP", part_number="RXCLN-SFP"
        )

    def test_regex_mode_without_pattern_fails(self):
        """module_type_is_regex=True but no pattern → ValidationError on module_type_pattern."""
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="",
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type_pattern", ctx.exception.message_dict)

    def test_regex_mode_with_module_type_fk_fails(self):
        """module_type_is_regex=True but module_type FK also set → ValidationError on module_type."""
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="QSFP-.*",
            module_type=self.module_type,
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type", ctx.exception.message_dict)

    def test_regex_mode_invalid_pattern_fails(self):
        """module_type_is_regex=True with syntactically invalid regex → ValidationError."""
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="[unclosed",
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type_pattern", ctx.exception.message_dict)

    def test_regex_mode_valid_pattern_passes(self):
        """module_type_is_regex=True with valid pattern and no FK → passes clean()."""
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-.*",
            name_template="port{bay_position}",
        )
        rule.clean()  # Should not raise


class ModelCleanExactModeTest(TestCase):
    """Test clean() validation for exact mode (module_type_is_regex=False)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="ExactCleanMfg", slug="exactcleanmfg")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="EXCLN-SFP", part_number="EXCLN-SFP"
        )

    def test_exact_mode_without_module_type_fails(self):
        """Exact mode with no module_type FK → ValidationError on module_type."""
        rule = InterfaceNameRule(
            module_type_is_regex=False,
            module_type=None,
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type", ctx.exception.message_dict)

    def test_exact_mode_clears_stale_pattern(self):
        """Exact mode with a leftover module_type_pattern → pattern is cleared by clean()."""
        rule = InterfaceNameRule(
            module_type=self.module_type,
            module_type_is_regex=False,
            module_type_pattern="stale-regex",
            name_template="port{bay_position}",
        )
        rule.clean()
        self.assertEqual(rule.module_type_pattern, "")

    def test_exact_mode_valid_passes(self):
        """Exact mode with module_type FK set → passes clean()."""
        rule = InterfaceNameRule(
            module_type=self.module_type,
            module_type_is_regex=False,
            name_template="port{bay_position}",
        )
        rule.clean()  # Should not raise


# ---------------------------------------------------------------------------
# api/serializers.py — validate() edge cases
# ---------------------------------------------------------------------------


class SerializerValidationTest(TestCase):
    """Test InterfaceNameRuleSerializer.validate() XOR constraints."""

    @classmethod
    def setUpTestData(cls):
        """Create fixtures for serializer validation tests."""
        manufacturer = Manufacturer.objects.create(name="SerMfg", slug="sermfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="SER-SFP", part_number="SER-SFP")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="SER-Dev", slug="ser-dev")

    def _get_serializer(self, data, instance=None):
        """Return an InterfaceNameRuleSerializer with the given data."""
        from netbox_interface_name_rules.api.serializers import InterfaceNameRuleSerializer

        return InterfaceNameRuleSerializer(instance=instance, data=data)

    def test_non_regex_with_pattern_fails(self):
        """Non-regex rule with module_type_pattern set → validation error."""
        s = self._get_serializer(
            {
                "module_type": self.module_type.pk,
                "module_type_is_regex": False,
                "module_type_pattern": "QSFP-.*",
                "name_template": "port{bay_position}",
                "channel_count": 0,
                "channel_start": 0,
            }
        )
        s.is_valid()
        self.assertIn("module_type_pattern", s.errors)

    def test_regex_without_module_type_valid(self):
        """Regex rule with no module_type FK and valid pattern passes serializer."""
        s = self._get_serializer(
            {
                "module_type": None,
                "module_type_is_regex": True,
                "module_type_pattern": "QSFP-100G-.*",
                "name_template": "Hu0/0/0/{bay_position}",
                "channel_count": 0,
                "channel_start": 0,
            }
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_regex_with_module_type_fails(self):
        """Regex rule with module_type FK set → validation error."""
        s = self._get_serializer(
            {
                "module_type": self.module_type.pk,
                "module_type_is_regex": True,
                "module_type_pattern": "QSFP-.*",
                "name_template": "port{bay_position}",
                "channel_count": 0,
                "channel_start": 0,
            }
        )
        s.is_valid()
        self.assertIn("module_type", s.errors)

    def test_exact_without_module_type_fails(self):
        """Non-regex rule without module_type FK → validation error."""
        s = self._get_serializer(
            {
                "module_type": None,
                "module_type_is_regex": False,
                "module_type_pattern": "",
                "name_template": "port{bay_position}",
                "channel_count": 0,
                "channel_start": 0,
            }
        )
        s.is_valid()
        self.assertIn("module_type", s.errors)


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
# Model csv_headers / to_csv() — regression: KeyError 'Ch' on CSV import
# ---------------------------------------------------------------------------


class ModelCSVExportTest(TestCase):
    """Test that InterfaceNameRule exposes csv_headers and to_csv() for round-trip CSV."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="CSVMfg", slug="csvmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="CSV-SFP", part_number="CSV-SFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="et-0/0/{bay_position}",
            channel_count=4,
            channel_start=0,
            description="CSV test rule",
        )
        cls.regex_rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-.*",
            name_template="{base}/{channel}",
            channel_count=4,
            channel_start=1,
            description="Regex CSV rule",
        )

    def test_csv_headers_attribute_exists(self):
        """InterfaceNameRule.csv_headers class attribute must exist."""
        self.assertTrue(hasattr(InterfaceNameRule, "csv_headers"), "InterfaceNameRule.csv_headers missing")

    def test_csv_headers_is_list_or_tuple(self):
        """csv_headers must be a list or tuple of strings."""
        self.assertIsInstance(InterfaceNameRule.csv_headers, (list, tuple))

    def test_csv_headers_matches_import_form_fields(self):
        """csv_headers must exactly match InterfaceNameRuleImportForm.Meta.fields."""
        from netbox_interface_name_rules.forms import InterfaceNameRuleImportForm

        form_fields = list(InterfaceNameRuleImportForm.Meta.fields)
        self.assertEqual(list(InterfaceNameRule.csv_headers), form_fields)

    def test_to_csv_method_exists(self):
        """InterfaceNameRule instances must have a to_csv() method."""
        self.assertTrue(callable(getattr(self.rule, "to_csv", None)), "InterfaceNameRule.to_csv() missing")

    def test_to_csv_returns_sequence(self):
        """to_csv() must return a tuple or list."""
        result = self.rule.to_csv()
        self.assertIsInstance(result, (tuple, list))

    def test_to_csv_length_matches_csv_headers(self):
        """to_csv() must return exactly len(csv_headers) values."""
        result = self.rule.to_csv()
        self.assertEqual(len(result), len(InterfaceNameRule.csv_headers))

    def test_to_csv_exact_rule_module_type(self):
        """to_csv() for exact rule must include the module_type model string."""
        result = self.rule.to_csv()
        values = list(result)
        idx = list(InterfaceNameRule.csv_headers).index("module_type")
        self.assertEqual(values[idx], "CSV-SFP")

    def test_to_csv_regex_rule_no_module_type(self):
        """to_csv() for regex rule must have empty string for module_type."""
        result = self.regex_rule.to_csv()
        values = list(result)
        idx = list(InterfaceNameRule.csv_headers).index("module_type")
        self.assertEqual(values[idx], "")

    def test_to_csv_no_dots_in_headers(self):
        """csv_headers must not contain dots (regression guard for KeyError 'Ch' bug)."""
        for header in InterfaceNameRule.csv_headers:
            self.assertNotIn(".", header, f"csv_headers entry '{header}' contains a dot — would break import")


# ---------------------------------------------------------------------------
# __init__.py — PluginConfig version gate (must match the documented floor)
# ---------------------------------------------------------------------------


class PluginConfigVersionTest(TestCase):
    """The enforced min_version gate must match the compatibility floor stated in the docs."""

    def test_min_version_matches_documented_floor(self):
        """min_version is the gate NetBox uses to refuse loading the plugin, so it must equal the
        floor advertised in README.md / docs/installation.md (NetBox >= 4.3.0). A drift here would
        let an unsupported NetBox load the plugin (or reject a supported one) with no other guard —
        the inconsistency a reviewer flagged when the docs were raised to 4.3.0 but the gate stayed 4.2.0.
        """
        from netbox_interface_name_rules import InterfaceNameRulesConfig

        self.assertEqual(InterfaceNameRulesConfig.min_version, "4.3.0")
