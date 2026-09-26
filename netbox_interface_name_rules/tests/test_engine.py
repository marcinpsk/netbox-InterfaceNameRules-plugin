# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the template evaluation engine (pure functions, no DB)."""

from unittest import TestCase
from unittest.mock import patch

from netbox_interface_name_rules.engine import evaluate_name_template


class EvaluateNameTemplateTest(TestCase):
    """Test evaluate_name_template with various template patterns."""

    def test_substituted_tokens_are_literal_in_either_variable_order(self):
        variables = {"base": "Eth{channel}", "channel": "2"}
        forward = evaluate_name_template("{base}:{channel}", variables)
        reverse = evaluate_name_template("{base}:{channel}", dict(reversed(variables.items())))
        self.assertEqual(forward, "Eth{channel}:2")
        self.assertEqual(reverse, forward)

    def test_substituted_braces_cannot_close_arithmetic_groups(self):
        for value in ("2}suffix", "2{", "\x00base\x00", "letters"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    evaluate_name_template("{1 + {base}}", {"base": value})
                self.assertEqual(str(caught.exception), f"Unsafe expression in name template: 1 + {value}")

    def test_simple_variable_substitution(self):
        result = evaluate_name_template(
            "et-0/0/{bay_position}",
            {"bay_position": "4", "bay_position_num": "4", "slot": "0"},
        )
        self.assertEqual(result, "et-0/0/4")

    def test_multiple_variables(self):
        result = evaluate_name_template(
            "HundredGigE{slot}/0/0/{bay_position}",
            {"slot": "0", "bay_position": "3", "bay_position_num": "3"},
        )
        self.assertEqual(result, "HundredGigE0/0/0/3")

    def test_bay_position_num(self):
        result = evaluate_name_template(
            "swp{bay_position_num}",
            {"bay_position": "swp5", "bay_position_num": "5", "slot": "0"},
        )
        self.assertEqual(result, "swp5")

    def test_channel_variable(self):
        result = evaluate_name_template(
            "xe-0/0/{bay_position}:{channel}",
            {"bay_position": "2", "bay_position_num": "2", "slot": "0", "channel": "3"},
        )
        self.assertEqual(result, "xe-0/0/2:3")

    def test_arithmetic_expression(self):
        with patch("builtins.eval", side_effect=AssertionError("template arithmetic called eval")):
            result = evaluate_name_template(
                "GigabitEthernet{slot}/{8 + 3}",
                {"slot": "2", "bay_position": "0", "bay_position_num": "0"},
            )
        self.assertEqual(result, "GigabitEthernet2/11")

    def test_arithmetic_with_variables(self):
        """Arithmetic with substituted variables — the converter offset pattern."""
        result = evaluate_name_template(
            "GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}",
            {
                "slot": "3",
                "parent_bay_position": "2",
                "sfp_slot": "1",
                "bay_position": "1",
                "bay_position_num": "1",
            },
        )
        # 8 + (2 - 1) * 2 + 1 = 8 + 2 + 1 = 11
        self.assertEqual(result, "GigabitEthernet3/11")

    def test_arithmetic_parentheses(self):
        result = evaluate_name_template(
            "port{(2 + 3) * 4}",
            {"bay_position": "0", "bay_position_num": "0", "slot": "0"},
        )
        self.assertEqual(result, "port20")

    def test_no_variables_static_string(self):
        result = evaluate_name_template("Management0", {})
        self.assertEqual(result, "Management0")

    def test_unmatched_and_empty_brace_groups_pass_through(self):
        """The extraction keeps the evaluator's existing treatment of unreadable groups."""
        templates = (
            "et-0/0/{bay_position",
            "et-0/0/bay}",
            "et-{}-0",
        )
        for template in templates:
            with self.subTest(template=template):
                self.assertEqual(evaluate_name_template(template, {}), template)

    def test_stray_closing_brace_does_not_hide_a_later_substitution(self):
        self.assertEqual(evaluate_name_template("a}b{bay_position}", {"bay_position": "2"}), "a}b2")

    def test_evaluation_substitutes_an_arbitrary_supplied_variable(self):
        """Evaluation reads its inputs, not the template-variable catalogue."""
        self.assertEqual(evaluate_name_template("port-{custom}", {"custom": "value"}), "port-value")

    def test_nested_arithmetic_still_runs_after_variable_substitution(self):
        self.assertEqual(
            evaluate_name_template(
                "GigabitEthernet{slot_num}/{8 + {sfp_slot}}",
                {"slot_num": "2", "sfp_slot": "3"},
            ),
            "GigabitEthernet2/11",
        )

    def test_unsupplied_variable_is_rejected_as_an_unsafe_expression(self):
        with self.assertRaises(ValueError) as raised:
            evaluate_name_template("et-0/0/{missing}", {})
        self.assertEqual(str(raised.exception), "Unsafe expression in name template: missing")

    def test_unsafe_expression_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_name_template(
                "{__import__('os').system('ls')}",
                {},
            )

    def test_a_format_spec_field_names_the_unsupported_construct(self):
        """The template language is substitution plus arithmetic, so `str.format` fields must say so."""
        message = (
            "Name templates take a variable or an arithmetic expression, not str.format "
            "conversions and format specifications: {bay_position!r}"
        )
        with self.assertRaises(ValueError) as raised:
            evaluate_name_template("{bay_position!r}", {"bay_position": "2"})
        self.assertEqual(str(raised.exception), message)

    def test_a_format_spec_field_on_a_non_ascii_name_names_the_unsupported_construct(self):
        for field in ("naïve!r", "é!r"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ValueError, rf"not str\.format conversions and format specifications: \{{{field}\}}"
                ):
                    evaluate_name_template(f"{{{field}}}", {})

    def test_division_expression(self):
        result = evaluate_name_template(
            "port{10 // 3}",
            {},
        )
        self.assertEqual(result, "port3")

    def test_floor_division_by_zero_rejected(self):
        """A zero divisor is a bad template, reported like every other one so callers can skip it."""
        with self.assertRaises(ValueError):
            evaluate_name_template("port{10 // 0}", {})

    def test_modulo_not_allowed(self):
        """Modulo operator is not in the allowed character set."""
        with self.assertRaises(ValueError):
            evaluate_name_template("port{10 % 3}", {})

    def test_true_division_not_allowed(self):
        """True division (single slash) is rejected; only floor division (//) is allowed."""
        with self.assertRaises(ValueError) as raised:
            evaluate_name_template("{1 / 2}", {})
        self.assertEqual(str(raised.exception), "Unsafe expression in name template: 1 / 2")

    def test_negative_number_in_expression(self):
        result = evaluate_name_template(
            "port{5 + -1}",
            {},
        )
        self.assertEqual(result, "port4")

    def test_bay_position_num_non_numeric_uses_zero(self):
        """When bay_position has no trailing digits, bay_position_num falls back to '0'."""
        from netbox_interface_name_rules.naming import _resolve_bay_position

        class FakeBay:
            position = "abc"
            name = "Bay abc"

        bay_position, bay_position_num = _resolve_bay_position(FakeBay())
        self.assertEqual(bay_position, "abc")
        self.assertEqual(bay_position_num, "0")
