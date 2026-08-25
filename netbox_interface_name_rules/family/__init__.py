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
    ProspectiveFamilyPlan,
    ProspectiveFamilyPlanSet,
    ProspectiveMember,
    StructuralFamilyPlan,
)
from .execution import execute_installed_plan_set
from .installed import module_db_alias, plan_installed_families
from .prospective import (
    ProspectiveInterface,
    describe_interfaces,
    describe_module_interfaces,
    describe_template_interfaces,
    plan_prospective_families,
)
from .structural import (
    execute_structural_family,
    has_flat_expansion,
    install_channelized_family,
    plan_structural_family,
)
from .targets import channelized_family_names, template_channel_suffixes
from .template_names import resolved_template_names

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
    "ProspectiveFamilyPlan",
    "ProspectiveFamilyPlanSet",
    "ProspectiveInterface",
    "ProspectiveMember",
    "StructuralFamilyPlan",
    "channelized_family_names",
    "describe_interfaces",
    "describe_module_interfaces",
    "describe_template_interfaces",
    "execute_installed_plan_set",
    "execute_structural_family",
    "has_flat_expansion",
    "install_channelized_family",
    "module_db_alias",
    "plan_installed_families",
    "plan_prospective_families",
    "plan_structural_family",
    "resolved_template_names",
    "supports_channelization",
    "template_channel_suffixes",
)
