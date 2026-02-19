from django import forms
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm, NetBoxModelImportForm

from .models import InterfaceNameRule


class InterfaceNameRuleForm(NetBoxModelForm):
    class Meta:
        model = InterfaceNameRule
        fields = [
            "module_type",
            "parent_module_type",
            "device_type",
            "name_template",
            "channel_count",
            "channel_start",
            "description",
        ]


class InterfaceNameRuleImportForm(NetBoxModelImportForm):
    class Meta:
        model = InterfaceNameRule
        fields = [
            "module_type",
            "parent_module_type",
            "device_type",
            "name_template",
            "channel_count",
            "channel_start",
            "description",
        ]


class InterfaceNameRuleFilterForm(NetBoxModelFilterSetForm):
    module_type_id = forms.IntegerField(required=False, label="Module Type ID")
    parent_module_type_id = forms.IntegerField(required=False, label="Parent Module Type ID")
    device_type_id = forms.IntegerField(required=False, label="Parent Device Type ID")

    model = InterfaceNameRule
