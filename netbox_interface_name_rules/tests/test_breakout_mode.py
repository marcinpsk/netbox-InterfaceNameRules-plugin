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
from unittest import skipIf

import yaml
from dcim.models import Interface, InterfaceTemplate, ModuleType
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.urls import reverse
from utilities.testing import APITestCase

from netbox_interface_name_rules.engine import (
    _VERSION_COLUMNS,
    apply_interface_name_rules,
    apply_rule_to_existing,
    find_interfaces_for_rule,
    find_matching_rule,
    predict_rule_output,
    supports_channelization,
)
from netbox_interface_name_rules.filters import InterfaceNameRuleFilterSet
from netbox_interface_name_rules.forms import RuleTestForm
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.test_channelization import (
    PARENT_TYPE,
    PLUGIN_LOGGER,
    ChannelizationTestCase,
    _build_device,
)
from netbox_interface_name_rules.views import RulePreview

FLAT = "flat"
CHANNELIZED = "channelized"

TEST_PASSWORD = "testpass123"  # noqa: S105 - test credential only

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
    def _form(**overrides):
        """Return a bound tester form with *overrides* applied over a valid breakout rule."""
        data = {
            "name_template": "xe-0/0/{bay_position}:{channel}",
            "breakout_mode": FLAT,
            "channel_count": "4",
            "channel_start": "0",
        }
        data.update(overrides)
        return RuleTestForm(data=data)

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
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_test"),
            {
                "name_template": "xe-0/0/{bay_position}:{channel}",
                "breakout_mode": CHANNELIZED,
                "channel_count": "0",
                "channel_start": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("channel_count", response.context["form"].errors)
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

        self.assertEqual(built, 0)
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
        return sorted(name for app_label, name in loader.graph.leaf_nodes(self.APP))[-1]

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
