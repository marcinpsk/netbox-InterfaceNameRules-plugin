# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the per-rule breakout mode, on every supported NetBox release.

``breakout_mode`` names the topology a breakout rule produces: ``flat`` — today's N sibling
interfaces — or ``channelized`` — one parent carrying ``channels`` plus N channel subinterfaces.
The two topologies are different objects to the API, to cabling and to automation, so the mode is
never inferred: it is a rule field that travels through validation, the exports, the REST and
GraphQL APIs and the forms, and a channelized rule on a NetBox that cannot model channels is
skipped rather than quietly rebuilt as a flat family.

Everything here runs on every NetBox the plugin supports; behaviour that only exists where NetBox
models channelized interfaces lives in test_channelized_mode.py.
"""

import csv
import io
import json
import re
from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import skipIf, skipUnless
from unittest.mock import patch

import yaml
from dcim.models import Interface, InterfaceTemplate, ModuleType, VirtualChassis
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django.test import TestCase
from django.urls import reverse
from utilities.testing import APITestCase

from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    apply_rule_to_existing,
    find_interfaces_for_rule,
    find_matching_rule,
    predict_rule_output,
    supports_channelization,
)
from netbox_interface_name_rules.family import UNCLAIMED_BASE_REASON, FamilyStatus
from netbox_interface_name_rules.family.names import COLLISION_REASON, INTERFACE_NAME_CONSTRAINT
from netbox_interface_name_rules.family.structural import MODULE_CHANGED_REASON, STALE_REASON
from netbox_interface_name_rules.filters import InterfaceNameRuleFilterSet
from netbox_interface_name_rules.forms import RuleTestForm
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.name_template import referenced_variables
from netbox_interface_name_rules.rule_selection import _VERSION_COLUMNS
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_channelization import (
    PARENT_TYPE,
    PLUGIN_LOGGER,
    REQUIRES_CHANNELIZATION,
    ChannelizationTestCase,
    _build_device,
)
from netbox_interface_name_rules.views import RulePreview

FLAT = "flat"
CHANNELIZED = "channelized"

TEST_PASSWORD = "testpass123"  # noqa: S105 - Test credential only.

User = get_user_model()


def _plain_module_type(manufacturer, model, iface_type=PARENT_TYPE):
    """Create a ModuleType with a single plain (non-channelized) port template."""
    module_type = ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=model)
    InterfaceTemplate.objects.create(module_type=module_type, name="{module}", type=iface_type)
    return module_type


class BreakoutModeFieldTest(TestCase):
    """The model exposes the mode and the parent template with the defaults existing rules rely on."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkField")
        cls.module_type = _plain_module_type(manufacturer, "BrkField-QSFP")

    def test_a_new_rule_defaults_to_the_flat_topology(self):
        """Nothing changes for a rule that never mentions the mode — flat is what it always did."""
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        rule.refresh_from_db()

        self.assertEqual(rule.breakout_mode, FLAT)

    def test_parent_name_template_defaults_to_blank(self):
        """A blank parent template means the parent keeps the name NetBox gave it."""
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="et-0/0/{bay_position}")

        rule.refresh_from_db()

        self.assertEqual(rule.parent_name_template, "")

    def test_the_parent_template_is_as_long_as_the_name_template(self):
        """It is the same kind of expression, so a template that fits one must fit the other."""
        self.assertEqual(
            InterfaceNameRule._meta.get_field("parent_name_template").max_length,
            InterfaceNameRule._meta.get_field("name_template").max_length,
        )

    def test_the_mode_offers_exactly_the_two_topologies(self):
        """Values name the topology produced, so a future model change adds a value instead of relabelling."""
        choices = dict(InterfaceNameRule._meta.get_field("breakout_mode").choices)

        self.assertEqual(set(choices), {FLAT, CHANNELIZED})

    def test_an_unknown_mode_is_rejected(self):
        """'legacy'/'native' style values are not accepted — the field is a closed choice set."""
        rule = InterfaceNameRule(
            module_type=self.module_type, name_template="et-0/0/{bay_position}", breakout_mode="native"
        )

        with self.assertRaises(ValidationError) as ctx:
            rule.full_clean()

        self.assertIn("breakout_mode", ctx.exception.message_dict)

    def test_both_fields_are_cloned_with_a_rule(self):
        """Duplicating a rule must carry its topology, or the copy silently changes meaning."""
        self.assertIn("breakout_mode", InterfaceNameRule.clone_fields)
        self.assertIn("parent_name_template", InterfaceNameRule.clone_fields)


