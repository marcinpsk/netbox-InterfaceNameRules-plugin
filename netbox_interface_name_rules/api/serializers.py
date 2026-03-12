# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from netbox_interface_name_rules.models import InterfaceNameRule


class InterfaceNameRuleSerializer(NetBoxModelSerializer):
    """Serializer for InterfaceNameRule with mutual-exclusivity validation."""

    class Meta:
        model = InterfaceNameRule
        fields = [
            "id",
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

    def validate(self, attrs):
        """Ensure module_type XOR module_type_pattern depending on regex mode."""
        attrs = super().validate(attrs)
        instance = self.instance

        # For PATCH requests, fall back to existing instance values for fields not in attrs
        is_device_level = attrs.get(
            "applies_to_device_interfaces", getattr(instance, "applies_to_device_interfaces", False)
        )
        is_regex = attrs.get("module_type_is_regex", getattr(instance, "module_type_is_regex", False))
        module_type = attrs.get("module_type", getattr(instance, "module_type", None))
        pattern = attrs.get("module_type_pattern", getattr(instance, "module_type_pattern", ""))

        if is_device_level:
            # Device-level rules must not have a module_type FK
            if module_type:  # pragma: no cover  # model.clean() runs first
                raise serializers.ValidationError(
                    {"module_type": "Module type must be empty for device-level interface rules."}
                )
        elif is_regex:
            if not pattern:  # pragma: no cover  # model.clean() runs first
                raise serializers.ValidationError(
                    {"module_type_pattern": "Regex pattern is required when regex mode is enabled."}
                )
            if module_type:  # pragma: no cover  # model.clean() runs first
                raise serializers.ValidationError(
                    {"module_type": "Cannot set both module_type and module_type_pattern. Choose one."}
                )
        else:
            if not module_type:  # pragma: no cover  # model.clean() runs first
                raise serializers.ValidationError(
                    {"module_type": "module_type is required when regex mode is disabled."}
                )
            if pattern:  # pragma: no cover  # model.clean() runs first
                raise serializers.ValidationError(
                    {"module_type_pattern": "Cannot set module_type_pattern when regex mode is disabled."}
                )
        return attrs
