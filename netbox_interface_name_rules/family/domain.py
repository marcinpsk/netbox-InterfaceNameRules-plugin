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
    module_id: int | None
    members: tuple[PlannedMember, ...]
    parent_pk: int | None = None
    precondition_status: FamilyStatus | None = None
    precondition_reason: str = ""

    @property
    def member_pks(self) -> tuple[int, ...]:
        """Return member primary keys in plan order."""
        return tuple(member.snapshot.pk for member in self.members)

    @property
    def live_members(self) -> tuple[PlannedMember, ...]:
        """Return the planned members that already have live interface rows."""
        return self.members


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
class PlannedChannel:
    """One channel row a structural plan creates under its parent."""

    channel_id: int
    name: str


@dataclass(frozen=True, slots=True)
class StructuralFamilyPlan:
    """An executable plan that turns one plain interface into a channelized family."""

    family_id: str
    device_id: int
    module_id: int
    module_type_id: int
    base: InterfaceSnapshot
    parent_target_name: str
    channel_count: int
    channels: tuple[PlannedChannel, ...]
    precondition_status: FamilyStatus | None = None
    precondition_reason: str = ""

    @property
    def topology(self) -> FamilyTopology:
        """Return the topology this plan installs."""
        return FamilyTopology.CHANNELIZED

    @property
    def target_names(self) -> tuple[str, ...]:
        """Return the parent name and every channel name in creation order."""
        return (self.parent_target_name, *(channel.name for channel in self.channels))

    @property
    def live_members(self) -> tuple[PlannedMember, ...]:
        """Return the base row this plan rewrites."""
        return (PlannedMember(self.base, self.parent_target_name, MemberRole.PARENT),)


@dataclass(frozen=True, slots=True)
class FlatCreationPlan:
    """An executable plan that expands one plain interface into a flat breakout family."""

    family_id: str
    device_id: int
    module_id: int
    base: InterfaceSnapshot
    target_names: tuple[str, ...]
    precondition_status: FamilyStatus | None = None
    precondition_reason: str = ""

    @property
    def topology(self) -> FamilyTopology:
        """Return the topology this plan installs."""
        return FamilyTopology.FLAT

    @property
    def live_members(self) -> tuple[PlannedMember, ...]:
        """Return the base row this plan rewrites."""
        return (PlannedMember(self.base, self.target_names[0], MemberRole.FLAT_MEMBER),)


@dataclass(frozen=True, slots=True)
class ProspectiveMember:
    """One member of a family planned from names alone."""

    source_name: str | None
    target_name: str
    role: MemberRole
    channel_id: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ProspectiveFamilyPlan:
    """What a rule intends for one family of named interfaces.

    A plan whose *base_name* is set describes a family the rule builds out of that one name, so the
    name expands to every target.  A plan without one renames members that already exist.
    """

    family_id: str
    topology: FamilyTopology
    base_name: str | None
    members: tuple[ProspectiveMember, ...]
    precondition_status: FamilyStatus | None = None
    precondition_reason: str = ""

    @property
    def source_names(self) -> tuple[str, ...]:
        """Return the name of every member that already exists, in plan order."""
        return tuple(member.source_name for member in self.members if member.source_name is not None)

    @property
    def target_names(self) -> tuple[str, ...]:
        """Return every intended name, in plan order."""
        return tuple(member.target_name for member in self.members)


@dataclass(frozen=True, slots=True)
class ProspectiveFamilyPlanSet:
    """Exactly one prospective plan for each family the rule intends on a module."""

    module_id: int
    plans: tuple[ProspectiveFamilyPlan, ...]

    def predicted_names(self, source_name: str) -> tuple[str, ...]:
        """Return the names *source_name* becomes, or the name itself when no plan claims it."""
        for plan in self.plans:
            if plan.base_name == source_name:
                return plan.target_names
            for member in plan.members:
                if member.source_name == source_name:
                    return (member.target_name,)
        return (source_name,)


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


@dataclass(frozen=True, slots=True)
class ConversionMember:  # pragma: no cover - requires channelization support
    """One installed flat sibling and the channel identifier it takes."""

    snapshot: InterfaceSnapshot
    channel_id: int


