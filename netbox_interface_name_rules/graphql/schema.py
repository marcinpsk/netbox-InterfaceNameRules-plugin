# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

"""GraphQL query registered into NetBox's schema via PluginConfig.graphql_schema."""

import strawberry
import strawberry_django

from .types import InterfaceNameRuleType


@strawberry.type(name="Query")
class InterfaceNameRulesQuery:
    """Adds interface_name_rule and interface_name_rule_list to NetBox's Query."""

    interface_name_rule: InterfaceNameRuleType = strawberry_django.field()
    interface_name_rule_list: list[InterfaceNameRuleType] = strawberry_django.field()


schema = [InterfaceNameRulesQuery]

__all__ = ("InterfaceNameRulesQuery", "schema")
