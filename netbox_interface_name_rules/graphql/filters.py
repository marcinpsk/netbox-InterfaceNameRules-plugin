# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL filter input for InterfaceNameRule.

NetBox grew a shared GraphQL filter base in 4.5; on 4.3 the filter is simply absent and
the type is registered without one, so the plugin still exposes queries there.
"""

import strawberry
import strawberry_django

from netbox_interface_name_rules.models import InterfaceNameRule

_FILTER_BASE_MODULE = "netbox.graphql.filters"

try:
    from netbox.graphql.filters import NetBoxModelFilter
except ModuleNotFoundError as exc:  # pragma: no cover — only taken on NetBox 4.3
    # Degrade only when the module is genuinely absent (or one of its parents). Any other
    # ImportError is a real breakage and must not masquerade as "old NetBox".
    _missing = exc.name or ""
    if _missing != _FILTER_BASE_MODULE and not _FILTER_BASE_MODULE.startswith(f"{_missing}."):
        raise
    InterfaceNameRuleFilter = None
else:  # pragma: no cover — field declarations; behaviour is covered by the GraphQL tests
    from strawberry_django import BaseFilterLookup

    try:
        from strawberry_django import StrFilterLookup
    except ImportError:
        # strawberry-graphql-django < 0.86, which is what NetBox 4.5 pins.
        from strawberry_django import FilterLookup as StrFilterLookup

    @strawberry_django.filter_type(InterfaceNameRule, lookups=True)
    class InterfaceNameRuleFilter(NetBoxModelFilter):
        """Filter input mirroring the REST filterset's fields."""

        module_type_id: strawberry.ID | None = strawberry_django.filter_field()
        module_type_pattern: StrFilterLookup[str] | None = strawberry_django.filter_field()
        module_type_is_regex: BaseFilterLookup[bool] | None = strawberry_django.filter_field()
        parent_module_type_id: strawberry.ID | None = strawberry_django.filter_field()
        device_type_id: strawberry.ID | None = strawberry_django.filter_field()
        platform_id: strawberry.ID | None = strawberry_django.filter_field()
        name_template: StrFilterLookup[str] | None = strawberry_django.filter_field()
        channel_count: BaseFilterLookup[int] | None = strawberry_django.filter_field()
        channel_start: BaseFilterLookup[int] | None = strawberry_django.filter_field()
        description: StrFilterLookup[str] | None = strawberry_django.filter_field()
        enabled: BaseFilterLookup[bool] | None = strawberry_django.filter_field()
        applies_to_device_interfaces: BaseFilterLookup[bool] | None = strawberry_django.filter_field()


__all__ = ("InterfaceNameRuleFilter",)
