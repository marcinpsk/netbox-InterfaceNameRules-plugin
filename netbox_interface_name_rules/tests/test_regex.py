# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for regex pattern matching on module type rules."""

import importlib
import re

import re2
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
    Site,
)
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase

from netbox_interface_name_rules.engine import apply_interface_name_rules, find_matching_rule, has_applicable_interfaces
from netbox_interface_name_rules.models import InterfaceNameRule

_RE2_AUDIT = importlib.import_module("netbox_interface_name_rules.migrations.0014_validate_re2_patterns")

_RE2_DIFFERENTIAL_CORPUS = (
    "",
    "Gi0/1",
    "Ethernet_1",
    "ASCII",
    "123",
    "QSFP-100G",
    "qsfp-100g",
    "k",
    "K",
    "s",
    "S",
    "i",
    "I",
    " ",
    "\t",
    "_",
    "é",
    "ó",
    "İ",
    "ı",
    "ſ",
    "K",
    "٥",
    "٣",
    "³",
    "\xa0",
    "\u2028",
    "\u3000",
    "Gi٥/٣",
    "Gié",
    "éGi",
    "Móduló",
    "a_٥",
    "é ٣",
    "K_1",
)

_RE2_DIFFERENTIAL_PATTERNS = {
    r"\d": (r"\d", r"\d+", r"Gi\d+/\d+", r"[\d]+", r"[^\d]+", r"[^^]\d"),
    r"\D": (r"\D", r"\D+", r"Gi\D+", r"[\D]+"),
    r"\s": (r"\s", r"\s+", r"Gi\s+", r"[\s]+", r"[^\s]+"),
    r"\S": (r"\S", r"\S+", r"Gi\S+", r"[\S]+"),
    r"\w": (r"\w", r"\w+", r"Gi\w+", r"[\w]+", r"[^\w]+"),
    r"\W": (r"\W", r"\W+", r"Gi\W+", r"[\W]+"),
    r"[^\D]": (r"[^\D]", r"[^\D]+"),
    r"[^\S]": (r"[^\S]", r"[^\S]+"),
    r"[^\W]": (r"[^\W]", r"[^\W]+"),
    r"\b": (r"\b.*", r"Gi\b.*", r".*\bGi", r"\bMóduló\b"),
    r"\B": (r"\B.*", r"Gi\B.*", r".*\BGi"),
    "(?i)": (
        r"(?i)k",
        r"(?i)K",
        r"(?i)s",
        r"(?i)ſ",
        r"(?i)i",
        r"(?i)^qsfp-.*$",
        r"(?i)İ",
        r"(?i)ı",
        r"(?i)[^i]",
        r"(?i:[^i])",
        r"[^i](?i:a)",
    ),
    "neutral": (r"QSFP-.*", r"a{1,3}"),
}

_CASE_FOLDING_MATCHES = (
    ("k", "K", True, True),
    ("K", "k", True, True),
    ("s", "ſ", True, True),
    ("ſ", "s", True, True),
    ("i", "İ", True, False),
    ("İ", "i", True, False),
    ("i", "ı", True, False),
    ("ı", "i", True, False),
)

_SUPPORTED_PATTERNS = (
    "QSFP-.*",
    "QSFP28-(100G|40G)",
    r"^ge-\d+/\d+/\d+$",
    r"[A-Z]+-\d+",
    r"Ethernet\d+",
    "^(10|25|100)G$",
    "(?:QSFP|SFP)+",
    r"(?i)^qsfp-.*$",
    "^(a+)+$",
    "(?:a|aa)+",
    "^" + "(a|aa)" * 40 + "$",
)

_PYTHON_ONLY_PATTERNS = (
    r"(?=QSFP)QSFP",
    r"(?<=QSFP-)100G",
    r"(QSFP)\1",
    r"(?>QSFP)",
    r"(?a:QSFP)",
    r"QSFP\Z",
)


