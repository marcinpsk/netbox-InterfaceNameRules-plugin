# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_filters

from .models import InterfaceNameRule


class InterfaceNameRuleFilterSet(django_filters.FilterSet):
    class Meta:
        model = InterfaceNameRule
        fields = ["module_type", "parent_module_type", "device_type"]
