# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Immutable domain values for interface-family operations."""

from dataclasses import dataclass
from enum import StrEnum


class FamilyTopology(StrEnum):
    """Installed interface-family topology."""

    FLAT = "flat"
    CHANNELIZED = "channelized"


class FamilyStatus(StrEnum):
    """Result of a family or member operation."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"
    BLOCKED = "blocked"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class MemberRole(StrEnum):
    """A member's role in its installed family."""

    PARENT = "parent"
    CHANNEL = "channel"
    FLAT_MEMBER = "flat_member"


@dataclass(frozen=True, slots=True)
class InterfaceSnapshot:
    """Live interface facts that execution must revalidate."""

    pk: int
    device_id: int
    module_id: int | None
    name: str
    parent_id: int | None
    channel_id: int | None
    channels: int | None

    @classmethod
    def from_interface(cls, interface):
        """Capture the protected facts from a NetBox interface row."""
        return cls(
            pk=interface.pk,
            device_id=interface.device_id,
            module_id=interface.module_id,
            name=interface.name,
            parent_id=getattr(interface, "parent_id", None),
            channel_id=getattr(interface, "channel_id", None),
            channels=getattr(interface, "channels", None),
        )


@dataclass(frozen=True, slots=True)
class PlannedMember:
    """One installed member and its intended name."""

    snapshot: InterfaceSnapshot
    target_name: str | None
    role: MemberRole
    reason: str = ""


@dataclass(frozen=True, slots=True)
class InstalledFamilyPlan:
    """An executable rename plan for one installed interface family."""

    family_id: str
    topology: FamilyTopology
    device_id: int
    module_id: int
    db_alias: str
    members: tuple[PlannedMember, ...]
    parent_pk: int | None = None
    blocked_reason: str = ""

    @property
    def member_pks(self) -> tuple[int, ...]:
        """Return member primary keys in plan order."""
        return tuple(member.snapshot.pk for member in self.members)


@dataclass(frozen=True, slots=True)
class InstalledFamilyPlanSet:
    """Exactly one immutable plan for each discovered installed family."""

    module_id: int
    plans: tuple[InstalledFamilyPlan, ...]

    @property
    def member_pks(self) -> frozenset[int]:
        """Return every interface claimed by the plan set."""
        return frozenset(pk for plan in self.plans for pk in plan.member_pks)


@dataclass(frozen=True, slots=True)
class MemberOutcome:
    """Result facts for one planned family member."""

    interface_pk: int
    current_name: str
    target_name: str | None
    status: FamilyStatus
    reason: str = ""


@dataclass(frozen=True, slots=True)
class FamilyOutcome:
    """Execution result for one installed family."""

    family_id: str
    topology: FamilyTopology
    status: FamilyStatus
    members: tuple[MemberOutcome, ...]
    reason: str = ""

    @property
    def changed_count(self) -> int:
        """Return the number of members renamed by this operation."""
        return sum(member.status == FamilyStatus.CHANGED for member in self.members)


@dataclass(frozen=True, slots=True)
class InstalledPlanSetOutcome:
    """Execution results for an installed family plan set."""

    families: tuple[FamilyOutcome, ...]

    @property
    def changed_count(self) -> int:
        """Return the number of members renamed across all families."""
        return sum(family.changed_count for family in self.families)