class RegexPatternEngineTest(TestCase):
    """Compile and execute stored patterns with RE2's bounded engine."""

    def _rule(self, pattern):
        return InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern=pattern,
            name_template="port{bay_position}",
        )

    def test_backtracking_shape_is_accepted_and_matches_through_rule_selection(self):
        rule = self._rule("^(a+)+$")
        rule.clean()
        rule.save()
        manufacturer = Manufacturer.objects.create(name="Linear Regex", slug="linear-regex")
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model="aaaa", part_number="LINEAR")

        self.assertEqual(find_matching_rule(module_type, None, None), rule)

    def test_supported_patterns_are_accepted(self):
        for pattern in _SUPPORTED_PATTERNS:
            with self.subTest(pattern=pattern):
                self._rule(pattern).clean()

    def test_python_only_patterns_are_refused(self):
        for pattern in _PYTHON_ONLY_PATTERNS:
            with self.subTest(pattern=pattern), self.assertRaises(ValidationError) as ctx:
                self._rule(pattern).clean()
            self.assertIn("module_type_pattern", ctx.exception.message_dict)
            self.assertIn("Invalid RE2 pattern", ctx.exception.messages[0])

    def test_device_level_rule_refuses_python_only_syntax(self):
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            module_type_pattern=r"(?=Gi)Gi\d+/\d+",
            name_template="port{bay_position}",
        )

        with self.assertRaises(ValidationError) as ctx:
            rule.clean()

        self.assertIn("module_type_pattern", ctx.exception.message_dict)

    def test_rule_tester_refuses_python_only_syntax(self):
        from netbox_interface_name_rules.forms import RuleTestForm

        form = RuleTestForm(
            {
                "name_template": "et-0/0/{bay_position}",
                "channel_count": "0",
                "channel_start": "0",
                "module_type_is_regex": True,
                "module_type_pattern": r"(?=QSFP)QSFP-.*",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("module_type_pattern", form.errors)
        self.assertIn("Invalid RE2 pattern", form.errors["module_type_pattern"][0])


class RegexModelValidationTest(TestCase):
    """Test model clean() validation for regex fields."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RxValMfg", slug="rxvalmfg")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="RX-SFP", part_number="RX-SFP")

    def test_regex_requires_pattern(self):
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="",
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type_pattern", ctx.exception.message_dict)

    def test_regex_rejects_fk_with_pattern(self):
        rule = InterfaceNameRule(
            module_type=self.module_type,
            module_type_is_regex=True,
            module_type_pattern="QSFP-.*",
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type", ctx.exception.message_dict)

    def test_regex_validates_pattern_syntax(self):
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="[invalid(",
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type_pattern", ctx.exception.message_dict)

    def test_exact_requires_module_type_fk(self):
        rule = InterfaceNameRule(
            module_type_is_regex=False,
            name_template="port{bay_position}",
        )
        with self.assertRaises(ValidationError) as ctx:
            rule.clean()
        self.assertIn("module_type", ctx.exception.message_dict)

    def test_valid_regex_rule_passes_clean(self):
        rule = InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            name_template="port{bay_position}",
        )
        rule.clean()  # Should not raise

    def test_valid_exact_rule_passes_clean(self):
        rule = InterfaceNameRule(
            module_type=self.module_type,
            module_type_is_regex=False,
            name_template="port{bay_position}",
        )
        rule.clean()  # Should not raise


class RegexFindMatchingRuleTest(TestCase):
    """Test two-tier matching: exact FK first, then regex pattern."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RxMfg", slug="rxmfg")
        cls.sfp_lr4 = ModuleType.objects.create(manufacturer=manufacturer, model="QSFP-DD-400G-LR4", part_number="LR4")
        cls.sfp_lr8 = ModuleType.objects.create(manufacturer=manufacturer, model="QSFP-DD-400G-LR8", part_number="LR8")
        cls.sfp_zr = ModuleType.objects.create(manufacturer=manufacturer, model="QSFP-DD-400G-ZR", part_number="ZR")
        cls.sfp_10g = ModuleType.objects.create(manufacturer=manufacturer, model="SFP-10G-LR", part_number="10GLR")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="8201-SYS", slug="8201-sys")
        cls.parent_mt = ModuleType.objects.create(manufacturer=manufacturer, model="RX-PARENT", part_number="RX-PARENT")

    def test_regex_matches_multiple_module_types(self):
        """A single regex rule matches several module types."""
        rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            name_template="FourHundredGigE0/0/0/{bay_position}",
        )
        self.assertEqual(find_matching_rule(self.sfp_lr4, None, None), rule)
        self.assertEqual(find_matching_rule(self.sfp_lr8, None, None), rule)
        self.assertEqual(find_matching_rule(self.sfp_zr, None, None), rule)

    def test_regex_does_not_match_unrelated(self):
        """Regex only matches its pattern, not other module types."""
        InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            name_template="FourHundredGigE0/0/0/{bay_position}",
        )
        self.assertIsNone(find_matching_rule(self.sfp_10g, None, None))

    def test_python_only_pattern_written_without_validation_is_not_evaluated(self):
        """Rule selection skips a legacy RE2-incompatible row and returns the next match."""
        fallback = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="abab",
            name_template="fallback/{bay_position}",
        )
        incompatible = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="unused",
            name_template="incompatible/{bay_position}",
        )
        InterfaceNameRule.objects.filter(pk=incompatible.pk).update(module_type_pattern=r"(?=abab)abab")
        module_type = ModuleType.objects.create(
            manufacturer=self.sfp_lr4.manufacturer,
            model="abab",
            part_number="CASE-OVERLAP",
        )

        self.assertEqual(find_matching_rule(module_type, None, None), fallback)

    def test_exact_takes_priority_over_regex(self):
        """Exact FK match is preferred over regex pattern match."""
        InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            name_template="generic400G/{bay_position}",
        )
        exact_rule = InterfaceNameRule.objects.create(
            module_type=self.sfp_lr4,
            name_template="exact-lr4/{bay_position}",
        )
        result = find_matching_rule(self.sfp_lr4, None, None)
        self.assertEqual(result, exact_rule)

    def test_regex_with_device_type_specificity(self):
        """Regex rule with device_type is preferred over generic regex."""
        generic = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            name_template="generic/{bay_position}",
        )
        specific = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            device_type=self.device_type,
            name_template="specific/{bay_position}",
        )
        # With device_type context → specific rule
        result = find_matching_rule(self.sfp_lr4, None, self.device_type)
        self.assertEqual(result, specific)
        # Without device_type context → generic rule
        result = find_matching_rule(self.sfp_lr4, None, None)
        self.assertEqual(result, generic)

    def test_regex_fullmatch_not_partial(self):
        """A regex rule requires the entire model name to match."""
        InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="400G",  # Partial — should NOT match
            name_template="partial/{bay_position}",
        )
        self.assertIsNone(find_matching_rule(self.sfp_lr4, None, None))

    def test_regex_with_parent_module_type(self):
        """Regex rule with parent_module_type specificity."""
        rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            parent_module_type=self.parent_mt,
            name_template="nested/{bay_position}",
        )
        result = find_matching_rule(self.sfp_lr8, self.parent_mt, None)
        self.assertEqual(result, rule)
        # Without parent context → no match
        self.assertIsNone(find_matching_rule(self.sfp_lr8, None, None))

    def test_disabled_regex_rule_not_returned(self):
        """Disabled regex rule is not returned even if its pattern matches."""
        InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            name_template="disabled/{bay_position}",
            enabled=False,
        )
        self.assertIsNone(find_matching_rule(self.sfp_lr4, None, None))


