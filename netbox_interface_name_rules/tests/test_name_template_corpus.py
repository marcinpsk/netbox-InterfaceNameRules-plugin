# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Characterize name-template acceptance through every rule input adapter."""

from dataclasses import dataclass

from dcim.models import Manufacturer, ModuleType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from utilities.testing import APITestCase

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.forms import InterfaceNameRuleForm, InterfaceNameRuleImportForm, RuleTestForm
from netbox_interface_name_rules.models import InterfaceNameRule


@dataclass(frozen=True)
class AdapterVerdicts:
    """Record whether each rule input adapter accepts one corpus entry."""

    edit_form: bool
    rest_api: bool
    bulk_import: bool
    rule_tester: bool
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


ACCEPTED = AdapterVerdicts(True, True, True, True, True, True)
REFUSED = AdapterVerdicts(False, False, False, False, False, False)

NAME_TEMPLATE_CORPUS = (
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
        ACCEPTED,
        applies_to_device_interfaces=True,
    ),
    TemplateCase("unbalanced name template", "xe-{bay_position", ACCEPTED),
    TemplateCase("unknown template variable", "xe-{unknown}", ACCEPTED),
    TemplateCase("format conversion", "xe-{bay_position!r}", ACCEPTED),
    TemplateCase("channel without declared channels", "xe-{channel}", ACCEPTED),
    TemplateCase(
        "module parent names channel",
        "{base}:{channel}",
        REFUSED,
        parent_name_template="parent-{channel}",
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
    """Pin every current name-template verdict without correcting it."""

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

    @staticmethod
    def _tester_data(fields):
        """Return the subset of one corpus entry that the rule tester can express."""
        return {
            name: value
            for name, value in fields.items()
            if name
            in {
                "name_template",
                "parent_name_template",
                "breakout_mode",
                "channel_count",
                "channel_start",
            }
        }

    def test_rule_edit_form_verdicts(self):
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                fields = self._rule_fields(case, "edit", index)
                form = InterfaceNameRuleForm(data=self._form_data(fields))
                accepted = form.is_valid()
                if accepted:
                    form.save()
                self.assertEqual(accepted, case.verdicts.edit_form, form.errors)

    def test_rest_api_verdicts(self):
        url = self._get_list_url()
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                fields = self._form_data(self._rule_fields(case, "api", index))
                response = self.client.post(url, fields, format="json", **self.header)
                accepted = response.status_code == 201
                self.assertEqual(accepted, case.verdicts.rest_api, response.data)

    def test_bulk_import_form_verdicts(self):
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                fields = self._rule_fields(case, "import", index)
                form = InterfaceNameRuleImportForm(data=self._import_data(fields))
                accepted = form.is_valid()
                if accepted:
                    form.save()
                self.assertEqual(accepted, case.verdicts.bulk_import, form.errors)

    def test_rule_tester_verdicts(self):
        for index, case in enumerate(NAME_TEMPLATE_CORPUS):
            with self.subTest(case=case.label):
                fields = self._rule_fields(case, "tester", index)
                form = RuleTestForm(data=self._tester_data(fields))
                self.assertEqual(form.is_valid(), case.verdicts.rule_tester, form.errors)

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
