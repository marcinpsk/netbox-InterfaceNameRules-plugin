# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and execute interface-family operations."""

from .capabilities import supports_channelization
from .domain import (
    FamilyOutcome,
    FamilyStatus,
    FamilyTopology,
    InstalledFamilyPlan,
    InstalledFamilyPlanSet,
    InstalledPlanSetOutcome,
    InterfaceSnapshot,
    MemberOutcome,
    MemberRole,
    PlannedChannel,
    PlannedMember,
    StructuralFamilyPlan,
)
from .execution import execute_installed_plan_set
from .installed import module_db_alias, plan_installed_families
from .structural import (
    channelized_family_names,
    execute_structural_family,
    has_flat_expansion,
    install_channelized_family,
    plan_structural_family,
)

__all__ = (
    "FamilyOutcome",
    "FamilyStatus",
    "FamilyTopology",
    "InstalledFamilyPlan",
    "InstalledFamilyPlanSet",
    "InstalledPlanSetOutcome",
    "InterfaceSnapshot",
    "MemberOutcome",
    "MemberRole",
    "PlannedChannel",
    "PlannedMember",
    "StructuralFamilyPlan",
    "channelized_family_names",
    "execute_installed_plan_set",
    "execute_structural_family",
    "has_flat_expansion",
    "install_channelized_family",
    "module_db_alias",
    "plan_installed_families",
    "plan_structural_family",
    "supports_channelization",
)
