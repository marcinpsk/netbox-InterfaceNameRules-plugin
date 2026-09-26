# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Characterize name-template acceptance through every rule input adapter."""

from dataclasses import dataclass

from dcim.models import Manufacturer, ModuleType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from utilities.testing import APITestCase

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.forms import InterfaceNameRuleForm, InterfaceNameRuleImportForm
from netbox_interface_name_rules.models import InterfaceNameRule


@dataclass(frozen=True)
class AdapterVerdicts:
    """Record whether each rule input adapter accepts one corpus entry."""

    edit_form: bool
    rest_api: bool
    bulk_import: bool
    model_validation: bool
    direct_write: bool


@dataclass(frozen=True)
class TemplateCase:
    """Describe one name template and its current adapter verdicts."""

    label: str
    name_template: str
    verdicts: AdapterVerdicts
    parent_name_template: str = ""
    breakout_mode: str = BreakoutModeChoices.FLAT
    channel_count: int = 0
    applies_to_device_interfaces: bool = False
    error_field: str = "name_template"
    error_message: str = ""


ACCEPTED = AdapterVerdicts(True, True, True, True, True)
REFUSED = AdapterVerdicts(False, False, False, False, False)
SHAPE_REFUSAL = (
    " is neither a variable token nor integer arithmetic over variable tokens. "
    "Write each variable as its own {name} token, and use only +, -, *, // and parentheses, "
    "as in {{slot_num} // 2}."
)

NAME_TEMPLATE_CORPUS = (
    TemplateCase("module names port", "{port}", REFUSED),
    TemplateCase("device names channel", "{channel}", REFUSED, applies_to_device_interfaces=True),
    TemplateCase("module names virtual chassis position", "{vc_position}", ACCEPTED),
    TemplateCase(
        "parent names unknown variable",
        "{base}",
        REFUSED,
        parent_name_template="{unknown}",
        breakout_mode=BreakoutModeChoices.CHANNELIZED,
        channel_count=4,
    ),
    TemplateCase("module member variables", "{base}-{slot_num}/{bay_position_num}", ACCEPTED),
    TemplateCase(
        "module member channel",
        "{base}:{channel}",
        ACCEPTED,
        channel_count=4,
    ),
    TemplateCase(
        "module parent variables",
        "{base}:{channel}",
        ACCEPTED,
        parent_name_template="parent-{slot_num}/{bay_position_num}",
        breakout_mode=BreakoutModeChoices.CHANNELIZED,
        channel_count=4,
    ),
    TemplateCase(
        "device interface variables",
        "{base}-{vc_position}-{port}",
        ACCEPTED,
        applies_to_device_interfaces=True,
    ),
    TemplateCase(
        "device interface names a module variable",
        "{base}-{bay_position}",
        REFUSED,
        applies_to_device_interfaces=True,
        error_message="{bay_position} is not available in a device-level rule's name template. "
        "Available: {base}, {port}, {vc_position}.",
    ),
    TemplateCase("unbalanced name template", "xe-{bay_position", REFUSED),
    TemplateCase(
        "unknown template variable",
        "xe-{unknown}",
        REFUSED,
        error_message="{unknown} is not available in a module rule's name template. "
        "Available: {base}, {bay_position}, {bay_position_num}, {parent_bay_position}, "
        "{parent_bay_position_num}, {sfp_slot}, {slot}, {slot_num}, {vc_position}.",
    ),
    TemplateCase(
        "format conversion",
        "xe-{bay_position!r}",
        REFUSED,
        error_message="Name templates take a variable or an arithmetic expression, not str.format "
        "conversions and format specifications: {bay_position!r}",
    ),
    TemplateCase(
        "bare variable name in arithmetic",
        "eth{slot_num // 2}",
        REFUSED,
        error_message="{slot_num // 2}" + SHAPE_REFUSAL,
    ),
    TemplateCase(
        "bare variable name beside a nested token",
        "{slot_num + {sfp_slot}}",
        REFUSED,
        error_message="{slot_num + {sfp_slot}}" + SHAPE_REFUSAL,
    ),
    TemplateCase("spaces around a variable", "{ channel }", REFUSED, channel_count=4),
    TemplateCase("attribute access", "{bay_position.x}", REFUSED),
    TemplateCase("index access", "{bay_position[0]}", REFUSED),
    TemplateCase("true division", "{{slot_num} / 2}", REFUSED),
    TemplateCase(
        "parent template bare variable name in arithmetic",
        "{base}:{channel}",
        REFUSED,
        parent_name_template="p{slot_num // 2}",
        error_field="parent_name_template",
        error_message="{slot_num // 2}" + SHAPE_REFUSAL,
        breakout_mode=BreakoutModeChoices.CHANNELIZED,
        channel_count=4,
    ),
    TemplateCase("variable token in arithmetic", "eth{{slot_num} // 2}", ACCEPTED),
    TemplateCase("converter offset arithmetic", "swp{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}", ACCEPTED),
    TemplateCase("value-dependent zero divisor", "{{slot_num} // {sfp_slot}}", ACCEPTED),
    TemplateCase(
        "power after a placeholder zero divisor",
        "{1 // ({slot_num} - {sfp_slot}) + 2 ** 3}",
        REFUSED,
        error_message="{1 // ({slot_num} - {sfp_slot}) + 2 ** 3}" + SHAPE_REFUSAL,
    ),
    TemplateCase("power of a variable token", "{{slot_num} ** 2}", REFUSED),
    TemplateCase("constant zero divisor is well-shaped", "{8 // 0}", ACCEPTED),
    TemplateCase("literal zero before a variable token", "{0{slot_num}}", ACCEPTED),
    TemplateCase("literal digit after a variable token", "{{slot_num}5}", ACCEPTED),
    TemplateCase("two variable tokens with different leading digits", "{0{slot_num} + {sfp_slot}5}", ACCEPTED),
    TemplateCase(
        "one variable token that needs two leading digits",
        "xe-{0{slot_num} + {slot_num}5}",
        REFUSED,
        error_message="{0{slot_num} + {slot_num}5}" + SHAPE_REFUSAL,
    ),
    TemplateCase("channel without declared channels", "xe-{channel}", REFUSED),
    TemplateCase(
        "module parent names channel",
        "{base}:{channel}",
        REFUSED,
        parent_name_template="parent-{channel}",
        error_field="parent_name_template",
        error_message="The parent interface has no channel number; remove {channel}.",
        breakout_mode=BreakoutModeChoices.CHANNELIZED,
        channel_count=4,
    ),
    TemplateCase(
        "unbalanced module parent template",
        "{base}:{channel}",
        REFUSED,
        parent_name_template="parent-{bay_position",
        breakout_mode=BreakoutModeChoices.CHANNELIZED,
        channel_count=4,
    ),
)


