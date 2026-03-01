# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for utils, jobs, model properties, and API serializer edge-cases."""

from dcim.models import DeviceType, Manufacturer, ModuleType, Platform
from django.core.exceptions import ValidationError
from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.utils import supports_module_path


# ---------------------------------------------------------------------------
# utils.py
# ---------------------------------------------------------------------------


class SupportModulePathTest(TestCase):
    """Test the supports_module_path feature-detection helper."""

    def test_returns_bool(self):
        """supports_module_path() returns a boolean (True or False)."""
        result = supports_module_path()
        self.assertIsInstance(result, bool)


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
