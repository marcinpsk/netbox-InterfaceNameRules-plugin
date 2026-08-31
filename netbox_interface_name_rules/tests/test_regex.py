# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for regex pattern matching on module type rules."""

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
from django.test import TestCase

from netbox_interface_name_rules.engine import apply_interface_name_rules, find_matching_rule
from netbox_interface_name_rules.models import InterfaceNameRule

# Realistic module-model patterns an operator writes; none of these may be refused.
_SAFE_PATTERNS = (
    "QSFP-.*",
    "QSFP28-(100G|40G)",
    r"^ge-\d+/\d+/\d+$",
    r"[A-Z]+-\d+",
    "(abc)+",
    "(a)+?",
    r"Ethernet\d+",
    "^(10|25|100)G$",
    "(ab){2,4}",
    "^(a|aa)(a|aa)(a|aa)(a|aa)$",
    "^" + "(|)" * 4 + "x$",
    "^a+b+c+d+e+$",
    "^" + "((a|aa)x)" * 5 + "$",
    "^" + "(?:xa|xaa)" * 5 + "$",
    "^" + "[^a](a|aa)" * 5 + "$",
    "^" + "[^a](?:a|aa)" * 5 + "$",
    "^" + "(?i:[^a])(?-i:a|aa)" * 5 + "Z$",
    "^" + "(?i:[^a])(?:A|AA)" * 5 + "$",
    "^" + r"\d(?:a|aa)" * 5 + "$",
    "^" + r"\D(?:\d|\d\d)" * 5 + "$",
    "^" + r"\D(?a:\d|\d\d)" * 5 + "$",
    "^" + r"\W(?:\w|\w\w)" * 5 + "$",
    "^" + r"\S(?:\s|\s\s)" * 5 + "$",
    "^" + "a?x" * 5 + "$",
    "^" + "(?>x)(a|aa)" * 5 + "$",
    "^" + "(?:a[^a]|aa[^b])" * 2 + "a$",
    "^" + "[^a](|)" * 2 + "x$",
    "^(?>(a|aa)+)Z$",
    "^(?>(a|aa){4,})Z$",
    "^(?:a{0}|b)x$",
    "^(?:[ab]x|[bc]y)$",
    "^(?:[^ab]x|[^bc]y)$",
    "^(?:a|[^bc])x$",
    "^([a-z]x|ay)$",
    r"\d{1,3}(\.\d{1,3}){3}",
    "(?:QSFP|SFP)+",
    r"(?i)^(?-i:ab|AB)+$",
)

# Patterns whose evaluation backtracks exponentially; all must be refused.
_EXPONENTIAL_PATTERNS = (
    "^(a+)+$",
    "(a*)*",
    "^(a|a)+$",
    r"(\d+)+",
    "(a+)+?",
    "(?:a|aa)+",
    r"^(\w+\s?)*$",
    "((a+)+){2}",
    "(a{0,30}){0,30}",
    r"(?i)^(ab|AB)+$",
    r"^(?:(?i:ab)|AB)+$",
    "^" + "(a|aa)" * 40 + "$",
    "^" + "(|)" * 5 + "x$",
    "^" + ("(" + "(a|aa)" * 4 + ")") * 4 + "$",
    "^" + "(a|aa)()" * 40 + "$",
    "^" + "(?:(a|aa)){1}" * 40 + "$",
    "^" + "(?:a|aa)" * 40 + "$",
    "^" + "a(a|aa)" * 5 + "Z$",
    "^" + "(a|aa)a" * 5 + "Z$",
    "^" + "(?:(a|aa)){1,2}" * 5 + "Z$",
    "^" + "a?" * 5 + "a" * 5 + "$",
    "^" + "(?i:(ab|AB))" * 5 + "Z$",
    "^" + "(?i:((ab){1}|(AB)))" * 5 + "Z$",
    "^" + "(?i:(ab|AB)){1,2}" * 5 + "Z$",
    "^" + "(?-i:[^a])(?i:a|aa)" * 5 + "Z$",
    "^" + r"(?a:\D)(?:\d|\d\d)" * 5 + "$",
    "^(?>(a|aa){40})Z$",
    "^(?>(a|aa){40,})Z$",
    "^(?>(a|aa){40}Z)$",
    "^(?>((a|aa){40}Z))$",
    "^(?>((a|aa){40}))Z$",
    "^(?>(?i:((ab|AB)){40}))Z$",
    "^(?>" + "(?:a|aa)" * 5 + ")Z$",
    "^(?>" + "(a|aa)" * 5 + "$)",
    "^(?>" + "(a|aa)" * 5 + "Z)",
    "^(" + "(a|aa)" * 5 + ")Z$",
)


class RegexPatternSafetyTest(TestCase):
    """Refuse a module-type pattern whose evaluation can backtrack exponentially."""

    def _rule(self, pattern):
        return InterfaceNameRule(
            module_type_is_regex=True,
            module_type_pattern=pattern,
            name_template="port{bay_position}",
        )

    def test_exponential_patterns_are_refused(self):
        for pattern in _EXPONENTIAL_PATTERNS:
            with self.subTest(pattern=pattern), self.assertRaises(ValidationError) as ctx:
                self._rule(pattern).clean()
            self.assertIn("module_type_pattern", ctx.exception.message_dict)

    def test_realistic_patterns_are_accepted(self):
        for pattern in _SAFE_PATTERNS:
            with self.subTest(pattern=pattern):
                self._rule(pattern).clean()

    def test_device_level_rule_refuses_an_exponential_pattern(self):
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            module_type_pattern="^(a+)+$",
            name_template="port{bay_position}",
        )

        with self.assertRaises(ValidationError) as ctx:
            rule.clean()

        self.assertIn("module_type_pattern", ctx.exception.message_dict)

    def test_rule_tester_refuses_an_exponential_pattern(self):
        from netbox_interface_name_rules.forms import RuleTestForm

        form = RuleTestForm(
            {
                "name_template": "et-0/0/{bay_position}",
                "channel_count": "0",
                "channel_start": "0",
                "module_type_is_regex": True,
                "module_type_pattern": "^(a+)+$",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("module_type_pattern", form.errors)


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

    def test_unsafe_pattern_written_without_validation_is_not_evaluated(self):
        """Rule selection skips a legacy unsafe row and returns the next match."""
        fallback = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern=r"(?i)^abab$",
            name_template="fallback/{bay_position}",
        )
        unsafe = InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="unused",
            name_template="unsafe/{bay_position}",
        )
        InterfaceNameRule.objects.filter(pk=unsafe.pk).update(module_type_pattern=r"(?i)^(ab|AB)+$")
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
        """re.fullmatch requires the entire model name to match."""
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