class BreakoutModeValidationTest(TestCase):
    """clean() keeps the mode, the channel count and the parent template mutually consistent."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkValid")
        cls.module_type = _plain_module_type(manufacturer, "BrkValid-QSFP")

    def _rule(self, **kwargs):
        """Return an unsaved module rule with *kwargs* applied over sane defaults."""
        fields = {
            "module_type": self.module_type,
            "name_template": "xe-0/0/{bay_position}:{channel}",
        }
        fields.update(kwargs)
        return InterfaceNameRule(**fields)

    def _assert_rejected(self, rule, *expected_fields):
        """Assert *rule* fails validation, blaming at least one of *expected_fields*."""
        with self.assertRaises(ValidationError) as ctx:
            rule.full_clean()
        blamed = set(ctx.exception.message_dict)
        self.assertTrue(blamed & set(expected_fields), f"expected one of {expected_fields}, got {sorted(blamed)}")

    def test_a_parent_template_needs_the_channelized_mode(self):
        """A flat family has no parent row, so a parent template there could never be applied."""
        rule = self._rule(breakout_mode=FLAT, channel_count=4, parent_name_template="et-0/0/{bay_position}")

        self._assert_rejected(rule, "parent_name_template", "breakout_mode")

    def test_the_channelized_mode_needs_a_channel_count(self):
        """Channelizing means 'create N channels'; N=0 describes no family at all."""
        rule = self._rule(breakout_mode=CHANNELIZED, channel_count=0)

        self._assert_rejected(rule, "channel_count", "breakout_mode")

    def test_a_parent_template_must_not_reference_the_channel(self):
        """The parent is the one interface in the family that has no channel number."""
        rule = self._rule(
            breakout_mode=CHANNELIZED, channel_count=4, parent_name_template="et-0/0/{bay_position}:{channel}"
        )

        self._assert_rejected(rule, "parent_name_template")

    def test_a_parent_template_must_not_reference_the_channel_in_any_spelling(self):
        """Same reference, different syntax — each one reaches the engine and fails there instead."""
        for template in (
            "et-0/0/{bay_position}:{channel!r}",
            "et-0/0/{bay_position}:{channel:>2}",
            "et-0/0/{bay_position}:{ channel }",
            "et-0/0/{{channel} + 1}",
            "et-0/0/{channel + 1}",
            "et-0/0/{ channel*2 }",
        ):
            with self.subTest(parent_name_template=template):
                rule = self._rule(breakout_mode=CHANNELIZED, channel_count=4, parent_name_template=template)

                self._assert_rejected(rule, "parent_name_template")

    def test_the_channel_guard_answers_only_the_channel_question(self):
        """A brace group the AST cannot read is not a channel reference, whatever else is wrong with it.

        The expression pass exists to catch ``{channel + 1}``; turning every group it fails to parse
        into a channel error would blame the wrong thing.
        """
        self.assertNotIn("channel", referenced_variables("et-0/0/{bay_position!r}"))
        self.assertIn("channel", referenced_variables("et-0/0/{channel!r}"))

    def test_channel_references_in_partial_and_nested_brace_groups(self):
        """The reference view keeps its last-open-brace behavior on unusual input."""
        for template, expected in (
            ("{{channel}", True),
            ("}{channel}", True),
            ("{}", False),
            ("{channel", False),
        ):
            with self.subTest(template=template):
                self.assertEqual("channel" in referenced_variables(template), expected)

    def test_a_parent_template_may_still_do_arithmetic_on_the_other_variables(self):
        """The rule is 'no channel', not 'no expressions' — arithmetic parent names must still save."""
        rule = self._rule(
            breakout_mode=CHANNELIZED,
            channel_count=4,
            parent_name_template="et-0/0/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}",
        )

        rule.full_clean()

    def test_a_malformed_parent_template_is_answered_with_validation_not_a_traceback(self):
        """Stray braces are user input; the operator must get an error page, not an interface named '{'."""
        for template in ("et-0/0/{channel", "et-0/0/}{", "et-0/0/{bay_position"):
            with self.subTest(parent_name_template=template):
                rule = self._rule(breakout_mode=CHANNELIZED, channel_count=4, parent_name_template=template)

                with self.assertRaises(ValidationError) as ctx:
                    rule.full_clean()

                self.assertIn("parent_name_template", ctx.exception.message_dict)

    def test_device_interface_rules_cannot_be_channelized(self):
        """The device-level path renames existing interfaces; it never creates a family."""
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            name_template="xe-{vc_position}/0/0:{channel}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
        )

        self._assert_rejected(rule, "breakout_mode", "applies_to_device_interfaces")

    def test_device_interface_rules_cannot_carry_a_parent_template(self):
        """Same reason: there is no family for the device path to name a parent in."""
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True,
            name_template="xe-{vc_position}/0/0",
            parent_name_template="et-{vc_position}/0/0",
        )

        self._assert_rejected(rule, "parent_name_template", "applies_to_device_interfaces")

    def test_a_channelized_rule_with_channels_and_a_parent_template_is_valid(self):
        """The combination Phase B exists for must pass validation untouched."""
        rule = self._rule(breakout_mode=CHANNELIZED, channel_count=4, parent_name_template="et-0/0/{bay_position}")

        rule.full_clean()

    def test_a_channelized_rule_without_a_parent_template_is_valid(self):
        """A blank parent template is the documented 'keep the current name' case."""
        rule = self._rule(breakout_mode=CHANNELIZED, channel_count=4)

        rule.full_clean()

    def test_a_plain_flat_rule_is_still_valid(self):
        """Every rule that validated before Phase B must still validate."""
        rule = self._rule(breakout_mode=FLAT, channel_count=0, name_template="et-0/0/{bay_position}")

        rule.full_clean()


class BreakoutModeExportTest(TestCase):
    """CSV and YAML export carry the topology, so an exported rule imports back as itself."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkExp")
        cls.module_type = _plain_module_type(manufacturer, "BrkExp-QSFP")
        cls.channelized = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    @staticmethod
    def _csv_value(rule, header):
        """Return the exported CSV value of *header* for *rule*."""
        return rule.to_csv()[list(InterfaceNameRule.csv_headers).index(header)]

    def test_csv_headers_include_both_fields(self):
        """A CSV export missing the mode would re-import every rule as flat."""
        self.assertIn("breakout_mode", InterfaceNameRule.csv_headers)
        self.assertIn("parent_name_template", InterfaceNameRule.csv_headers)

    def test_to_csv_reports_the_mode_and_the_parent_template(self):
        """Values land in their own columns, in csv_headers order."""
        self.assertEqual(self._csv_value(self.channelized, "breakout_mode"), CHANNELIZED)
        self.assertEqual(self._csv_value(self.channelized, "parent_name_template"), "et-0/0/{bay_position}")

    def test_yaml_export_names_the_mode(self):
        """YAML is the plugin's own export format; the topology must be part of it."""
        entry = yaml.safe_load(self.channelized.to_yaml())[0]

        self.assertEqual(entry["breakout_mode"], CHANNELIZED)
        self.assertEqual(entry["parent_name_template"], "et-0/0/{bay_position}")

    def test_yaml_export_of_a_flat_rule_still_names_the_mode(self):
        """flat is a real value, not an absence — an importer must not have to guess it."""
        flat = InterfaceNameRule.objects.create(
            module_type=_plain_module_type(self.module_type.manufacturer, "BrkExp-QSFP-FLAT"),
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
        )

        entry = yaml.safe_load(flat.to_yaml())[0]

        self.assertEqual(entry["breakout_mode"], FLAT)
        self.assertNotIn("parent_name_template", entry)  # blank optionals stay out of the export


class BreakoutModeImportTest(TestCase):
    """A CSV exported from a channelized rule imports back into the same rule."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="brkimport", password=TEST_PASSWORD, email="brkimport@example.com"
        )
        manufacturer, cls.device = _build_device("BrkImp")
        cls.module_type = _plain_module_type(manufacturer, "BrkImp-QSFP")
        cls.target_type = _plain_module_type(manufacturer, "BrkImp-QSFP-TARGET")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def setUp(self):
        """Log in before posting to the import view."""
        self.client.force_login(self.superuser)

    def test_csv_round_trip_preserves_the_topology(self):
        """Export → import must not silently downgrade a channelized rule to a flat one."""
        row = list(self.rule.to_csv())
        row[list(InterfaceNameRule.csv_headers).index("module_type")] = self.target_type.model
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(InterfaceNameRule.csv_headers)
        writer.writerow(row)

        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_bulk_import"),
            {"data": buf.getvalue(), "format": "csv", "csv_delimiter": "auto"},
        )

        self.assertEqual(response.status_code, 302, "the import did not succeed")
        imported = InterfaceNameRule.objects.get(module_type=self.target_type)
        self.assertEqual(imported.breakout_mode, CHANNELIZED)
        self.assertEqual(imported.parent_name_template, "et-0/0/{bay_position}")


class BreakoutModeBulkEditTest(TestCase):
    """Bulk editing one field must leave the topology of every selected rule alone.

    A bulk-edit form is submitted whole: every field the page renders is posted, whether or not the
    operator touched it.  A field that cannot express "no change" therefore rewrites the column on
    every selected rule.
    """

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="brkbulk", password=TEST_PASSWORD, email="brkbulk@example.com"
        )
        manufacturer, cls.device = _build_device("BrkBulk")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=_plain_module_type(manufacturer, "BrkBulk-QSFP"),
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
        )

    def setUp(self):
        """Log in before posting to the bulk-edit view."""
        self.client.force_login(self.superuser)

    @staticmethod
    def _url():
        """Return the bulk-edit URL."""
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_bulk_edit")

    @staticmethod
    def _posted_by_an_untouched_browser(html, field_name):
        """Return the value a browser submits for *field_name* when nobody touches that select."""
        select = re.search(rf'<select[^>]*\bname="{field_name}"[^>]*>(.*?)</select>', html, re.DOTALL)
        if select is None:
            return None
        options = re.findall(r'<option value="([^"]*)"([^>]*)>', select.group(1))
        for value, attributes in options:
            if "selected" in attributes:
                return value
        return options[0][0] if options else None

    @staticmethod
    def _form_errors(response):
        """Return the bulk-edit form's errors, or None when the view redirected instead of rendering."""
        return getattr(response.context.get("form"), "errors", None) if response.context else None

    def _open_the_form(self):
        """POST the selection to the bulk-edit view and return the page it renders."""
        return self.client.post(self._url(), {"pk": [self.rule.pk], "_edit": ""})

    def test_editing_only_the_description_keeps_the_topology(self):
        """The operator changed a note, not the shape of the interfaces the rule builds."""
        page = self._open_the_form()
        untouched = self._posted_by_an_untouched_browser(page.content.decode(), "breakout_mode")
        self.assertIsNotNone(untouched, "the bulk-edit page renders no breakout_mode select")

        response = self.client.post(
            self._url(),
            {"pk": [self.rule.pk], "_apply": "", "description": "Bulk edited", "breakout_mode": untouched},
        )

        self.assertEqual(response.status_code, 302, self._form_errors(response))
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.breakout_mode, CHANNELIZED)
        self.assertEqual(self.rule.description, "Bulk edited")

    def test_the_mode_can_still_be_changed_deliberately(self):
        """A "no change" option must not cost the operator the ability to switch topology."""
        response = self.client.post(
            self._url(),
            {
                "pk": [self.rule.pk],
                "_apply": "",
                "breakout_mode": FLAT,
                "_nullify": "parent_name_template",  # a flat rule has no parent to name
            },
        )

        self.assertEqual(response.status_code, 302, self._form_errors(response))
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.breakout_mode, FLAT)
        self.assertEqual(self.rule.parent_name_template, "")


