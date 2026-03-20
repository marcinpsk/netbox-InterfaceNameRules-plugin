# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_filters
from dcim.models import DeviceType, ModuleType, Platform
from django.db.models import Q
from netbox.filtersets import NetBoxModelFilterSet

from .models import InterfaceNameRule


class InterfaceNameRuleFilterSet(NetBoxModelFilterSet):
    """FilterSet for InterfaceNameRule list/API filtering."""

    q = django_filters.CharFilter(method="search", label="Search")

    module_type_id = django_filters.ModelChoiceFilter(
        queryset=ModuleType.objects.all(),
        field_name="module_type",
        label="Module Type",
    )
    module_type_is_regex = django_filters.BooleanFilter(label="Regex Mode")
    enabled = django_filters.BooleanFilter(label="Enabled")
    applies_to_device_interfaces = django_filters.BooleanFilter(label="Device Interface Rules")
    module_type_pattern = django_filters.CharFilter(lookup_expr="icontains", label="Pattern")
    parent_module_type_id = django_filters.ModelChoiceFilter(
        queryset=ModuleType.objects.all(),
        field_name="parent_module_type",
        label="Parent Module Type",
    )
    device_type_id = django_filters.ModelChoiceFilter(
        queryset=DeviceType.objects.all(),
        field_name="device_type",
        label="Device Type",
    )
    platform_id = django_filters.ModelChoiceFilter(
        queryset=Platform.objects.all(),
        field_name="platform",
        label="Platform",
    )

    class Meta:
        model = InterfaceNameRule
        fields = [
            "module_type_id",
            "module_type_is_regex",
            "applies_to_device_interfaces",
            "module_type_pattern",
            "parent_module_type_id",
            "device_type_id",
            "platform_id",
            "enabled",
        ]

    def search(self, queryset, name, value):
        """Filter by pattern, template, description, or module type model name."""
        return queryset.select_related("module_type").filter(
            Q(module_type_pattern__icontains=value)
            | Q(name_template__icontains=value)
            | Q(description__icontains=value)
            | Q(module_type__model__icontains=value)
        )
