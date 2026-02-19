from netbox.api.serializers import NetBoxModelSerializer

from netbox_interface_name_rules.models import InterfaceNameRule


class InterfaceNameRuleSerializer(NetBoxModelSerializer):
    class Meta:
        model = InterfaceNameRule
        fields = [
            "id",
            "module_type",
            "parent_module_type",
            "device_type",
            "name_template",
            "channel_count",
            "channel_start",
            "description",
        ]