class BreakoutModeDetailViewTest(TestCase):
    """The rule detail page shows the topology a rule produces."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="brkdetail", password=TEST_PASSWORD, email="brkdetail@example.com"
        )
        manufacturer, cls.device = _build_device("BrkDetail")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=_plain_module_type(manufacturer, "BrkDetail-QSFP"),
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-XYZZY/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
        )

    def setUp(self):
        """Log in before requesting the detail page."""
        self.client.force_login(self.superuser)

    def test_detail_page_renders_the_parent_template(self):
        """An operator cannot review a rule whose parent name is invisible on its page."""
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_detail", args=[self.rule.pk])

        response = self.client.get(url)

        self.assertContains(response, "et-XYZZY/0/")


class BreakoutModeAPITest(APITestCase):
    """The REST API reads and writes both fields, and enforces the model's rules."""

    model = InterfaceNameRule
    view_namespace = "plugins-api:netbox_interface_name_rules"
    user_permissions = (
        "netbox_interface_name_rules.view_interfacenamerule",
        "netbox_interface_name_rules.add_interfacenamerule",
        "netbox_interface_name_rules.change_interfacenamerule",
    )

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkApi")
        cls.module_type = _plain_module_type(manufacturer, "BrkApi-QSFP")
        cls.other_type = _plain_module_type(manufacturer, "BrkApi-QSFP-2")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def test_detail_response_exposes_both_fields(self):
        """An API consumer must be able to tell the two topologies apart."""
        response = self.client.get(self._get_detail_url(self.rule), **self.header)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["breakout_mode"], CHANNELIZED)
        self.assertEqual(response.data["parent_name_template"], "et-0/0/{bay_position}")

    def test_a_channelized_rule_can_be_created_over_the_api(self):
        """Automation that provisions rules must be able to ask for the channelized topology."""
        data = {
            "module_type": self.other_type.pk,
            "name_template": "xe-0/0/{bay_position}:{channel}",
            "parent_name_template": "et-0/0/{bay_position}",
            "breakout_mode": CHANNELIZED,
            "channel_count": 4,
            "channel_start": 0,
        }

        response = self.client.post(self._get_list_url(), data, format="json", **self.header)

        self.assertEqual(response.status_code, 201, response.data)
        created = InterfaceNameRule.objects.get(module_type=self.other_type)
        self.assertEqual(created.breakout_mode, CHANNELIZED)
        self.assertEqual(created.parent_name_template, "et-0/0/{bay_position}")

    def test_the_api_refuses_a_channelized_rule_without_channels(self):
        """The model's consistency rules are enforced on the API path too, not only in the forms."""
        data = {
            "module_type": self.other_type.pk,
            "name_template": "xe-0/0/{bay_position}",
            "breakout_mode": CHANNELIZED,
            "channel_count": 0,
        }

        response = self.client.post(self._get_list_url(), data, format="json", **self.header)

        self.assertEqual(response.status_code, 400, response.data)

    def test_the_mode_can_be_switched_over_the_api(self):
        """Switching topology is a deliberate edit; the API must accept it as one."""
        response = self.client.patch(
            self._get_detail_url(self.rule),
            {"breakout_mode": FLAT, "parent_name_template": ""},
            format="json",
            **self.header,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.breakout_mode, FLAT)


class BreakoutModeGraphQLTest(APITestCase):
    """The GraphQL filter has an explicit field for each new column."""

    model = InterfaceNameRule
    user_permissions = ("netbox_interface_name_rules.view_interfacenamerule",)

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkGql")
        cls.channelized = InterfaceNameRule.objects.create(
            module_type=_plain_module_type(manufacturer, "BrkGql-QSFP-CH"),
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
        )
        cls.flat = InterfaceNameRule.objects.create(
            module_type=_plain_module_type(manufacturer, "BrkGql-QSFP-FL"),
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
        )

    @staticmethod
    def _filter_class():
        """Return the plugin's GraphQL filter class, or None on a NetBox without a filter base."""
        from netbox_interface_name_rules.graphql.filters import InterfaceNameRuleFilter

        return InterfaceNameRuleFilter

    def test_filter_declares_both_fields(self):
        """``fields="__all__"`` covers the type, not the filter — each filter field is declared by hand."""
        filter_class = self._filter_class()
        if filter_class is None:
            self.skipTest("No GraphQL filter base on this NetBox version")

        declared = {field.name for field in filter_class.__strawberry_definition__.fields}

        self.assertIn("breakout_mode", declared)
        self.assertIn("parent_name_template", declared)

    def test_query_can_filter_by_the_mode(self):
        """The declaration is only useful if a query can actually select on it."""
        if self._filter_class() is None:
            self.skipTest("No GraphQL filter base on this NetBox version")
        query = """
        {
          interface_name_rule_list(filters: {breakout_mode: {exact: "channelized"}}) {
            id
            name_template
          }
        }
        """

        response = self.client.post(reverse("graphql"), data={"query": query}, format="json", **self.header)

        self.assertEqual(response.status_code, 200, response.content)
        payload = json.loads(response.content)
        self.assertNotIn("errors", payload, payload)
        returned = [str(entry["id"]) for entry in payload["data"]["interface_name_rule_list"]]
        self.assertEqual(returned, [str(self.channelized.pk)])


