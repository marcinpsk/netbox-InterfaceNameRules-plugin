# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from .models import InterfaceNameRule


class InterfaceNameRuleTable(NetBoxTable):
    pk = columns.ToggleColumn()
    module_type = tables.Column(verbose_name="Module Type", linkify=True)
    parent_module_type = tables.Column(verbose_name="Parent Module Type", linkify=True)
    device_type = tables.Column(verbose_name="Parent Device Type", linkify=True)
    name_template = tables.Column(verbose_name="Name Template")
    channel_count = tables.Column(verbose_name="Channels")
    channel_start = tables.Column(verbose_name="Ch. Start")
    description = tables.Column(verbose_name="Description", linkify=False)
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta:
        model = InterfaceNameRule
        fields = (
            "pk",
            "id",
            "module_type",
            "parent_module_type",
            "device_type",
            "name_template",
            "channel_count",
            "channel_start",
            "description",
            "actions",
        )
        default_columns = (
            "pk",
            "module_type",
            "parent_module_type",
            "device_type",
            "name_template",
            "channel_count",
            "description",
            "actions",
        )
        attrs = {"class": "table table-hover table-headings table-striped"}
