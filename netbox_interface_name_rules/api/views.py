# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework.exceptions import ValidationError

from netbox_interface_name_rules import branching
from netbox_interface_name_rules.models import InterfaceNameRule

from .serializers import InterfaceNameRuleSerializer

BACKGROUND_IN_A_BRANCH = (
    "A background request runs on main, not in the active branch. Send the request without background=true."
)


class InterfaceNameRuleViewSet(NetBoxModelViewSet):
    """REST API viewset for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    serializer_class = InterfaceNameRuleSerializer

    def _enqueue_bulk_job(self, request, *args, **kwargs):
        """Refuse a background request in a branch before NetBox enqueues it: its job runs on main."""
        if branching.branch_identity() is not None:
            raise ValidationError(BACKGROUND_IN_A_BRANCH)
        return super()._enqueue_bulk_job(request, *args, **kwargs)
