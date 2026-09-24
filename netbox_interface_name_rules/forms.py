# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from dcim.models import Device, DeviceType, Interface, ModuleBay, ModuleType, Platform
from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from netbox.forms import (
    NetBoxModelBulkEditForm,
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
)
from utilities.forms import add_blank_choice
from utilities.forms.fields import CSVModelChoiceField, DynamicModelChoiceField
from utilities.forms.rendering import FieldSet
from utilities.forms.widgets import BulkEditNullBooleanSelect

from .choices import BreakoutModeChoices
from .models import InterfaceNameRule
from .name_template import validate_rule

# A preview variable holds a real position or interface name, so the model fields bound them.
_POSITION_MAX_LENGTH = ModuleBay._meta.get_field("position").max_length
_VC_POSITION_VALIDATORS = Device._meta.get_field("vc_position").validators
_VC_POSITION_MIN = max(v.limit_value for v in _VC_POSITION_VALIDATORS if isinstance(v, MinValueValidator))
_VC_POSITION_MAX = min(v.limit_value for v in _VC_POSITION_VALIDATORS if isinstance(v, MaxValueValidator))
_INTERFACE_NAME_MAX_LENGTH = Interface._meta.get_field("name").max_length


class RuleTestForm(forms.Form):
    """Standalone form for previewing interface name rule output without saving."""

    applies_to_device_interfaces = forms.BooleanField(
        required=False,
        label="Device-level interfaces",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    var_vc_position = forms.IntegerField(
        required=False,
        initial=1,
        min_value=_VC_POSITION_MIN,
        max_value=_VC_POSITION_MAX,
        label="{vc_position}",
        help_text="Leave blank for a device outside a virtual chassis.",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )

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
        label="Module Type Pattern (RE2)",
        help_text="RE2 pattern matched against the complete ModuleType.model value",
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
    parent_name_template = forms.CharField(
        required=False,
        label="Parent Name Template",
        help_text="Channelized mode only: name for the parent interface, e.g. et-0/0/{bay_position}",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    breakout_mode = forms.ChoiceField(
        required=False,
        choices=BreakoutModeChoices,
        initial=BreakoutModeChoices.FLAT,
        label="Breakout Mode",
        help_text="flat = sibling interfaces; channelized = one parent with channel subinterfaces",
        widget=forms.Select(attrs={"class": "form-select"}),
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
        required=False,
        initial="1",
        max_length=_POSITION_MAX_LENGTH,
        label="{slot}",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    var_bay_position = forms.CharField(
        required=False,
        initial="1",
        max_length=_POSITION_MAX_LENGTH,
        label="{bay_position}",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    var_parent_bay_position = forms.CharField(
        required=False,
        initial="1",
        max_length=_POSITION_MAX_LENGTH,
        label="{parent_bay_position}",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    var_base = forms.CharField(
        required=False,
        initial="Ethernet1",
        max_length=_INTERFACE_NAME_MAX_LENGTH,
        label="{base} (interface name)",
        help_text="Device rules derive {port} from the segment after the last slash, or the full name without a slash.",
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def clean(self):
        """Validate rule matching, templates, and the selected breakout topology."""
        cleaned_data = super().clean()
        self._clean_rule(cleaned_data)
        module_type_is_regex = cleaned_data.get("module_type_is_regex", False)
        module_type = cleaned_data.get("module_type")
        module_type_pattern = cleaned_data.get("module_type_pattern", "")

        if cleaned_data.get("applies_to_device_interfaces"):
            cleaned_data["module_type_is_regex"] = False
            if module_type:
                self.add_error("module_type", "Module type must be empty for device-level interface rules.")
            if cleaned_data.get("var_vc_position") is None and "var_vc_position" not in self.errors:
                self.add_error("var_vc_position", "A virtual-chassis position is required for a device rule.")
            if module_type_pattern:
                from .regex_safety import compile_module_type_pattern

                try:
                    compile_module_type_pattern(module_type_pattern)
                except ValidationError as exc:
                    for field, messages in exc.message_dict.items():
                        self.add_error(field, messages)
        elif module_type_is_regex:
            if not module_type_pattern:
                self.add_error("module_type_pattern", "A regex pattern is required when regex mode is enabled.")
            else:
                from .regex_safety import compile_module_type_pattern

                try:
                    compile_module_type_pattern(module_type_pattern)
                except ValidationError as exc:
                    for field, messages in exc.message_dict.items():
                        self.add_error(field, messages)
            if module_type:
                self.add_error("module_type", "Module Type (exact) must be empty when regex mode is enabled.")
        else:
            if module_type_pattern:
                self.add_error("module_type_pattern", "Module Type Pattern must be empty when regex mode is disabled.")

        return cleaned_data

    def _clean_rule(self, cleaned_data):
        """Validate the templates and topology for the selected rule kind."""
        try:
            validate_rule(
                breakout_mode=cleaned_data.get("breakout_mode") or BreakoutModeChoices.FLAT,
                channel_count=cleaned_data.get("channel_count") or 0,
                name_template=cleaned_data.get("name_template") or "",
                parent_name_template=cleaned_data.get("parent_name_template") or "",
                applies_to_device_interfaces=cleaned_data.get("applies_to_device_interfaces", False),
            )
        except ValidationError as exc:
            for field, messages in exc.message_dict.items():
                self.add_error(field, messages)

    def clean_breakout_mode(self):
        """Return the flat topology when the field is left blank."""
        return self.cleaned_data.get("breakout_mode") or BreakoutModeChoices.FLAT

    def clean_channel_count(self):
        """Return 0 when the field is blank or None."""
        return self.cleaned_data.get("channel_count") or 0

    def clean_channel_start(self):
        """Return 0 when the field is blank or None."""
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
            "parent_name_template",
            "breakout_mode",
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
    """CSV/YAML bulk-import form for InterfaceNameRule."""

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
            "parent_name_template",
            "breakout_mode",
            "channel_count",
            "channel_start",
            "description",
            "enabled",
            "applies_to_device_interfaces",
        ]


class InterfaceNameRuleBulkEditForm(NetBoxModelBulkEditForm):
    """Bulk-edit form for InterfaceNameRule.

    Only fields that are meaningful to set across many rules at once are offered;
    module_type and the regex-mode flags are per-rule and stay out of it.
    """

    model = InterfaceNameRule

    parent_module_type = DynamicModelChoiceField(queryset=ModuleType.objects.all(), required=False)
    device_type = DynamicModelChoiceField(queryset=DeviceType.objects.all(), required=False)
    platform = DynamicModelChoiceField(queryset=Platform.objects.all(), required=False)
    name_template = forms.CharField(max_length=255, required=False)
    parent_name_template = forms.CharField(max_length=255, required=False)
    # Blank first choice: a bulk edit posts every rendered field, so without a "no change" option a
    # select rewrites the column on every selected rule.
    breakout_mode = forms.ChoiceField(choices=add_blank_choice(BreakoutModeChoices), required=False, initial="")
    channel_count = forms.IntegerField(min_value=0, required=False)
    channel_start = forms.IntegerField(min_value=0, required=False)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    enabled = forms.NullBooleanField(required=False, widget=BulkEditNullBooleanSelect())

    fieldsets = (
        FieldSet(
            "name_template",
            "parent_name_template",
            "breakout_mode",
            "channel_count",
            "channel_start",
            "enabled",
            name="Rule",
        ),
        FieldSet("parent_module_type", "device_type", "platform", name="Scope"),
        FieldSet("description", name="Description"),
    )
    nullable_fields = ("parent_module_type", "device_type", "platform", "parent_name_template", "description")

    def clean(self):
        """Refuse a bulk change before any selected rule is written."""
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data
        nullified = self.data.getlist("_nullify")
        for rule in cleaned_data.get("pk", ()):
            fields = {
                name: cleaned_data[name] if name in self.changed_data else getattr(rule, name)
                for name in ("breakout_mode", "channel_count", "name_template", "parent_name_template")
            }
            if "parent_name_template" in nullified:
                fields["parent_name_template"] = ""
            try:
                validate_rule(**fields, applies_to_device_interfaces=rule.applies_to_device_interfaces)
            except ValidationError as exc:
                for message in exc.messages:
                    self.add_error(None, f"Rule {rule.pk} ({rule}): {message}")
        return cleaned_data


class InterfaceNameRuleFilterForm(NetBoxModelFilterSetForm):
    """Filter form for the InterfaceNameRule list view."""

    q = forms.CharField(required=False, label="Search")
    module_type_id = forms.ModelChoiceField(
        queryset=ModuleType.objects.all(),
        required=False,
        label="Module Type",
    )
    module_type_is_regex = forms.NullBooleanField(required=False, label="Regex Mode")
    applies_to_device_interfaces = forms.NullBooleanField(required=False, label="Device Interface Rules")
    enabled = forms.NullBooleanField(required=False, label="Enabled")
    module_type_pattern = forms.CharField(required=False, label="Pattern (contains)")
    breakout_mode = forms.MultipleChoiceField(choices=BreakoutModeChoices, required=False, label="Breakout Mode")
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