class RegexApplyRulesTest(TestCase):
    """Test regex rules through the full apply pipeline."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="RxApplyMfg", slug="rxapplymfg")
        cls.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="RX-APPLY-DEV", slug="rx-apply-dev"
        )
        cls.mt_lr4 = ModuleType.objects.create(manufacturer=manufacturer, model="QSFP-DD-400G-LR4", part_number="APLR4")
        cls.mt_zr = ModuleType.objects.create(manufacturer=manufacturer, model="QSFP-DD-400G-ZR", part_number="APZR")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 0", position="0")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Transceiver 1", position="1")
        role = DeviceRole.objects.create(name="RxApplyRole", slug="rxapplyrole")
        site = Site.objects.create(name="RxApplySite", slug="rxapplysite")
        cls.device = Device.objects.create(name="rx-apply-01", device_type=cls.device_type, role=role, site=site)

    def test_regex_rule_renames_interface(self):
        """Regex rule matches and renames interface for LR4."""
        InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            name_template="FourHundredGigE0/0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.mt_lr4)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="400gbase-x-osfp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "FourHundredGigE0/0/0/0")

    def test_same_regex_rule_different_module_type(self):
        """Same regex rule works for ZR module type too."""
        InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP-DD-400G-.*",
            name_template="FourHundredGigE0/0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 1")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.mt_zr)
        iface = Interface.objects.create(device=self.device, module=module, name="1", type="400gbase-x-osfp")
        renamed = apply_interface_name_rules(module, bay)
        self.assertEqual(renamed, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "FourHundredGigE0/0/0/1")

    def test_python_only_pattern_written_without_validation_is_not_applicable(self):
        """The applicability scan must not execute a legacy pattern outside RE2 syntax."""
        rule = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern=r"(?=QSFP)QSFP-DD-400G-.*",
            name_template="FourHundredGigE0/0/0/{bay_position}",
        )
        bay = ModuleBay.objects.get(device=self.device, name="Transceiver 0")
        module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.mt_lr4)
        Interface.objects.create(device=self.device, module=module, name="0", type="400gbase-x-osfp")

        self.assertFalse(has_applicable_interfaces(rule))


class RegexEngineMigrationTest(TestCase):
    """Audit stored rule patterns before the plugin starts executing them with RE2."""

    APP = "netbox_interface_name_rules"
    BEFORE = "0013_interfacenamerule_breakout_mode_and_more"

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        return executor.migrate([(self.APP, target)])

    def _latest_migration(self):
        loader = MigrationExecutor(connection).loader
        return sorted(name for app_label, name in loader.graph.leaf_nodes(self.APP))[-1]

    def test_every_pattern_admitted_by_the_migration_is_one_way(self):
        """An admitted pattern must never add an RE2 full match."""
        checked_constructs = set()
        for construct, patterns in _RE2_DIFFERENTIAL_PATTERNS.items():
            for pattern in patterns:
                breaks, _narrows = _RE2_AUDIT._re2_differences(pattern)
                if breaks:
                    continue
                checked_constructs.add(construct)
                broadened = [
                    subject
                    for subject in _RE2_DIFFERENTIAL_CORPUS
                    if re2.fullmatch(pattern, subject) and not re.fullmatch(pattern, subject)
                ]
                with self.subTest(construct=construct, pattern=pattern):
                    self.assertEqual(broadened, [])
        self.assertTrue(checked_constructs)
        self.assertTrue({r"\d", r"\s", r"\w", "(?i)"}.issubset(checked_constructs))

    def test_character_class_backspace_has_shared_semantics(self):
        self.assertEqual(_RE2_AUDIT._re2_differences(r"[\b]"), (False, False))

    def test_second_leading_caret_is_character_class_content(self):
        self.assertEqual(_RE2_AUDIT._re2_differences(r"[^^]\d"), (False, True))

    def test_case_insensitive_literals_have_expected_unicode_folds(self):
        for pattern_character, subject, python_expected, re2_expected in _CASE_FOLDING_MATCHES:
            pattern = f"(?i){re.escape(pattern_character)}"
            python_matches = bool(re.fullmatch(pattern, subject))
            re2_matches = bool(re2.fullmatch(pattern, subject))
            with self.subTest(pattern=pattern, subject=subject):
                self.assertEqual((python_matches, re2_matches), (python_expected, re2_expected))

    def test_case_insensitive_pattern_without_negated_class_only_narrows(self):
        for pattern in (r"(?i)i", r"(?i)^qsfp-.*$"):
            with self.subTest(pattern=pattern):
                self.assertEqual(_RE2_AUDIT._re2_differences(pattern), (False, True))

    def test_case_insensitive_negated_class_can_add_a_match(self):
        for pattern in (r"(?i)[^i]", r"(?i:[^i])"):
            subject = "İ"
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.fullmatch(pattern, subject))
                self.assertIsNotNone(re2.fullmatch(pattern, subject))
                self.assertTrue(_RE2_AUDIT._uses_different_re2_semantics(pattern))

    def test_case_insensitive_flag_and_negated_class_are_conservative_across_order(self):
        self.assertTrue(_RE2_AUDIT._uses_different_re2_semantics(r"[^i](?i:a)"))

    def test_upgrade_refuses_a_stored_pattern_outside_re2_syntax(self):
        latest = self._latest_migration()
        old_state = self._migrate(self.BEFORE)
        Rule = old_state.apps.get_model(self.APP, "InterfaceNameRule")
        rule = Rule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern=r"(?=Gi)Gi\d+/\d+",
            name_template="Gi{vc_position}/{port}",
        )
        connection.check_constraints()

        try:
            with self.assertRaisesRegex(RuntimeError, rf"InterfaceNameRule ID: {rule.pk}\b"):
                self._migrate(latest)
        finally:
            Rule.objects.filter(pk=rule.pk).delete()
            self._migrate(latest)

    def test_upgrade_refuses_a_stored_pattern_with_different_re2_semantics(self):
        latest = self._latest_migration()
        old_state = self._migrate(self.BEFORE)
        Rule = old_state.apps.get_model(self.APP, "InterfaceNameRule")
        rules = [
            Rule.objects.create(
                applies_to_device_interfaces=True,
                module_type_pattern=pattern,
                name_template="Gi{vc_position}/{port}",
            )
            for pattern in (
                r"[[:alpha:]]+",
                r"[[:^alpha:]]+",
                r"a{,3}",
                r"a{01}",
                r"a{1,03}",
                r"\D+",
                r"\S+",
                r"\W+",
                r"\B.*",
                r"Gi\b.*",
                r".*\bGi",
                r"\bMóduló\b",
                r"(?i)[^i]",
                r"(?i:[^i])",
                r"[^\d]+",
                r"[^\s]+",
                r"[^\w]+",
            )
        ]
        connection.check_constraints()

        try:
            with self.assertRaises(RuntimeError) as ctx:
                self._migrate(latest)
            for rule in rules:
                self.assertRegex(str(ctx.exception), rf"\b{rule.pk}\b")
        finally:
            Rule.objects.filter(pk__in=[rule.pk for rule in rules]).delete()
            self._migrate(latest)

    def test_upgrade_accepts_a_pattern_that_only_drops_matches(self):
        """These patterns can skip a rename but never redirect one."""
        latest = self._latest_migration()
        old_state = self._migrate(self.BEFORE)
        Rule = old_state.apps.get_model(self.APP, "InterfaceNameRule")
        rules = [
            Rule.objects.create(
                applies_to_device_interfaces=True,
                module_type_pattern=pattern,
                name_template="Gi{vc_position}/{port}",
            )
            for pattern in (
                r"\w+",
                r"\d+",
                r"\s+",
                r"Gi\d+/\d+",
                r"[^\D]+",
                r"[^\S]+",
                r"[^\W]+",
                r"[^^]\d",
                r"(?i)i",
                r"(?i)^qsfp-.*$",
            )
        ]
        connection.check_constraints()

        try:
            with self.assertLogs("netbox_interface_name_rules", level="WARNING") as logs:
                self._migrate(latest)
            for rule in rules:
                self.assertRegex("\n".join(logs.output), rf"\b{rule.pk}\b")
        finally:
            Rule.objects.filter(pk__in=[rule.pk for rule in rules]).delete()
            self._migrate(latest)

    def test_upgrade_preserves_unambiguous_shared_syntax(self):
        latest = self._latest_migration()
        old_state = self._migrate(self.BEFORE)
        Rule = old_state.apps.get_model(self.APP, "InterfaceNameRule")
        patterns = (r"\\w+", r"a{1,3}")
        rules = [
            Rule.objects.create(
                applies_to_device_interfaces=True,
                module_type_pattern=pattern,
                name_template="Gi{vc_position}/{port}",
            )
            for pattern in patterns
        ]
        connection.check_constraints()

        try:
            new_state = self._migrate(latest)
            migrated_patterns = new_state.apps.get_model(self.APP, "InterfaceNameRule").objects.filter(
                pk__in=[rule.pk for rule in rules]
            )
            self.assertEqual(set(migrated_patterns.values_list("module_type_pattern", flat=True)), set(patterns))
        finally:
            Rule.objects.filter(pk__in=[rule.pk for rule in rules]).delete()
            self._migrate(latest)

    def test_upgrade_preserves_a_backtracking_pattern_that_re2_can_execute(self):
        latest = self._latest_migration()
        old_state = self._migrate(self.BEFORE)
        Rule = old_state.apps.get_model(self.APP, "InterfaceNameRule")
        rule = Rule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern="^(a+)+$",
            name_template="Gi{vc_position}/{port}",
        )
        connection.check_constraints()

        try:
            new_state = self._migrate(latest)
            migrated_rule = new_state.apps.get_model(self.APP, "InterfaceNameRule").objects.get(pk=rule.pk)
            self.assertEqual(migrated_rule.module_type_pattern, "^(a+)+$")
        finally:
            Rule.objects.filter(pk=rule.pk).delete()
            self._migrate(latest)
