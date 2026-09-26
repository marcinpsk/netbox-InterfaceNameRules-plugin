# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.serializers import NetBoxModelSerializer

from netbox_interface_name_rules.models import InterfaceNameRule


class InterfaceNameRuleSerializer(NetBoxModelSerializer):
    """Serializer for InterfaceNameRule; the model validates and normalises the mode fields."""

    class Meta:
        model = InterfaceNameRule
        fields = [
            "id",
            "url",
            "display",
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
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        ]
        brief_fields = ["id", "url", "display", "name_template", "description"]
