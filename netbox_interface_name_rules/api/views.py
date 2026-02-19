from netbox.api.viewsets import NetBoxModelViewSet

from netbox_interface_name_rules.models import InterfaceNameRule

from .serializers import InterfaceNameRuleSerializer


class InterfaceNameRuleViewSet(NetBoxModelViewSet):
    queryset = InterfaceNameRule.objects.all()
    serializer_class = InterfaceNameRuleSerializer