@dataclass(frozen=True, slots=True)
class ConversionPlan:  # pragma: no cover - requires channelization support
    """An executable plan that turns one installed flat family into a channelized family.

    The ch-0 row keeps its primary key and becomes the parent, giving up its name to a new
    channel-1 row; each sibling is retyped in place as a channel under the name it already has.

    *channel_names* is every name the rule spells for the family, whether or not the module still
    carries a row for it, so a family with a gap can still say what it would have become.
    """

    family_id: str
    device_id: int
    module_id: int
    base: InterfaceSnapshot
    parent_target_name: str
    channel_names: tuple[str, ...]
    siblings: tuple[ConversionMember, ...]
    precondition_status: FamilyStatus | None = None
    precondition_reason: str = ""

    @property
    def topology(self) -> FamilyTopology:
        """Return the topology this plan installs."""
        return FamilyTopology.CHANNELIZED

    @property
    def channel_count(self) -> int:
        """Return how many channels the converted parent declares."""
        return len(self.channel_names)

    @property
    def member_pks(self) -> tuple[int, ...]:
        """Return the primary key of every row the plan rewrites, in family order."""
        return (self.base.pk, *(sibling.snapshot.pk for sibling in self.siblings))

    @property
    def current_names(self) -> tuple[str, ...]:
        """Return the names the family carries now, in family order."""
        return (self.base.name, *(sibling.snapshot.name for sibling in self.siblings))

    @property
    def sibling_target_names(self) -> tuple[str, ...]:
        """Return the name each installed sibling keeps as a channel, in family order."""
        return tuple(sibling.snapshot.name for sibling in self.siblings)

    @property
    def missing_names(self) -> tuple[str, ...]:
        """Return every name the family needs that this module carries no row for."""
        present = {self.base.name, *(sibling.snapshot.name for sibling in self.siblings)}
        return tuple(name for name in self.channel_names if name not in present)

    @property
    def target_names(self) -> tuple[str, ...]:
        """Return the parent name and every channel name, in family order."""
        return (self.parent_target_name, *self.channel_names)


@dataclass(frozen=True, slots=True)
class PlannedName:  # pragma: no cover - requires channelization support
    """One name a family plan intends, and the role it fills in that family."""

    name: str
    role: str
    channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class ConversionCandidate:  # pragma: no cover - requires channelization support
    """One flat family an operator may convert, and what converting it would do.

    *module* and *interface* are the live rows the Apply page links to and submits; every name,
    role and reason on the candidate comes from the immutable plan instead.
    """

    plan: ConversionPlan
    module: object
    interface: object
    reason: str = ""

    @property
    def convertible(self) -> bool:
        """Return whether a dry run of this family succeeded."""
        return not self.reason

    @property
    def status_label(self) -> str:
        """Return the operator-facing result of the conversion preview."""
        if self.convertible:
            return "Convertible"
        if self.plan.precondition_status == FamilyStatus.UNSUPPORTED:
            return "Unsupported"
        return "Blocked"

    @property
    def current_name(self) -> str:
        """Return the name of the ch-0 row the confirm form submits."""
        return self.plan.base.name

    @property
    def current_names(self) -> tuple[str, ...]:
        """Return the names the family carries now."""
        return self.plan.current_names

    @property
    def new_names(self) -> tuple[str, ...]:
        """Return the names the family would carry once converted."""
        return self.plan.target_names

    @property
    def name_details(self) -> tuple[PlannedName, ...]:
        """Describe every intended name so the page can render a family as a family."""
        return (
            PlannedName(self.plan.parent_target_name, "parent"),
            *(
                PlannedName(name, "channel", channel_id)
                for channel_id, name in enumerate(self.plan.channel_names, start=1)
            ),
        )

    @property
    def metadata_note(self) -> str:
        """Return the sentence the Apply page shows about where the ch-0 configuration ends up."""
        return (
            f"The interface VRF, addresses, VLANs, MTU, description and tags on {self.plan.base.name} move to the new "
            f"channel 1 interface that takes over that name; custom field values are copied. The physical "
            f"row keeps its ID and becomes the parent {self.plan.parent_target_name}, so automation keyed "
            f"on that interface ID will address the parent afterwards."
        )


@dataclass(frozen=True, slots=True)
class ConversionPreview:
    """Every flat family a conversion scan examined, and whether it left any unexamined."""

    candidates: tuple[ConversionCandidate, ...]
    has_more: bool = False
