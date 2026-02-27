# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.viewsets import NetBoxModelViewSet

from netbox_interface_name_rules.models import InterfaceNameRule

from .serializers import InterfaceNameRuleSerializer


class InterfaceNameRuleViewSet(NetBoxModelViewSet):
    """REST API viewset for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    serializer_class = InterfaceNameRuleSerializer
