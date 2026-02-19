import django_filters

from .models import InterfaceNameRule


class InterfaceNameRuleFilterSet(django_filters.FilterSet):
    class Meta:
        model = InterfaceNameRule
        fields = ["module_type", "parent_module_type", "device_type"]
