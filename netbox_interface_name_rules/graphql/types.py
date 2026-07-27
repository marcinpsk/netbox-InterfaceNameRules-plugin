# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL object type for InterfaceNameRule."""

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import NetBoxObjectType

from netbox_interface_name_rules.models import InterfaceNameRule

from .filters import InterfaceNameRuleFilter

if TYPE_CHECKING:
    from dcim.graphql.types import DeviceTypeType, ModuleTypeType, PlatformType

# The filter base only exists on NetBox 4.5+; register without one where it doesn't.
_type_kwargs = {"fields": "__all__", "pagination": True}
if InterfaceNameRuleFilter is not None:  # pragma: no branch — always set except on NetBox 4.3
    _type_kwargs["filters"] = InterfaceNameRuleFilter


@strawberry_django.type(InterfaceNameRule, **_type_kwargs)
class InterfaceNameRuleType(NetBoxObjectType):
    """An interface rename rule."""

    # Without these the relations resolve to a bare DjangoModelType with no queryable fields.
    module_type: Annotated["ModuleTypeType", strawberry.lazy("dcim.graphql.types")] | None
    parent_module_type: Annotated["ModuleTypeType", strawberry.lazy("dcim.graphql.types")] | None
    device_type: Annotated["DeviceTypeType", strawberry.lazy("dcim.graphql.types")] | None
    platform: Annotated["PlatformType", strawberry.lazy("dcim.graphql.types")] | None


__all__ = ("InterfaceNameRuleType",)