class BreakoutModeFilterSetTest(TestCase):
    """The list view's free-text search reaches the parent template, as it does the name template."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkFs")
        cls.match = InterfaceNameRule.objects.create(
            module_type=_plain_module_type(manufacturer, "BrkFs-QSFP-A"),
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-XYZZY/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
        )
        cls.other = InterfaceNameRule.objects.create(
            module_type=_plain_module_type(manufacturer, "BrkFs-QSFP-B"),
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
        )

    def test_search_matches_the_parent_template(self):
        """Searching for a name the plugin will produce must find the rule that produces it."""
        results = InterfaceNameRuleFilterSet({"q": "XYZZY"}, queryset=InterfaceNameRule.objects.all()).qs

        self.assertEqual(list(results), [self.match])


class BreakoutModeRuleTestFormTest(TestCase):
    """The interactive rule builder carries the new fields into the preview."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="brkform", password=TEST_PASSWORD, email="brkform@example.com"
        )
        manufacturer, cls.device = _build_device("BrkForm")
        cls.module_type = _plain_module_type(manufacturer, "BrkForm-QSFP")

    def setUp(self):
        """Log in before posting to the rule test view."""
        self.client.force_login(self.superuser)

    def test_form_cleans_the_new_fields(self):
        """Without them on the form the preview would silently describe a different topology."""
        form = RuleTestForm(
            data={
                "name_template": "xe-0/0/{bay_position}:{channel}",
                "parent_name_template": "et-0/0/{bay_position}",
                "breakout_mode": CHANNELIZED,
                "channel_count": "4",
                "channel_start": "0",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["breakout_mode"], CHANNELIZED)
        self.assertEqual(form.cleaned_data["parent_name_template"], "et-0/0/{bay_position}")

    def test_preview_stand_in_accepts_the_new_fields(self):
        """find_interfaces_for_rule reads the mode off the rule, so the stand-in must carry it."""
        preview = RulePreview(
            module_type_is_regex=False,
            module_type_pattern="",
            module_type=self.module_type,
            parent_module_type=None,
            device_type=None,
            platform=None,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

        self.assertEqual(preview.breakout_mode, CHANNELIZED)
        self.assertEqual(preview.parent_name_template, "et-0/0/{bay_position}")

    def test_the_tester_controls_render_with_netboxs_form_classes(self):
        """The tester is a plain Django form, so NetBox's widget templates are what style it.

        NetBox 4.x removed ``BootstrapMixin`` and styles every form — model or not — by overriding
        Django's widget templates (``FORM_RENDERER = TemplatesSetting``).  Pinning the rendered
        classes catches a control that ends up unstyled, whatever supplies them.
        """
        response = self.client.get(reverse("plugins:netbox_interface_name_rules:interfacenamerule_test"))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for field, css_class in (
            ("name_template", "form-control"),
            ("parent_name_template", "form-control"),
            ("channel_count", "form-control"),
            ("breakout_mode", "form-select"),
            ("module_type", "form-select"),
            ("module_type_is_regex", "form-check-input"),
        ):
            with self.subTest(field=field):
                tag = re.search(rf'<[^>]*\bid="id_{field}"[^>]*>', html)
                self.assertIsNotNone(tag, f"{field} is not rendered on the tester page")
                self.assertIn(css_class, tag.group(0))

    def test_the_rule_test_view_accepts_a_channelized_rule(self):
        """The form is reached through the view; a field the view drops is a field the preview ignores."""
        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_test"),
            {
                "name_template": "xe-0/0/{bay_position}:{channel}",
                "parent_name_template": "et-0/0/{bay_position}",
                "breakout_mode": CHANNELIZED,
                "channel_count": "4",
                "channel_start": "0",
                "module_type": str(self.module_type.pk),
            },
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.errors, {})
        self.assertIsNone(response.context["error"])
        # An unbound extra POST key would be dropped silently; the preview must actually receive it.
        self.assertEqual(form.cleaned_data["breakout_mode"], CHANNELIZED)
        self.assertEqual(form.cleaned_data["parent_name_template"], "et-0/0/{bay_position}")


class BreakoutModeManualPreviewTest(TestCase):
    """The manual (variable-only) preview shows every name the rule would produce.

    It is the only preview an operator gets before any hardware exists, so a channelized rule has to
    show its parent there — otherwise the page describes a flat breakout under a channelized rule.
    """

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="brkprev", password=TEST_PASSWORD, email="brkprev@example.com"
        )

    def setUp(self):
        """Log in before posting to the rule test view."""
        self.client.force_login(self.superuser)

    def _preview(self, **fields):
        """POST the tester form with *fields* over the breakout defaults; return the preview rows."""
        data = {
            "name_template": "xe-0/0/{bay_position}:{channel}",
            "breakout_mode": FLAT,
            "channel_count": "4",
            "channel_start": "0",
            "var_bay_position": "3",
        }
        data.update(fields)
        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_test"), data, follow=False
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors, {})
        self.assertIsNone(response.context["error"])
        self.response = response
        return response.context["preview_results"]

    def test_a_channelized_preview_leads_with_the_parent(self):
        """The parent is the row the rule creates first; the channels hang off it."""
        results = self._preview(breakout_mode=CHANNELIZED, parent_name_template="et-0/0/{bay_position}")

        self.assertEqual(
            [entry["result"] for entry in results],
            ["et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"],
        )
        self.assertEqual([entry["role"] for entry in results], ["parent", "channel", "channel", "channel", "channel"])
        self.assertContains(self.response, "et-0/0/3")

    def test_a_blank_parent_template_previews_the_ports_own_name(self):
        """Blank keeps the port's name, so the parent row shows the base it was given."""
        results = self._preview(breakout_mode=CHANNELIZED, var_base="Ethernet1")

        self.assertEqual(results[0]["result"], "Ethernet1")
        self.assertEqual(results[0]["role"], "parent")

    def test_a_flat_breakout_previews_only_its_channels(self):
        """A flat rule builds no parent, so nothing is added to what the page always showed."""
        results = self._preview()

        self.assertEqual(
            [entry["result"] for entry in results],
            ["xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"],
        )
        self.assertEqual({entry["role"] for entry in results}, {"channel"})

    def test_a_simple_rename_previews_one_interface(self):
        """A rule with no channels at all is a plain rename, and previews as one row."""
        results = self._preview(name_template="et-0/0/{bay_position}", channel_count="0")

        self.assertEqual([(entry["result"], entry["role"]) for entry in results], [("et-0/0/3", "interface")])


