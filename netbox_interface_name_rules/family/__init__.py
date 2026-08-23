# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and execute interface-family operations."""

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
    PlannedMember,
)
from .execution import execute_installed_plan_set
from .installed import plan_installed_families

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
    "PlannedMember",
    "execute_installed_plan_set",
    "plan_installed_families",
)
