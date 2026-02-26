# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_tables2 as tables
from django.utils.html import format_html
from netbox.tables import NetBoxTable, columns

from .models import InterfaceNameRule


class SpecificityColumn(tables.Column):
    """Renders the rule's specificity_score as a badge with its label."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("verbose_name", "Priority")
        kwargs.setdefault("orderable", False)
        kwargs.setdefault("attrs", {"th": {"title": "Higher score = higher priority in rule lookup"}})
        super().__init__(*args, **kwargs)

    def render(self, value, record):
        label = record.specificity_label
        if not record.module_type_is_regex:
            css = "text-bg-success"  # exact FK — always highest priority
        elif record.device_type_id or record.parent_module_type_id:
            css = "text-bg-primary"  # device- or parent-scoped regex
        elif record.platform_id:
            css = "text-bg-info"  # platform-scoped regex
        else:
            css = "text-bg-secondary"  # unscoped (global) regex
        return format_html(
            '<span class="badge {}" title="{}" style="font-family:monospace">{}</span>',
            css,
            label,
            value,
        )

    def value(self, value, record=None, **kwargs):
        return value


class InterfaceNameRuleTable(NetBoxTable):
    pk = columns.ToggleColumn()
    enabled = tables.TemplateColumn(
        template_code="""
{% if record.enabled %}
<form method="post" action="{% url 'plugins:netbox_interface_name_rules:interfacenamerule_toggle' record.pk %}" class="toggle-form" style="display:inline">
  {% csrf_token %}
  <button type="submit" class="btn btn-xs btn-success toggle-btn" data-pk="{{ record.pk }}" title="Enabled — click to disable" aria-label="Toggle rule enabled" style="padding:1px 5px;font-size:0.75rem">
    <i class="mdi mdi-check"></i>
  </button>
</form>
{% else %}
<form method="post" action="{% url 'plugins:netbox_interface_name_rules:interfacenamerule_toggle' record.pk %}" class="toggle-form" style="display:inline">
  {% csrf_token %}
  <button type="submit" class="btn btn-xs btn-secondary toggle-btn" data-pk="{{ record.pk }}" title="Disabled — click to enable" aria-label="Toggle rule enabled" style="padding:1px 5px;font-size:0.75rem">
    <i class="mdi mdi-minus"></i>
  </button>
</form>
{% endif %}
""",
        verbose_name="Enabled",
        orderable=True,
        attrs={"td": {"class": "text-center"}, "th": {"class": "text-center"}},
    )
    specificity_score = SpecificityColumn()
    module_type = tables.Column(verbose_name="Module Type", linkify=True)
    module_type_pattern = tables.Column(verbose_name="Pattern")
    module_type_is_regex = columns.BooleanColumn(verbose_name="Regex")
    parent_module_type = tables.Column(verbose_name="Parent Module Type", linkify=True)
    device_type = tables.Column(verbose_name="Device Type", linkify=True)
    platform = tables.Column(verbose_name="Platform", linkify=True)
    name_template = tables.Column(verbose_name="Name Template")
    channel_count = tables.Column(verbose_name="Channels")
    channel_start = tables.Column(verbose_name="Ch. Start")
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(
        actions=("edit", "delete"),
        extra_buttons=(
            "<a href=\"{% url 'plugins:netbox_interface_name_rules:interfacenamerule_test' %}?rule_id={{ record.pk }}\""
            ' class="btn btn-sm btn-outline-secondary" title="Test in Build Rule" aria-label="Test in Build Rule">'
            '<i class="mdi mdi-flask-outline"></i></a>'
            "<a href=\"{% url 'plugins:netbox_interface_name_rules:interfacenamerule_duplicate' record.pk %}\""
            ' class="btn btn-sm btn-success" title="Duplicate" aria-label="Duplicate">'
            '<i class="mdi mdi-content-copy"></i></a>'
        ),
    )

    class Meta:
        model = InterfaceNameRule
        fields = (
            "pk",
            "id",
            "enabled",
            "specificity_score",
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
            "actions",
        )
        default_columns = (
            "pk",
            "enabled",
            "specificity_score",
            "module_type",
            "module_type_pattern",
            "module_type_is_regex",
            "parent_module_type",
            "device_type",
            "platform",
            "name_template",
            "channel_count",
            "description",
            "actions",
        )
        row_attrs = {"class": lambda record: "" if record.enabled else "text-muted opacity-50"}
        attrs = {"class": "table table-hover table-headings table-striped"}