class RuleTestFormTopologyTest(TestCase):
    """The tester form refuses the combinations a save would refuse.

    The form's whole purpose is to answer "what will this rule do?" before it is saved, so a
    combination the model rejects has to fail here rather than after a preview that describes a rule
    the operator can never store.
    """

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="brktopo", password=TEST_PASSWORD, email="brktopo@example.com"
        )

    @staticmethod
    def _data(**overrides):
        """Return tester-form POST data with *overrides* applied over a valid breakout rule."""
        data = {
            "name_template": "xe-0/0/{bay_position}:{channel}",
            "breakout_mode": FLAT,
            "channel_count": "4",
            "channel_start": "0",
        }
        data.update(overrides)
        return data

    @classmethod
    def _form(cls, **overrides):
        """Return a bound tester form built from the same data the view would receive."""
        return RuleTestForm(data=cls._data(**overrides))

    def _post(self, **overrides):
        """Submit the tester form through the real view and return the rendered response."""
        self.client.force_login(self.superuser)
        return self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_test"), self._data(**overrides)
        )

    def _assert_rejected(self, form, field):
        """Assert *form* is invalid and blames *field*."""
        self.assertFalse(form.is_valid())
        self.assertIn(field, form.errors)

    def test_the_channelized_mode_needs_a_channel_count(self):
        """Channelizing means 'create N channels'; N=0 describes no family the preview could show."""
        self._assert_rejected(self._form(breakout_mode=CHANNELIZED, channel_count="0"), "channel_count")

    def test_a_parent_template_needs_the_channelized_mode(self):
        """A flat family has no parent row, so a parent name there is a name nothing ever takes."""
        self._assert_rejected(
            self._form(breakout_mode=FLAT, parent_name_template="et-0/0/{bay_position}"), "parent_name_template"
        )

    def test_a_parent_template_must_not_reference_the_channel(self):
        """The parent is the one interface in the family without a channel number."""
        self._assert_rejected(
            self._form(breakout_mode=CHANNELIZED, parent_name_template="et-0/0/{bay_position}:{channel}"),
            "parent_name_template",
        )

    def test_a_parent_template_must_not_reference_the_channel_in_any_spelling(self):
        """The tester has to refuse every spelling the model refuses, or it previews an unsavable rule."""
        for template in (
            "et-0/0/{bay_position}:{channel!r}",
            "et-0/0/{bay_position}:{channel:>2}",
            "et-0/0/{bay_position}:{ channel }",
            "et-0/0/{{channel} + 1}",
            "et-0/0/{channel + 1}",
            "et-0/0/{ channel*2 }",
        ):
            with self.subTest(parent_name_template=template):
                self._assert_rejected(
                    self._form(breakout_mode=CHANNELIZED, parent_name_template=template), "parent_name_template"
                )

    def test_a_malformed_parent_template_is_refused_by_the_tester_too(self):
        """A template the model refuses to store must not preview as if it were savable."""
        for template in ("et-0/0/{channel", "et-0/0/}{", "et-0/0/{bay_position"):
            with self.subTest(parent_name_template=template):
                self._assert_rejected(
                    self._form(breakout_mode=CHANNELIZED, parent_name_template=template), "parent_name_template"
                )

    def test_a_parent_template_may_still_do_arithmetic_on_the_other_variables(self):
        """Arithmetic parent names are a documented form and must still preview."""
        form = self._form(
            breakout_mode=CHANNELIZED, parent_name_template="et-0/0/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}"
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_the_channelized_combination_stays_valid(self):
        """The combination the feature exists for must still preview."""
        form = self._form(breakout_mode=CHANNELIZED, parent_name_template="et-0/0/{bay_position}")

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_channelized_rule_without_a_parent_template_stays_valid(self):
        """Blank is the documented 'keep the port's name' case."""
        form = self._form(breakout_mode=CHANNELIZED)

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_flat_breakout_stays_valid(self):
        """Every rule that previewed before the mode existed must still preview."""
        form = self._form()

        self.assertTrue(form.is_valid(), form.errors)

    def test_a_simple_rename_stays_valid(self):
        """No channels, no mode question — the plainest rule of all."""
        form = self._form(name_template="et-0/0/{bay_position}", channel_count="0", channel_start="0")

        self.assertTrue(form.is_valid(), form.errors)

    def test_the_tester_view_reports_the_rejection_instead_of_previewing(self):
        """A preview of a rule that cannot be saved is worse than no preview."""
        response = self._post(breakout_mode=CHANNELIZED, channel_count="0")

        self.assertEqual(response.status_code, 200)
        self.assertIn("channel_count", response.context["form"].errors)
        self.assertIsNone(response.context["preview_results"])

    def test_the_tester_page_says_why_a_channelized_rule_without_channels_is_refused(self):
        """An error the page keeps to itself leaves the operator with a blank result and no reason."""
        response = self._post(breakout_mode=CHANNELIZED, channel_count="0")

        self.assertContains(response, "A channelized rule must define at least one channel.")

    def test_the_tester_page_says_why_an_unknown_mode_is_refused(self):
        """Same for the mode field itself — a rejected choice has to be visible next to the select."""
        response = self._post(breakout_mode="native")

        self.assertContains(response, "is not one of the available choices")

    def test_the_tester_page_blames_the_parent_template_rather_than_reporting_a_template_error(self):
        """A channel reference the check missed reaches the engine and surfaces as a bare 'ValueError'."""
        response = self._post(breakout_mode=CHANNELIZED, parent_name_template="et-0/0/{bay_position}:{channel!r}")

        self.assertContains(response, "The parent interface has no channel number")
        self.assertIsNone(response.context["preview_results"])


class BreakoutModeFingerprintTest(TestCase):
    """Both columns change what a rule produces, so both must invalidate the rule cache."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkFp")
        cls.module_type = _plain_module_type(manufacturer, "BrkFp-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )

    def test_both_columns_are_fingerprinted(self):
        """They decide the names and the topology, so they belong in the enabled-rule fingerprint."""
        self.assertIn("breakout_mode", _VERSION_COLUMNS)
        self.assertIn("parent_name_template", _VERSION_COLUMNS)

    def test_a_mode_change_is_visible_to_the_next_lookup(self):
        """A bulk update bypasses signals — only the fingerprint can invalidate the cached rule."""
        self.assertEqual(find_matching_rule(self.module_type, None, None).breakout_mode, FLAT)

        InterfaceNameRule.objects.filter(pk=self.rule.pk).update(breakout_mode=CHANNELIZED)

        self.assertEqual(find_matching_rule(self.module_type, None, None).breakout_mode, CHANNELIZED)

    def test_a_parent_template_change_is_visible_to_the_next_lookup(self):
        """Same reason: the cached rule would keep naming the parent the old way."""
        self.assertEqual(find_matching_rule(self.module_type, None, None).parent_name_template, "")

        InterfaceNameRule.objects.filter(pk=self.rule.pk).update(
            breakout_mode=CHANNELIZED, parent_name_template="et-0/0/{bay_position}"
        )

        self.assertEqual(find_matching_rule(self.module_type, None, None).parent_name_template, "et-0/0/{bay_position}")


class FlatBreakoutModeTest(ChannelizationTestCase):
    """The flat topology is exactly what the plugin did before the mode existed."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkFlat", ["3", "4"])
        cls.explicit_type = _plain_module_type(manufacturer, "BrkFlat-QSFP")
        cls.default_type = _plain_module_type(manufacturer, "BrkFlat-QSFP-DEF")
        cls.explicit_rule = InterfaceNameRule.objects.create(
            module_type=cls.explicit_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=FLAT,
            channel_count=4,
            channel_start=0,
        )
        # No mode given: what a rule migrated from before Phase B looks like.
        cls.default_rule = InterfaceNameRule.objects.create(
            module_type=cls.default_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        cls.first_channel_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="BrkFlat-FIRST", part_number="BrkFlat-FIRST"
        )
        InterfaceTemplate.objects.create(
            module_type=cls.first_channel_type, name="Ethernet{module}/1", type=PARENT_TYPE
        )
        cls.first_channel_rule = InterfaceNameRule.objects.create(
            module_type=cls.first_channel_type,
            name_template="Ethernet{bay_position}/{channel}",
            breakout_mode=FLAT,
            channel_count=4,
            channel_start=1,
        )
        cls.second_channel_fails_type = _plain_module_type(manufacturer, "BrkFlat-DIV")
        # The second channel divides by zero, so the rule can name no family at all.
        cls.second_channel_fails_rule = InterfaceNameRule.objects.create(
            module_type=cls.second_channel_fails_type,
            name_template="x{base}:{12 // ({channel} - 2)}",
            breakout_mode=FLAT,
            channel_count=2,
            channel_start=1,
        )

    def _assert_flat_family(self, module, bay_position):
        """Assert *module* carries N plain siblings and no channelized structure at all."""
        self.assertEqual(
            self._names(module),
            [f"xe-0/0/{bay_position}:{channel}" for channel in range(4)],
        )
        for iface in Interface.objects.filter(module=module):
            self.assertIsNone(getattr(iface, "channels", None))
            self.assertIsNone(getattr(iface, "channel_id", None))
            self.assertEqual(iface.type, PARENT_TYPE)

    def test_a_flat_rule_creates_flat_siblings(self):
        """The base becomes the first channel and the rest are plain siblings — unchanged behaviour."""
        module, bay = self._install(self.explicit_type, "3", run_rules=False)

        self.assertEqual(apply_interface_name_rules(module, bay), 4)
        self._assert_flat_family(module, "3")

    def test_a_migrated_rule_behaves_like_an_explicitly_flat_one(self):
        """Rules that existed before the mode must keep producing the same interfaces."""
        module, bay = self._install(self.default_type, "4", run_rules=False)

        self.assertEqual(apply_interface_name_rules(module, bay), 4)
        self._assert_flat_family(module, "4")

    def test_a_name_that_spells_a_family_the_rule_cannot_name_is_no_claim(self):
        """``x3:-12`` spells the first channel, but no family exists, so the raw name stays one claim."""
        module, _ = self._install(self.second_channel_fails_type, "3")
        Interface.objects.create(device=self.device, module=module, name="x3:-12", type=PARENT_TYPE)

        outcome = apply_rule_to_existing(self.second_channel_fails_rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(self._names(module), ["3", "x3:-12"])
        reasons = {member.current_name: member.reason for member in outcome.skipped_members}
        self.assertTrue(reasons["3"].startswith("failed to evaluate the family names"), reasons)
        self.assertEqual(reasons["x3:-12"], UNCLAIMED_BASE_REASON)

    def test_a_raw_name_that_is_the_first_name_of_its_family_builds_the_family(self):
        """The template claims ``Ethernet3/1`` as its raw name and as its family's first name: one claim."""
        module, _ = self._install(self.first_channel_type, "3")
        family = [f"Ethernet3/{channel}" for channel in range(1, 5)]

        self.assertEqual(self._names(module), family)
        self.assertEqual(apply_rule_to_existing(self.first_channel_rule).changed_count, 0)
        self.assertEqual(self._names(module), family)


class FlatBreakoutClaimGateTest(ChannelizationTestCase):
    """A breakout rule builds a family only on a name that one template alone claims, in Apply Rules too."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device(
            "BrkGate", ["5"], virtual_chassis=VirtualChassis.objects.create(name="brkgate-vc"), vc_position=2
        )
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="BrkGate-QSFP", part_number="BrkGate-QSFP"
        )
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="z", type=PARENT_TYPE)

    def test_apply_rules_builds_no_family_on_names_the_claim_refuses(self):
        """``z`` is the raw name and ``x-1:0``, ``x-1:1`` the family the rule gave at position 1: one template claims both."""
        module, _ = self._install(self.module_type, "5")
        for name in ("x-1:0", "x-1:1"):
            Interface.objects.create(device=self.device, module=module, name=name, type=PARENT_TYPE)
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="x-{vc_position}:{channel}",
            breakout_mode=FLAT,
            channel_count=2,
            channel_start=0,
        )

        self.assertEqual(find_interfaces_for_rule(rule), ([], 3))
        outcome = apply_rule_to_existing(rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(self._names(module), ["x-1:0", "x-1:1", "z"])
        self.assertEqual(
            sorted((member.current_name, member.reason) for member in outcome.skipped_members),
            [(name, UNCLAIMED_BASE_REASON) for name in ("x-1:0", "x-1:1", "z")],
        )

    def test_a_selected_interface_the_claim_refuses_is_reported(self):
        """Template ``x0:1`` claims its raw name and the family ``xx0:1:0``, so it is refused.

        The family that template ``0`` would build takes the name ``x0:1``. Apply Rules with only
        ``x0:1`` selected must still report it.
        """
        module_type = ModuleType.objects.create(
            manufacturer=self.module_type.manufacturer, model="BrkGate-TWO", part_number="BrkGate-TWO"
        )
        for name in ("0", "x0:1"):
            InterfaceTemplate.objects.create(module_type=module_type, name=name, type=PARENT_TYPE)
        module, _ = self._install(module_type, "5")
        Interface.objects.create(device=self.device, module=module, name="xx0:1:0", type=PARENT_TYPE)
        rule = InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="x{base}:{channel}",
            breakout_mode=FLAT,
            channel_count=2,
            channel_start=0,
        )
        selected = Interface.objects.get(module=module, name="x0:1")

        preview, _checked = find_interfaces_for_rule(rule)
        outcome = apply_rule_to_existing(rule, interface_ids=[selected.pk])

        self.assertEqual([(entry["current_name"], entry["new_names"]) for entry in preview], [("0", ["x0:0", "x0:1"])])
        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(
            [(member.current_name, member.reason) for member in outcome.skipped_members],
            [("x0:1", UNCLAIMED_BASE_REASON)],
        )
        self.assertEqual(self._names(module), ["0", "x0:1", "xx0:1:0"])

    def test_a_blocked_family_takes_no_name_from_another_interface(self):
        """The family on ``0`` is blocked, so ``x0:1`` keeps its own outcome although the family names it."""
        module_type = ModuleType.objects.create(
            manufacturer=self.module_type.manufacturer, model="BrkGate-CH", part_number="BrkGate-CH"
        )
        InterfaceTemplate.objects.create(module_type=module_type, name="0", type=PARENT_TYPE)
        module, _ = self._install(module_type, "5")
        Interface.objects.create(device=self.device, module=module, name="x0:1", type=PARENT_TYPE)
        rule = InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="x{base}:{channel}",
            parent_name_template="p{base}",
            breakout_mode=CHANNELIZED,
            channel_count=2,
            channel_start=0,
        )

        outcome = apply_rule_to_existing(
            rule, interface_ids=Interface.objects.filter(module=module).values_list("pk", flat=True)
        )

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(self._names(module), ["0", "x0:1"])
        reasons = {member.current_name: member.reason for family in outcome.families for member in family.members}
        self.assertEqual(sorted(reasons), ["0", "x0:1"])
        self.assertEqual(reasons["x0:1"], UNCLAIMED_BASE_REASON)

    def test_prediction_keeps_the_names_the_claim_refuses(self):
        module, bay = self._install(self.module_type, "5")
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="x-{vc_position}:{channel}",
            breakout_mode=FLAT,
            channel_count=2,
            channel_start=0,
        )

        self.assertEqual(predict_rule_output(module, bay, ["z"]), ["x-2:0", "x-2:1"])
        self.assertEqual(predict_rule_output(module, bay, ["z", "x-1:0"]), ["z", "x-1:0"])


@contextmanager
def _before_the_first_row_lock(action):
    """Run *action* once, just before the executor locks its first row, as a concurrent writer would."""
    pending = [action]

    def wrapper(execute, sql, params, many, context):
        if pending and "FOR UPDATE" in sql:
            pending.pop()()
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        yield


class ExecutionOutcomeCoverageTest(ChannelizationTestCase):
    """Each selected interface is in exactly one member outcome, and no interface in two, whatever happens."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkExec", ["5"])
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="BrkExec-ZERO", part_number="BrkExec-ZERO"
        )
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="0", type=PARENT_TYPE)
        cls.pair_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="BrkExec-PAIR", part_number="BrkExec-PAIR"
        )
        for name in ("0", "x0:1"):
            InterfaceTemplate.objects.create(module_type=cls.pair_type, name=name, type=PARENT_TYPE)

    def _module_with(self, *names, module_type=None):
        """Install the module and add the interfaces *names* beside the ones its templates give."""
        module, _ = self._install(module_type or self.module_type, "5", run_rules=False)
        for name in names:
            Interface.objects.create(device=self.device, module=module, name=name, type=PARENT_TYPE)
        return module

    def _half_built_family(self, *names):
        """Return a module whose template ``0`` claims the flat family that *names* begin, with its raw name gone."""
        module = self._module_with(*names[1:])
        rename_out_of_band(Interface.objects.get(module=module, name="0"), names[0])
        return module

    def _rule(self, name_template, channel_count=2, module_type=None, **fields):
        return InterfaceNameRule.objects.create(
            module_type=module_type or self.module_type,
            name_template=name_template,
            breakout_mode=fields.pop("breakout_mode", FLAT),
            channel_count=channel_count,
            channel_start=0,
            **fields,
        )

    def _apply_to_every_interface(self, rule, module):
        """Apply *rule* with every interface of *module* selected; each is in one member outcome, none in two."""
        selected = dict(Interface.objects.filter(module=module).values_list("name", "pk"))
        outcome = apply_rule_to_existing(rule, interface_ids=selected.values())
        counts = Counter(member.interface_pk for family in outcome.families for member in family.members)
        self.assertEqual({name: counts[pk] for name, pk in selected.items()}, dict.fromkeys(selected, 1))
        self.assertEqual([pk for pk, count in counts.items() if count > 1], [])
        return {
            member.current_name: (member.status, member.reason)
            for family in outcome.families
            for member in family.members
        }

    def test_a_row_named_like_a_family_member_keeps_its_own_outcome(self):
        """The sibling has a name the family would take, but no template claims it: the family is refused."""
        sibling = "x" * 63 + "9"
        module = self._module_with(sibling)
        rule = self._rule("x" * 63 + "{10 - {channel}}")

        members = self._apply_to_every_interface(rule, module)

        self.assertEqual(members["0"], (FamilyStatus.BLOCKED, f"{COLLISION_REASON}: {sibling}"))
        self.assertEqual(members[sibling], (FamilyStatus.BLOCKED, UNCLAIMED_BASE_REASON))
        self.assertEqual(self._names(module), ["0", sibling])

    def test_a_family_netbox_rejects_reports_its_planned_member(self):
        """The third name has 65 characters, so NetBox rejects the family its first two rows rebuild."""
        prefix = "x" * 60 + "0:"
        module = self._half_built_family(prefix + "0", prefix + "50")
        rule = self._rule("x" * 60 + "{base}:{{channel} * {channel} * {channel} * 50}", channel_count=3)

        members = self._apply_to_every_interface(rule, module)

        self.assertEqual(members[prefix + "0"][0], FamilyStatus.BLOCKED)
        self.assertEqual(members[prefix + "50"], members[prefix + "0"])
        self.assertEqual(self._names(module), [prefix + "0", prefix + "50"])

    def test_a_first_name_in_use_refuses_the_family(self):
        module = self._module_with("x0:1")
        Interface.objects.create(device=self.device, name="x0:0", type=PARENT_TYPE)

        members = self._apply_to_every_interface(self._rule("x{base}:{channel}"), module)

        self.assertEqual(members["0"], (FamilyStatus.BLOCKED, f"{COLLISION_REASON}: x0:0"))
        self.assertEqual(members["x0:1"], (FamilyStatus.BLOCKED, UNCLAIMED_BASE_REASON))
        self.assertEqual(self._names(module), ["0", "x0:1"])

    def test_a_raw_name_another_family_would_take_builds_its_own_family(self):
        """Template ``0``'s family names ``x0:1``, the raw name of the other template, so it is refused."""
        module = self._module_with(module_type=self.pair_type)
        rule = self._rule("x{base}:{channel}", module_type=self.pair_type)

        members = self._apply_to_every_interface(rule, module)

        self.assertEqual(members["0"], (FamilyStatus.BLOCKED, f"{COLLISION_REASON}: x0:1"))
        self.assertEqual(members["x0:1"][0], FamilyStatus.CHANGED)
        self.assertEqual(self._names(module), ["0", "xx0:1:0", "xx0:1:1"])

    def test_a_name_collision_race_reports_the_planned_member(self):
        module = self._half_built_family("x0:0", "x0:1")
        rule = self._rule("x{base}:{channel}", channel_count=3)
        real_save = Interface.save

        def losing_save(interface, *args, **kwargs):
            if interface.name == "x0:2":
                cause = Exception("duplicate key value violates unique constraint")
                cause.diag = SimpleNamespace(constraint_name=INTERFACE_NAME_CONSTRAINT)
                raise IntegrityError("duplicate key value violates unique constraint") from cause
            return real_save(interface, *args, **kwargs)

        with patch.object(Interface, "save", losing_save):
            members = self._apply_to_every_interface(rule, module)

        self.assertEqual(members["x0:0"], (FamilyStatus.BLOCKED, COLLISION_REASON))
        self.assertEqual(members["x0:1"], members["x0:0"])
        self.assertEqual(self._names(module), ["x0:0", "x0:1"])

    def test_a_base_renamed_to_a_family_name_after_planning_is_reported_once(self):
        module = self._module_with()
        base = Interface.objects.get(module=module, name="0")

        with _before_the_first_row_lock(lambda: rename_out_of_band(base, "x0:1")):
            members = self._apply_to_every_interface(self._rule("x{base}:{channel}"), module)

        self.assertEqual(members, {"0": (FamilyStatus.STALE, STALE_REASON)})
        self.assertEqual(self._names(module), ["x0:1"])

    def test_both_rows_renamed_after_planning_are_each_reported(self):
        module = self._module_with("x0:1")
        rows = {row.name: row for row in Interface.objects.filter(module=module)}

        def rename_both():
            rename_out_of_band(rows["0"], "a")
            rename_out_of_band(rows["x0:1"], "b")

        with _before_the_first_row_lock(rename_both):
            members = self._apply_to_every_interface(self._rule("x{base}:{channel}"), module)

        self.assertEqual(members["0"], (FamilyStatus.STALE, STALE_REASON))
        self.assertEqual(members["x0:1"], (FamilyStatus.BLOCKED, UNCLAIMED_BASE_REASON))
        self.assertEqual(self._names(module), ["a", "b"])

    def test_a_planned_member_renamed_after_planning_refuses_the_family(self):
        module = self._half_built_family("x0:0", "x0:1")
        member = Interface.objects.get(module=module, name="x0:1")

        with _before_the_first_row_lock(lambda: rename_out_of_band(member, "moved")):
            members = self._apply_to_every_interface(self._rule("x{base}:{channel}", channel_count=3), module)

        self.assertEqual(members["x0:0"], (FamilyStatus.STALE, STALE_REASON))
        self.assertEqual(members["x0:1"], members["x0:0"])
        self.assertEqual(self._names(module), ["moved", "x0:0"])

    def test_a_base_and_its_planned_member_renamed_after_planning_are_each_reported_once(self):
        """The member takes the name the base had, so only the primary keys tell the two rows apart."""
        module = self._half_built_family("x0:0", "x0:1")
        rows = {row.name: row for row in Interface.objects.filter(module=module)}

        def swap_names():
            rename_out_of_band(rows["x0:0"], "a")
            rename_out_of_band(rows["x0:1"], "x0:0")

        with _before_the_first_row_lock(swap_names):
            members = self._apply_to_every_interface(self._rule("x{base}:{channel}", channel_count=3), module)

        self.assertEqual(members, dict.fromkeys(("x0:0", "x0:1"), (FamilyStatus.STALE, STALE_REASON)))
        self.assertEqual(self._names(module), ["a", "x0:0"])

    def test_a_half_built_family_is_completed_through_its_planned_member(self):
        module = self._half_built_family("x0:0", "x0:1")

        members = self._apply_to_every_interface(self._rule("x{base}:{channel}", channel_count=3), module)

        self.assertEqual(members["x0:0"], (FamilyStatus.UNCHANGED, ""))
        self.assertEqual(members["x0:1"], (FamilyStatus.UNCHANGED, ""))
        self.assertEqual(self._names(module), ["x0:0", "x0:1", "x0:2"])

    def test_selecting_a_planned_member_alone_completes_its_family(self):
        module = self._half_built_family("x0:0", "x0:1")
        member = Interface.objects.get(module=module, name="x0:1")
        rule = self._rule("x{base}:{channel}", channel_count=3)

        outcome = apply_rule_to_existing(rule, interface_ids=[member.pk])

        self.assertEqual(
            [(member.current_name, member.status) for family in outcome.families for member in family.members],
            [("x0:0", FamilyStatus.UNCHANGED), ("x0:1", FamilyStatus.UNCHANGED), ("x0:2", FamilyStatus.CHANGED)],
        )
        self.assertEqual(self._names(module), ["x0:0", "x0:1", "x0:2"])

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_a_channel_name_in_use_reports_the_base(self):
        module = self._module_with()
        Interface.objects.create(device=self.device, name="x0:1", type=PARENT_TYPE)
        rule = self._rule("x{base}:{channel}", breakout_mode=CHANNELIZED, parent_name_template="p{base}")

        members = self._apply_to_every_interface(rule, module)

        self.assertEqual(members["0"], (FamilyStatus.BLOCKED, f"{COLLISION_REASON}: x0:1"))
        self.assertEqual(self._names(module), ["0"])

    @skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
    def test_a_flat_expansion_found_at_execution_reports_the_base(self):
        module = self._module_with()
        rule = self._rule("x{base}:{channel}", breakout_mode=CHANNELIZED, parent_name_template="p{base}")

        def add_a_sibling():
            Interface.objects.create(device=self.device, module=module, name="late", type=PARENT_TYPE)

        with _before_the_first_row_lock(add_a_sibling):
            members = self._apply_to_every_interface(rule, module)

        self.assertEqual(members["0"], (FamilyStatus.STALE, MODULE_CHANGED_REASON))
        self.assertEqual(self._names(module), ["0", "late"])


