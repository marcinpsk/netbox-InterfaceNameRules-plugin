# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import re

from django import forms
from dcim.models import DeviceType, ModuleType, Platform
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm, NetBoxModelImportForm
from utilities.forms.fields import CSVModelChoiceField

from .models import InterfaceNameRule


class RuleTestForm(forms.Form):
    """Standalone form for previewing interface name rule output without saving."""

    # --- Rule definition ---
    module_type_is_regex = forms.BooleanField(
        required=False,
        label="Use Regex Pattern",
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    module_type = forms.ModelChoiceField(
        queryset=ModuleType.objects.all(),
        required=False,
        label="Module Type (exact)",
        help_text="FK match — used when regex mode is off",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    module_type_pattern = forms.CharField(
        required=False,
        label="Module Type Pattern (regex)",
        help_text="Regex pattern matched against ModuleType.model via re.fullmatch()",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    parent_module_type = forms.ModelChoiceField(
        queryset=ModuleType.objects.all(),
        required=False,
        label="Parent Module Type",
        help_text="Optional: scope to modules installed inside this parent module type",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    device_type = forms.ModelChoiceField(
        queryset=DeviceType.objects.all(),
        required=False,
        label="Device Type",
        help_text="Optional: scope to devices of this hardware model",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    platform = forms.ModelChoiceField(
        queryset=Platform.objects.all(),
        required=False,
        label="Platform",
        help_text="Optional: scope to devices running this software platform/OS (e.g. SONiC, EOS)",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    name_template = forms.CharField(
        required=True,
        label="Name Template",
        help_text="e.g. et-0/0/{bay_position} or {base}:{channel}",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    channel_count = forms.IntegerField(
        required=False,
        initial=0,
        min_value=0,
        label="Channel Count",
        help_text="0 = no breakout; > 0 generates one interface per channel",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    channel_start = forms.IntegerField(
        required=False,
        initial=0,
        min_value=0,
        label="Channel Start",
        help_text="Starting channel index (0 for Juniper, varies for Cisco)",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )

    # --- Variable override fields ---
    var_slot = forms.CharField(
        required=False, initial="1", label="{slot}", widget=forms.TextInput(attrs={"class": "form-control"})
    )
    var_bay_position = forms.CharField(
        required=False, initial="1", label="{bay_position}", widget=forms.TextInput(attrs={"class": "form-control"})
    )
    var_bay_position_num = forms.CharField(
        required=False, initial="1", label="{bay_position_num}", widget=forms.TextInput(attrs={"class": "form-control"})
    )
    var_parent_bay_position = forms.CharField(
        required=False,
        initial="1",
        label="{parent_bay_position}",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    var_sfp_slot = forms.CharField(
        required=False, initial="1", label="{sfp_slot}", widget=forms.TextInput(attrs={"class": "form-control"})
    )
    var_base = forms.CharField(
        required=False,
        initial="Ethernet1",
        label="{base} (current interface name)",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        module_type_is_regex = cleaned_data.get("module_type_is_regex", False)
        module_type = cleaned_data.get("module_type")
        module_type_pattern = cleaned_data.get("module_type_pattern", "")

        if module_type_is_regex:
            if not module_type_pattern:
                self.add_error("module_type_pattern", "A regex pattern is required when regex mode is enabled.")
            else:
                try:
                    re.compile(module_type_pattern)
                except re.error as exc:
                    self.add_error("module_type_pattern", f"Invalid regex: {exc}")
                else:
                    from .models import _REDOS_PATTERN

                    if _REDOS_PATTERN.search(module_type_pattern):
                        self.add_error("module_type_pattern", "Pattern contains potentially unsafe nested quantifiers.")
            if module_type:
                self.add_error("module_type", "Module Type (exact) must be empty when regex mode is enabled.")
        else:
            if module_type_pattern:
                self.add_error("module_type_pattern", "Module Type Pattern must be empty when regex mode is disabled.")

        return cleaned_data

    def clean_channel_count(self):
        return self.cleaned_data.get("channel_count") or 0

    def clean_channel_start(self):
        return self.cleaned_data.get("channel_start") or 0


class InterfaceNameRuleForm(NetBoxModelForm):
    """Add/edit form for InterfaceNameRule.

    Priority is auto-computed from the rule fields — it cannot be set manually.
    Scope fields (parent_module_type, device_type, platform) raise the priority score:
    parent_module_type +400, device_type +200, platform +100 (for regex rules).
    Exact FK rules always outrank regex rules (score 1000+ vs max 955).
    """

    class Meta:
        model = InterfaceNameRule
        fields = [
            "module_type",
            "module_type_pattern",
            "module_type_is_regex",
            "parent_module_type",
            "device_type",
            "platform",
            "name_template",
            "channel_count",
            "channel_start",
            "description",
            "enabled",
            "applies_to_device_interfaces",
        ]
        help_texts = {
            "parent_module_type": (
                "Optional. Restricts this rule to modules installed inside the given parent module type. "
                "Setting this raises the priority score by 400 (regex) or keeps exact priority at 1000+."
            ),
            "device_type": (
                "Optional. Restricts this rule to modules installed in this device model. "
                "Setting this raises the priority score by 200 (regex)."
            ),
            "platform": (
                "Optional. Restricts this rule to devices running this OS/platform. "
                "Setting this raises the priority score by 100 (regex)."
            ),
            "module_type_is_regex": (
                "When checked, use a regex pattern instead of an exact FK. "
                "Note: exact FK rules always outrank regex rules (exact score 1000–1007, regex max 955)."
            ),
        }


class InterfaceNameRuleImportForm(NetBoxModelImportForm):
    # FK fields must declare to_field_name explicitly so YAML/CSV can reference
    # objects by their natural key instead of numeric PK.
    module_type = CSVModelChoiceField(
        queryset=ModuleType.objects.all(),
        required=False,
        to_field_name="model",
        help_text="Module type matched by its model name (e.g. SFP-10G-LR)",
    )
    parent_module_type = CSVModelChoiceField(
        queryset=ModuleType.objects.all(),
        required=False,
        to_field_name="model",
        help_text="Parent module type matched by its model name",
    )
    device_type = CSVModelChoiceField(
        queryset=DeviceType.objects.all(),
        required=False,
        to_field_name="model",
        help_text="Device type matched by its model name (e.g. ACX7024)",
    )
    platform = CSVModelChoiceField(
        queryset=Platform.objects.all(),
        required=False,
        to_field_name="name",
        help_text="Platform matched by its name (e.g. SONiC)",
    )

    class Meta:
        model = InterfaceNameRule
        fields = [
            "module_type",
            "module_type_pattern",
            "module_type_is_regex",
            "parent_module_type",
            "device_type",
            "platform",
            "name_template",
            "channel_count",
            "channel_start",
            "description",
            "enabled",
            "applies_to_device_interfaces",
        ]


class InterfaceNameRuleFilterForm(NetBoxModelFilterSetForm):
    q = forms.CharField(required=False, label="Search")
    module_type_id = forms.ModelChoiceField(
        queryset=ModuleType.objects.all(),
        required=False,
        label="Module Type",
    )
    module_type_is_regex = forms.NullBooleanField(required=False, label="Regex Mode")
    module_type_pattern = forms.CharField(required=False, label="Pattern (contains)")
    parent_module_type_id = forms.ModelChoiceField(
        queryset=ModuleType.objects.all(),
        required=False,
        label="Parent Module Type",
    )
    device_type_id = forms.ModelChoiceField(
        queryset=DeviceType.objects.all(),
        required=False,
        label="Device Type",
    )
    platform_id = forms.ModelChoiceField(
        queryset=Platform.objects.all(),
        required=False,
        label="Platform",
    )

    model = InterfaceNameRule
