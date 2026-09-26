# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Validate templates at the language, bulk-edit, and upgrade boundaries."""

from dcim.models import Manufacturer, ModuleType
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_interface_name_rules import name_template
from netbox_interface_name_rules.models import InterfaceNameRule


class ReferencedVariablesTest(SimpleTestCase):
    def test_reference_names_follow_first_appearance(self):
        for template, expected in (
            ("{channel.x}", ("channel",)),
            ("{channel[0]}", ("channel",)),
            ("{{channel} + 1}", ("channel",)),
            ("{channel!r}", ("channel",)),
            ("{channel:>2}", ("channel",)),
            ("{slot + channel * port + slot}", ("slot", "channel", "port")),
            ("{slot[port] + channel}", ("slot", "port", "channel")),
            ("{bay_position!r}{channel}{bay_position}", ("bay_position", "channel")),
            ("{channel_count}{'channel'}{obj.channel}", ("channel_count", "obj")),
            ("{channel", ()),
            ("}{channel}", ("channel",)),
            ("xe-{port +}", ("port",)),
            ("xe-{port)}", ("port",)),
            ("{slot foo}", ("slot", "foo")),
            ("{8 + port-}", ("port",)),
            ("{é é}", ("é",)),
            ("{é.1 + port}", ("é", "port")),
            ("{℘ ℘}", ("℘",)),
            ("{a·b a·b}", ("a·b",)),
            ("{1a b}", ("a", "b")),
            ("{·channel}", ("channel",)),
        ):
            with self.subTest(template=template):
                self.assertEqual(name_template.referenced_variables(template), expected)

    def test_a_group_that_does_not_parse_still_names_its_variables(self):
        for template in ("xe-{port +}", "xe-{port)}", "{slot foo}", "{é é}", "{℘ ℘}", "{·channel}"):
            with self.subTest(template=template), self.assertRaises(ValidationError):
                name_template.validate_rule(
                    breakout_mode="flat",
                    channel_count=0,
                    name_template=template,
                    parent_name_template="",
                    applies_to_device_interfaces=False,
                )

    def test_collects_both_fields_and_deduplicates_in_source_order(self):
        with self.assertRaises(ValidationError) as caught:
            name_template.validate_rule(
                breakout_mode="channelized",
                channel_count=4,
                name_template="{port}-{unknown}-{port}",
                parent_name_template="{channel}-{unknown}",
                applies_to_device_interfaces=False,
            )
        errors = caught.exception.message_dict
        self.assertEqual(len(errors["name_template"]), 2)
        self.assertTrue(errors["name_template"][0].startswith("{port} is not available"))
        self.assertTrue(errors["name_template"][1].startswith("{unknown} is not available"))
        self.assertIn("{channel}", errors["name_template"][0].split("Available: ")[1])
        self.assertEqual(
            errors["parent_name_template"][0], "The parent interface has no channel number; remove {channel}."
        )
        self.assertTrue(errors["parent_name_template"][1].startswith("{unknown} is not available"))

    def test_unbalanced_field_skips_membership_but_checks_other_field(self):
        with self.assertRaises(ValidationError) as caught:
            name_template.validate_rule(
                breakout_mode="channelized",
                channel_count=4,
                name_template="{port}}",
                parent_name_template="{unknown}",
                applies_to_device_interfaces=False,
            )
        self.assertEqual(len(caught.exception.message_dict["name_template"]), 1)
        self.assertTrue(caught.exception.message_dict["name_template"][0].startswith("Unbalanced braces"))
        self.assertIn("parent_name_template", caught.exception.message_dict)


class BraceGroupShapeTest(SimpleTestCase):
    """A save refuses a brace group that no variable values can evaluate."""

    SUFFIX = (
        " is neither a variable token nor integer arithmetic over variable tokens. "
        "Write each variable as its own {name} token, and use only +, -, *, // and parentheses, "
        "as in {{slot_num} // 2}."
    )

    def _errors(self, template, channel_count=0):
        try:
            name_template.validate_rule(
                breakout_mode="flat",
                channel_count=channel_count,
                name_template=template,
                parent_name_template="",
                applies_to_device_interfaces=False,
            )
        except ValidationError as exc:
            return exc.message_dict["name_template"]
        return []

    def test_the_refusal_quotes_the_operators_group(self):
        for template, group in (
            ("eth{slot_num // 2}", "{slot_num // 2}"),
            ("{slot_num + {sfp_slot}}", "{slot_num + {sfp_slot}}"),
            ("xe-{{slot_num} / 2}", "{{slot_num} / 2}"),
            ("{ channel }", "{ channel }"),
            ("{bay_position[0]}", "{bay_position[0]}"),
        ):
            with self.subTest(template=template):
                self.assertEqual(self._errors(template, channel_count=4), [group + self.SUFFIX])

    def test_a_format_field_keeps_the_format_field_message(self):
        self.assertEqual(
            self._errors("xe-{ bay_position :>2}"),
            [
                (
                    "Name templates take a variable or an arithmetic expression, not str.format "
                    "conversions and format specifications: { bay_position :>2}"
                )
            ],
        )

    def test_each_distinct_group_is_reported_once_after_the_context_errors(self):
        self.assertEqual(
            self._errors("{port}-{slot_num // 2}/{slot_num // 2}/{sfp_slot.x}")[1:],
            ["{slot_num // 2}" + self.SUFFIX, "{sfp_slot.x}" + self.SUFFIX],
        )

    def test_an_unavailable_variable_token_gets_only_the_context_error(self):
        errors = self._errors("{{unknown} // 2}")
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("{unknown} is not available"))

    def test_a_forbidden_operator_is_refused_before_any_division(self):
        for template in ("{1 // ({slot_num} - {sfp_slot}) + 2 ** 3}", "{{slot_num} ** 2}"):
            with self.subTest(template=template):
                self.assertEqual(self._errors(template), [template + self.SUFFIX])

    def test_a_well_shaped_group_saves_even_when_it_divides_by_zero(self):
        for template in ("port{8 // ({slot_num} - {sfp_slot})}", "{8 // 0}"):
            with self.subTest(template=template):
                self.assertEqual(self._errors(template), [], "the group is well-shaped, so its values decide")

    def test_a_token_beside_a_literal_digit_saves_when_some_values_evaluate(self):
        for template, slot_num, name in (("{0{slot_num}}", "0", "0"), ("{{slot_num}5}", "1", "15")):
            with self.subTest(template=template):
                self.assertEqual(self._errors(template), [])
                self.assertEqual(name_template.evaluate_name_template(template, {"slot_num": slot_num}), name)

    def test_two_glued_literals_that_need_different_digits_are_refused(self):
        template = "{0{slot_num} + {sfp_slot}5}"
        self.assertEqual(self._errors(template), [template + self.SUFFIX])


class TemplateBulkEditTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="Template Mfg", slug="template-mfg")
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model="Template Module")
        cls.module_rule = InterfaceNameRule.objects.create(module_type=module_type, name_template="{base}")
        cls.device_rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern="Gi.*",
            name_template="{base}",
        )
        cls.user = get_user_model().objects.create_superuser(username="template-editor", password=None)

    def setUp(self):
        self.client.force_login(self.user)

    def _post(self, **fields):
        return self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_bulk_edit"),
            {"_apply": "1", "pk": [self.module_rule.pk, self.device_rule.pk], **fields},
        )

    def test_mixed_context_refusal_names_rule_and_changes_neither(self):
        response = self._post(name_template="{bay_position}")
        self.assertContains(response, f"Rule {self.device_rule.pk} ({self.device_rule}):", html=False)
        self.assertContains(response, "{bay_position} is not available")
        for rule in (self.module_rule, self.device_rule):
            rule.refresh_from_db()
            self.assertEqual(rule.name_template, "{base}")

    def test_shared_variable_applies_to_both_rules(self):
        response = self._post(name_template="new-{base}")
        self.assertEqual(response.status_code, 302)
        for rule in (self.module_rule, self.device_rule):
            rule.refresh_from_db()
            self.assertEqual(rule.name_template, "new-{base}")

    def test_nullify_parent_takes_precedence_over_posted_template(self):
        self.module_rule.breakout_mode = "channelized"
        self.module_rule.channel_count = 4
        self.module_rule.parent_name_template = "parent-{base}"
        self.module_rule.save()
        response = self._post(parent_name_template="{unknown}", _nullify=["parent_name_template"])
        self.assertEqual(response.status_code, 302)
        self.module_rule.refresh_from_db()
        self.assertEqual(self.module_rule.parent_name_template, "")

    def test_update_fields_validates_main_template(self):
        self.module_rule.name_template = "{port}"
        with self.assertRaises(ValidationError):
            self.module_rule.save(update_fields=(field for field in ["name_template"]))
        self.module_rule.refresh_from_db()
        self.assertEqual(self.module_rule.name_template, "{base}")


class TemplateAuditMigrationTest(TransactionTestCase):
    def test_upgrade_reports_every_refused_field_without_changing_rows(self):
        app_label = "netbox_interface_name_rules"
        target = (app_label, "0017_audit_name_templates")
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes(app_label)
        executor.migrate([(app_label, "0016_restrict_breakout_mode_values")])
        state = executor.loader.project_state([(app_label, "0016_restrict_breakout_mode_values")])
        Rule = state.apps.get_model(app_label, "InterfaceNameRule")
        rows = [
            Rule(applies_to_device_interfaces=True, module_type_pattern=f"audit-{index}", name_template=template)
            for index, template in enumerate(("{bay_position}", "{unknown}", "{base"))
        ]
        rows.extend(
            (
                Rule(
                    module_type_is_regex=True,
                    module_type_pattern="audit-parent",
                    channel_count=4,
                    breakout_mode="channelized",
                    name_template="{port}",
                    parent_name_template="{unknown}",
                ),
                Rule(module_type_is_regex=True, module_type_pattern="audit-channel", name_template="{channel}"),
                Rule(module_type_is_regex=True, module_type_pattern="audit-valid", name_template="{vc_position}"),
                Rule(module_type_is_regex=True, module_type_pattern="audit-shape", name_template="eth{slot_num // 2}"),
            )
        )
        Rule.objects.bulk_create(rows)
        before = list(Rule.objects.order_by("pk").values())
        try:
            with self.assertLogs(f"{app_label}.migrations.0017_audit_name_templates", level="WARNING") as logs:
                MigrationExecutor(connection).migrate([target])
            for row, field, reason in (
                (rows[0], "name_template", "{bay_position}"),
                (rows[1], "name_template", "{unknown}"),
                (rows[2], "name_template", "Unbalanced braces"),
                (rows[3], "name_template", "{port}"),
                (rows[3], "parent_name_template", "{unknown}"),
                (rows[4], "name_template", "{channel}"),
                (rows[6], "name_template", "{slot_num // 2} is neither"),
            ):
                self.assertTrue(
                    any(f"ID {row.pk}" in line and field in line and reason in line for line in logs.output)
                )
            self.assertEqual(list(Rule.objects.order_by("pk").values()), before)
            self.assertIn(target, MigrationExecutor(connection).loader.applied_migrations)
            self.assertEqual(len(logs.output), 7)
        finally:
            Rule.objects.filter(pk__in=[row.pk for row in rows]).delete()
            MigrationExecutor(connection).migrate(latest)