@skipIf(supports_channelization(), "requires a NetBox that cannot model channelized interfaces (4.6 and older)")
class ChannelizedModeWithoutSupportTest(ChannelizationTestCase):
    """Where NetBox has no channel model, a channelized rule is skipped — never downgraded to flat."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("BrkNoSup", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "BrkNoSup-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def test_the_rule_is_skipped_and_reported(self):
        """A flat family here would be the wrong topology, silently — so nothing is created."""
        module, bay = self._install(self.module_type, "3", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["3"])
        self.assertTrue(any("channelized" in line.lower() for line in logs.output), logs.output)

    def test_the_bulk_apply_path_skips_it_too(self):
        """Both entry points refuse the rule, so neither can build a flat family behind the other's back."""
        module, _ = self._install(self.module_type, "3", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            built = apply_rule_to_existing(self.rule)

        self.assertEqual(built.changed_count, 0)
        self.assertEqual(self._names(module), ["3"])
        self.assertTrue(any("channelized" in line.lower() for line in logs.output), logs.output)

    def test_the_preview_offers_nothing_it_cannot_build(self):
        """The Apply page must not promise a family this release has no rows for."""
        self._install(self.module_type, "3", run_rules=False)

        results, total_checked = find_interfaces_for_rule(self.rule)

        self.assertEqual(results, [])
        self.assertEqual(total_checked, 1)

    def test_prediction_leaves_the_names_alone(self):
        """Nothing is built here, so an integration must not be handed channel names that never appear."""
        module, bay = self._install(self.module_type, "3", run_rules=False)
        raw_names = self._names(module)

        predicted = predict_rule_output(module, bay, raw_names)

        self.assertEqual(predicted, raw_names)
        apply_interface_name_rules(module, bay)
        self.assertEqual(self._names(module), raw_names)

    def test_the_skip_is_not_read_as_an_obsolete_rule(self):
        """The rule is unusable on this release, not redundant — it must not be tagged deprecated."""
        module, bay = self._install(self.module_type, "3", run_rules=False)

        apply_interface_name_rules(module, bay)

        self.assertFalse(self.rule.tags.filter(slug="potentially-deprecated").exists())


class BreakoutModeMigrationTest(TestCase):
    """The migration that adds the mode must land every existing rule in the flat topology.

    The schema changes run inside the test's own transaction (PostgreSQL DDL is transactional), so
    the plugin's tables and migration history are restored by the same rollback as any other test.
    """

    APP = "netbox_interface_name_rules"
    BEFORE = "0012_remove_interfacenamerule_interfacenamerule_unique_exact_and_more"

    def _migrate(self, target):
        """Migrate the plugin to *target* and return the resulting project state."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        return executor.migrate([(self.APP, target)])

    def _latest_migration(self):
        """Return the name of the plugin's newest migration."""
        loader = MigrationLoader(connection)
        return max(name for app_label, name in loader.graph.leaf_nodes(self.APP))

    def test_the_migrations_describe_the_models_as_they_are_now(self):
        """A model change shipped without its migration only fails later, on someone else's upgrade.

        NetBox drops ``choices``, ``help_text`` and ``verbose_name`` from field deconstruction
        (``utilities.migration.custom_deconstruct``), so this is a schema-level comparison.
        """
        loader = MigrationLoader(None, ignore_no_migrations=True)
        autodetector = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(apps),
            NonInteractiveMigrationQuestioner(specified_apps={self.APP}, dry_run=True),
        )

        changes = autodetector.changes(graph=loader.graph, trim_to_apps={self.APP}, convert_apps={self.APP})

        pending = [op.describe() for migration in changes.get(self.APP, []) for op in migration.operations]
        self.assertEqual(pending, [])

    def test_rules_created_before_the_migration_become_flat(self):
        """An upgrade must not change what a single existing rule does on the next module install."""
        latest = self._latest_migration()
        self.addCleanup(self._migrate, latest)
        old_state = self._migrate(self.BEFORE)
        pk = (
            old_state.apps.get_model(self.APP, "InterfaceNameRule")
            .objects.create(
                module_type_is_regex=True,
                module_type_pattern="MIG-.*",
                name_template="xe-0/0/{bay_position}:{channel}",
                channel_count=4,
            )
            .pk
        )
        # Fire the deferred FK triggers the insert queued, or the forward migration cannot index.
        connection.check_constraints()

        new_state = self._migrate(latest)

        migrated = new_state.apps.get_model(self.APP, "InterfaceNameRule").objects.get(pk=pk)
        self.assertEqual(migrated.breakout_mode, FLAT)
        self.assertEqual(migrated.parent_name_template, "")