class NameTemplateCorpusTest(APITestCase):
    """Validate naming-context membership through every rule adapter."""

    model = InterfaceNameRule
    view_namespace = "plugins-api:netbox_interface_name_rules"
    user_permissions = ("netbox_interface_name_rules.add_interfacenamerule",)

    @classmethod
    def setUpTestData(cls):
        cls.manufacturer = Manufacturer.objects.create(name="Corpus Mfg", slug="corpus-mfg")

    def _rule_fields(self, case, adapter, index):
        """Return model-shaped fields with a unique rule scope."""
        fields = {
            "name_template": case.name_template,
            "parent_name_template": case.parent_name_template,
            "breakout_mode": case.breakout_mode,
            "channel_count": case.channel_count,
            "channel_start": 0,
            "applies_to_device_interfaces": case.applies_to_device_interfaces,
        }
        if case.applies_to_device_interfaces:
            fields["module_type_pattern"] = f"^corpus-{adapter}-{index}$"
        else:
            model = f"CORPUS-{adapter}-{index}"
            fields["module_type"] = ModuleType.objects.create(
                manufacturer=self.manufacturer,
                model=model,
                part_number=model,
            )
        return fields

    @staticmethod
    def _form_data(fields):
        """Convert model-shaped fields to the values the edit form receives."""
        data = dict(fields)
        if module_type := data.get("module_type"):
            data["module_type"] = module_type.pk
        return data

    @staticmethod
    def _import_data(fields):
        """Convert model-shaped fields to the natural keys the import form receives."""
        data = dict(fields)
        if module_type := data.get("module_type"):
            data["module_type"] = module_type.model
        return data

    def test_rule_edit_form_verdicts(self):
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                fields = self._rule_fields(case, "edit", index)
                form = InterfaceNameRuleForm(data=self._form_data(fields))
                accepted = form.is_valid()
                if accepted:
                    form.save()
                self.assertEqual(accepted, case.verdicts.edit_form, form.errors)
                if case.error_message and not case.verdicts.edit_form:
                    self.assertIn(case.error_message, form.errors[case.error_field])

    def test_rest_api_verdicts(self):
        url = self._get_list_url()
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                fields = self._form_data(self._rule_fields(case, "api", index))
                response = self.client.post(url, fields, format="json", **self.header)
                accepted = response.status_code == 201
                self.assertEqual(accepted, case.verdicts.rest_api, response.data)
                if case.error_message and not case.verdicts.rest_api:
                    self.assertIn(case.error_message, response.data[case.error_field])

    def test_bulk_import_form_verdicts(self):
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                fields = self._rule_fields(case, "import", index)
                form = InterfaceNameRuleImportForm(data=self._import_data(fields))
                accepted = form.is_valid()
                if accepted:
                    form.save()
                self.assertEqual(accepted, case.verdicts.bulk_import, form.errors)
                if case.error_message and not case.verdicts.bulk_import:
                    self.assertIn(case.error_message, form.errors[case.error_field])

    def test_rule_tester_verdicts(self):
        self.client.force_login(self.user)
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                fields = self._rule_fields(case, "tester", index)
                response = self.client.post(url, {**self._form_data(fields), "action": "check", "var_vc_position": "2"})
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertEqual(form.is_valid(), case.verdicts.edit_form, form.errors)
                if case.error_message and not case.verdicts.edit_form:
                    self.assertIn(case.error_message, form.errors[case.error_field])

    def test_model_validation_verdicts(self):
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                rule = InterfaceNameRule(**self._rule_fields(case, "model", index))
                try:
                    rule.full_clean()
                except ValidationError:
                    accepted = False
                else:
                    accepted = True
                self.assertEqual(accepted, case.verdicts.model_validation)

    def test_direct_write_verdicts(self):
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label), transaction.atomic():
                try:
                    rule = InterfaceNameRule.objects.create(**self._rule_fields(case, "write", index))
                except (IntegrityError, ValidationError):
                    accepted = False
                else:
                    accepted = True
                    rule.refresh_from_db()
                    self.assertEqual(rule.name_template, case.name_template)
                self.assertEqual(accepted, case.verdicts.direct_write)
