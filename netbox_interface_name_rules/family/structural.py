# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and execute the creation of interface families."""

import logging
from dataclasses import replace

from dcim.choices import InterfaceTypeChoices
from dcim.models import Interface, InterfaceTemplate
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from ..transactions import atomic_with_events
from .capabilities import supports_channelization
from .domain import (
    FamilyOutcome,
    FamilyStatus,
    FamilyTopology,
    FlatCreationPlan,
    InterfaceSnapshot,
    MemberOutcome,
    PlannedChannel,
    StructuralFamilyPlan,
)
from .names import (
    COLLISION_REASON,
    belongs_elsewhere,
    channelized_name_refusal,
    first_taken_name,
    is_name_collision,
    name_holders,
    name_owners,
    reconcile_after_parent_cascade,
    repeated_name_refusal,
    repeated_names,
    validate_or_block,
)
from .targets import UNCLAIMED_BASE_REASON, channelized_family_names, flat_family_names

logger = logging.getLogger(__name__)

UNSUPPORTED_REASON = "this NetBox release cannot model channelized interfaces"
STALE_REASON = "the base interface changed after planning"
MODULE_CHANGED_REASON = "the module's interfaces changed after planning"
HOLDER_CHANGED_REASON = "an interface with one of the channel names changed after planning"


def _flat_expansion(module_type_id, module_id, channel_names=()) -> bool:  # pragma: no cover - channelization only
    """Return whether the module carries more plain interfaces outside *channel_names* than its templates describe."""
    templates = InterfaceTemplate.objects.filter(module_type_id=module_type_id).count()
    plain = Interface.objects.filter(module_id=module_id, channel_id__isnull=True).exclude(name__in=channel_names)
    return plain.count() > templates


def has_flat_expansion(module, channel_names=()) -> bool:  # pragma: no cover - requires channelization support
    """Return whether *module* carries more plain interfaces than its module type's templates describe.

    A flat breakout leaves N-1 rows beyond the templates, so the surplus is the structural mark of a
    family an earlier apply installed.  A channel belongs to the parent that declares it, so it is
    never part of that surplus: counting one would stop a second port from gaining its own family.
    Counting templates rather than their resolved names keeps two templates that resolve to the same
    string from reading as one. A row with one of *channel_names* is a row the family takes over
    (ADR 0017), so it is not part of the surplus either.
    """
    return _flat_expansion(module.module_type_id, module.pk, channel_names)


def taken_over_names(base_name, channel_names) -> tuple[str, ...]:
    """Return the channel names whose rows a family on *base_name* takes over instead of stranding.

    A flat family gives its first row one of its channel names, so the siblings of such a base stay siblings.
    """
    return () if base_name in channel_names else tuple(channel_names)


def flat_surplus(interfaces, templates) -> int:
    """Return how many more rows outside a channel *interfaces* hold than *templates* has, as ``has_flat_expansion``."""
    return sum(1 for interface in interfaces if getattr(interface, "channel_id", None) is None) - len(templates)


def _plan(module, base, parent_target_name, channels, status=None, reason="", holders=None):
    """Build one immutable structural plan for *base*; *holders* snapshots the rows with a channel name, by name."""
    holders = holders or {}
    channel_names = [name for _channel_id, name in channels]
    return StructuralFamilyPlan(
        family_id=f"structural:{base.pk}",
        device_id=module.device_id,
        module_id=module.pk,
        module_type_id=module.module_type_id,
        base=InterfaceSnapshot.from_interface(base),
        parent_target_name=parent_target_name,
        channel_count=len(channels),
        channels=tuple(
            PlannedChannel(channel_id=channel_id, name=name, holder=holders.get(name)) for channel_id, name in channels
        ),
        precondition_status=status,
        precondition_reason=reason,
        taken_over_names=taken_over_names(base.name, channel_names),
    )


def _modelled_plan(module, rule, variables, base, base_name, family_bases):  # pragma: no cover - channelization only
    """Return the plan for a NetBox release that can hold the family."""
    if base_name is None:
        return _plan(module, base, base.name, (), FamilyStatus.BLOCKED, UNCLAIMED_BASE_REASON)
    try:
        parent_target_name, channels = channelized_family_names(rule, base.name, base_name, variables)
    except (TypeError, ValueError) as error:
        reason = f"failed to evaluate the family names: {error}"
        return _plan(module, base, base.name, (), FamilyStatus.FAILED, reason)
    channel_names = [name for _channel_id, name in channels]
    rows = {name: row for name, row in name_holders(module.device_id, channel_names).items() if row.pk != base.pk}
    # A row of another module or family is reported by its own outcome, so this plan does not hold it.
    holders = {
        name: InterfaceSnapshot.from_interface(row)
        for name, row in rows.items()
        if not belongs_elsewhere(row, module.pk, family_bases)
    }
    refusal = channelized_name_refusal(parent_target_name, channel_names, rows, (base.pk,), module.pk, family_bases)
    if refusal is not None:
        return _plan(module, base, parent_target_name, channels, FamilyStatus.BLOCKED, refusal[1], holders)
    return _plan(module, base, parent_target_name, channels, holders=holders)


def plan_structural_family(module, rule, variables, base, base_name, family_bases=frozenset()) -> StructuralFamilyPlan:
    """Return the plan for the channelized family *rule* builds on plain interface *base*.

    *base_name* is the value of ``{base}``, or None when no template claims *base*. *family_bases*
    are the primary keys of the module's rows that the claim gives a template, which no family takes
    over. ``settle_module_families`` then settles the families on the module against each other.
    """
    if not supports_channelization():
        return _plan(module, base, base.name, (), FamilyStatus.UNSUPPORTED, UNSUPPORTED_REASON)
    return _modelled_plan(module, rule, variables, base, base_name, family_bases)  # pragma: no cover - see above


def module_taken_over_names(families, bases) -> frozenset:
    """Return the names of the rows that the channelized families planned on one module take over.

    *families* pairs the base name of each family whose names were evaluated with its channel names.
    A row the claim gives a template is no family's to take over, so it is not one of them.
    """
    return frozenset(
        name
        for base_name, channel_names in families
        for name in taken_over_names(base_name, channel_names)
        if not bases.claims_alone(name)
    )


def _without_holders(plan, names=None):
    """Return *plan* holding no row with one of *names*, or no row at all when *names* is None."""
    channels = tuple(
        replace(channel, holder=None) if names is None or channel.name in names else channel
        for channel in plan.channels
    )
    return replace(plan, channels=channels)


def _settled(plan, repeated, names, expanded, module):  # pragma: no cover - requires channelization support
    """Return *plan* as the module's families settle it; see ``settle_module_families``."""
    refusal = repeated_name_refusal(plan.channel_names, repeated)
    if refusal is not None:
        plan = _without_holders(plan, repeated)
        return replace(plan, precondition_status=FamilyStatus.BLOCKED, precondition_reason=refusal[1])
    if plan.precondition_status is not None:
        return plan
    if expanded:
        # Converting one sibling into a parent would strand the others beside the new family.
        reason = f"module {module} already carries a flat breakout family"
        return replace(_without_holders(plan), precondition_status=FamilyStatus.BLOCKED, precondition_reason=reason)
    return replace(plan, taken_over_names=tuple(sorted(names)))


def settle_module_families(module, plans, interfaces, templates, bases):  # pragma: no cover - channelization only
    """Return *plans* with the structural families on *module* settled against each other before execution.

    A channel name that the families want more than once refuses every family that wants it, and no
    family takes over its row. A parent name is left to NetBox's unique name, as before: no family
    takes a row over by its parent name. Then, when *module* carries a flat breakout family, every other family is
    refused. *interfaces* are the module's rows and *templates* its resolved templates. The rows
    that the module's families take over are not flat siblings (ADR 0017); any other row beyond the
    templates is. Each family that is built keeps the names of the rows that the module's families
    take over, for the check under lock.
    """
    evaluated = [plan for plan in plans if isinstance(plan, StructuralFamilyPlan) and plan.channels]
    repeated = repeated_names(plan.channel_names for plan in evaluated)
    names = module_taken_over_names(((plan.base.name, plan.channel_names) for plan in evaluated), bases)
    expanded = flat_surplus([interface for interface in interfaces if interface.name not in names], templates) > 0
    settled = {plan.base.pk: _settled(plan, repeated, names, expanded, module) for plan in evaluated}
    return [settled.get(plan.base.pk, plan) if isinstance(plan, StructuralFamilyPlan) else plan for plan in plans]


def _outcome(plan, status, members, reason=""):
    """Build the immutable outcome of one structural family operation."""
    return FamilyOutcome(
        family_id=plan.family_id,
        topology=FamilyTopology.CHANNELIZED,
        status=status,
        members=members,
        reason=reason,
    )


def _refused(plan, status, reason, target_name):
    """Log why the family was not built and return an outcome for the base and every holder, which it left as they were."""
    logger.warning("Cannot build a channelized family on interface %r: %s.", plan.base.name, reason)
    members = (
        MemberOutcome(
            interface_pk=plan.base.pk,
            current_name=plan.base.name,
            target_name=target_name,
            status=status,
            reason=reason,
        ),
        *(MemberOutcome(holder.pk, holder.name, holder.name, status, reason) for holder in plan.holders),
    )
    return _outcome(plan, status, members, reason)


def _locked_rows(plan):
    """Lock the base and every planned holder in primary-key order, and return the live rows by primary key."""
    return (
        Interface.objects.select_for_update(of=("self",))
        .select_related("device", "module")
        .filter(pk__in=(plan.base.pk, *(holder.pk for holder in plan.holders)))
        .order_by("pk")
        .in_bulk()
    )


def _stale_reason(plan, rows):  # pragma: no cover - requires channelization support
    """Return why the locked *rows* no longer match *plan*, or an empty string."""
    base = rows.get(plan.base.pk)
    if base is None or InterfaceSnapshot.from_interface(base) != plan.base:
        return STALE_REASON
    if any(
        holder.pk not in rows or InterfaceSnapshot.from_interface(rows[holder.pk]) != holder for holder in plan.holders
    ):
        return HOLDER_CHANGED_REASON
    # A sibling added since planning would be stranded beside the family this plan builds.
    if _flat_expansion(plan.module_type_id, plan.module_id, plan.taken_over_names):
        return MODULE_CHANGED_REASON
    return ""


def _name_refusal(plan):  # pragma: no cover - requires channelization support
    """Return ``(status, reason, name)`` when the family cannot take its names, or None.

    One query, because the caller holds the plan's row locks while this runs.
    """
    repeated = repeated_name_refusal(plan.channel_names, repeated_names([plan.channel_names]))
    if repeated is not None:
        return FamilyStatus.BLOCKED, repeated[1], repeated[0]
    channel_names = plan.channel_names
    holders = name_holders(plan.device_id, plan.target_names)
    refusal = channelized_name_refusal(plan.parent_target_name, channel_names, holders, (plan.base.pk,), plan.module_id)
    if refusal is not None:
        name, reason = refusal
        return FamilyStatus.BLOCKED, reason, name
    # Every row left with a channel name qualifies, so the plan must hold exactly those rows.
    planned = {channel.name: channel.holder.pk for channel in plan.channels if channel.holder is not None}
    live = {name: row.pk for name, row in holders.items() if name in channel_names and row.pk != plan.base.pk}
    if live != planned:
        return FamilyStatus.STALE, HOLDER_CHANGED_REASON, plan.parent_target_name
    return None


def _first_taken_name(plan, own_pks):
    """Return the first planned name an interface outside *own_pks* already owns on the device, or None.

    One query, because the caller holds the plan's row locks while this runs.
    """
    return first_taken_name(plan.target_names, name_owners(plan.device_id, plan.target_names), own_pks)


def _channel_row(channel, parent, rows):  # pragma: no cover - requires channelization support
    """Return the row for *channel* under *parent*: its holder in *rows* bound to the family, else a new row."""
    if channel.holder is None:
        return Interface(
            device=parent.device,
            module=parent.module,
            name=channel.name,
            type=InterfaceTypeChoices.TYPE_CHANNEL,
            parent=parent,
            channel_id=channel.channel_id,
            enabled=parent.enabled,
        )
    row = rows[channel.holder.pk]
    row.snapshot()
    row.module = parent.module
    row.parent = parent
    row.channel_id = channel.channel_id
    row.type = InterfaceTypeChoices.TYPE_CHANNEL
    return row


def _create_channels(plan, parent, rows):  # pragma: no cover - requires channelization support
    """Create or take over every planned channel under *parent* and return their outcomes."""
    members = []
    for channel in plan.channels:
        row = _channel_row(channel, parent, rows)
        validate_or_block(row, "channel")
        row.save()
        members.append(
            MemberOutcome(
                interface_pk=row.pk,
                current_name=channel.name,
                target_name=channel.name,
                status=FamilyStatus.CHANGED,
            )
        )
    return members


def _create_family(plan, rows):  # pragma: no cover - requires channelization support
    """Rewrite the base in *rows* into the family parent, create or take over its channels, and return every outcome."""
    base = rows[plan.base.pk]
    parent_status = FamilyStatus.CHANGED if plan.parent_target_name != base.name else FamilyStatus.UNCHANGED
    base.snapshot()
    base.channels = plan.channel_count
    base.name = plan.parent_target_name
    base.full_clean()
    base.save()
    parent_member = MemberOutcome(
        interface_pk=base.pk,
        current_name=plan.base.name,
        target_name=plan.parent_target_name,
        status=parent_status,
    )
    channel_members = _create_channels(plan, base, rows)
    reconcile_after_parent_cascade(
        plan.base.name,
        plan.parent_target_name,
        tuple(
            (member.interface_pk, channel.channel_id, channel.name)
            for member, channel in zip(channel_members, plan.channels, strict=True)
        ),
    )
    return (parent_member, *channel_members)


def _install_family(plan):  # pragma: no cover - requires channelization support
    """Create the whole family in one transaction, or write nothing at all."""
    try:
        with atomic_with_events():
            rows = _locked_rows(plan)
            stale = _stale_reason(plan, rows)
            if stale:
                return _refused(plan, FamilyStatus.STALE, stale, plan.parent_target_name)
            refusal = _name_refusal(plan)
            if refusal is not None:
                return _refused(plan, *refusal)
            members = _create_family(plan, rows)
    except ValidationError as error:
        return _refused(plan, FamilyStatus.BLOCKED, " ".join(error.messages), plan.parent_target_name)
    except IntegrityError as error:
        if not is_name_collision(error):
            raise
        return _refused(plan, FamilyStatus.BLOCKED, COLLISION_REASON, plan.parent_target_name)
    return _outcome(plan, FamilyStatus.CHANGED, members)


def execute_structural_family(plan: StructuralFamilyPlan) -> FamilyOutcome:
    """Create the planned channelized family, or leave every row exactly as it was.

    Only a structural plan names the base row to rewrite, so anything else (a prospective plan
    above all) is refused before a single row is locked.
    """
    if not isinstance(plan, StructuralFamilyPlan):
        raise TypeError(f"{type(plan).__name__} is not an executable family plan")
    if plan.precondition_status is not None:
        return _refused(plan, plan.precondition_status, plan.precondition_reason, plan.parent_target_name)
    return _install_family(plan)  # pragma: no cover - requires channelization support


# ---------------------------------------------------------------------------
# Flat breakout families
# ---------------------------------------------------------------------------
# A flat family is N sibling interfaces on one module, built whole or not at all (ADR 0001).


def _flat_creation_plan(module, base, target_names, status=None, reason="", members=()):
    """Build one immutable flat-creation plan for *base*, with the rows *members* it keeps."""
    return FlatCreationPlan(
        family_id=f"flat:{base.pk}",
        device_id=module.device_id,
        module_id=module.pk,
        base=InterfaceSnapshot.from_interface(base),
        target_names=target_names,
        precondition_status=status,
        precondition_reason=reason,
        members=tuple(InterfaceSnapshot.from_interface(member) for member in members),
    )


def plan_flat_family(module, rule, variables, base, base_name, members=()) -> FlatCreationPlan:
    """Return the plan for the flat breakout family *rule* builds on plain interface *base*.

    *base_name* is the value of ``{base}``, or None when no template claims *base*. *members* are
    the rows of a half-built family that the claim gave the same template; the family keeps them.
    """
    if base_name is None:
        return _flat_creation_plan(module, base, (base.name,), FamilyStatus.BLOCKED, UNCLAIMED_BASE_REASON)
    try:
        target_names = flat_family_names(rule, variables, base_name)
    except (TypeError, ValueError) as error:
        reason = f"failed to evaluate the family names: {error}"
        return _flat_creation_plan(module, base, (base.name,), FamilyStatus.FAILED, reason)
    if not target_names:
        reason = "flat family requires at least one target name"
        return _flat_creation_plan(module, base, (base.name,), FamilyStatus.FAILED, reason)
    if any(member.name not in target_names[1:] for member in members):
        raise ValueError(f"a row the flat family on {base.name!r} keeps must carry one of its sibling names")
    return _flat_creation_plan(module, base, target_names, members=members)


def _flat_outcome(plan, status, members, reason=""):
    """Build the immutable outcome of one flat family operation."""
    return FamilyOutcome(
        family_id=plan.family_id,
        topology=FamilyTopology.FLAT,
        status=status,
        members=members,
        reason=reason,
    )


def _flat_refused(plan, status, reason):
    """Log why the family was not built and return an outcome for every planned row, which it left as it was."""
    logger.warning("Cannot build a flat family on interface %r: %s.", plan.base.name, reason)
    members = (
        MemberOutcome(plan.base.pk, plan.base.name, plan.target_names[0], status, reason),
        *(MemberOutcome(member.pk, member.name, member.name, status, reason) for member in plan.members),
    )
    return _flat_outcome(plan, status, members, reason)


def _locked_flat_rows(plan):
    """Lock the base and every planned member in primary-key order, and return the live rows by primary key."""
    return (
        Interface.objects.select_for_update(of=("self",))
        .select_related("device", "module")
        .filter(pk__in=plan.member_pks)
        .order_by("pk")
        .in_bulk()
    )


def _is_flat_stale(plan, rows):
    """Return whether the base or a planned member is gone or changed since planning."""
    planned = {plan.base.pk: plan.base, **{member.pk: member for member in plan.members}}
    return planned != {pk: InterfaceSnapshot.from_interface(row) for pk, row in rows.items()}


def _build_flat_family(plan, base):
    """Name the base, keep each planned member, and create every other sibling."""
    target_name = plan.target_names[0]
    status = FamilyStatus.UNCHANGED
    if target_name != base.name:
        base.snapshot()
        base.name = target_name
        base.full_clean()
        base.save()
        status = FamilyStatus.CHANGED
    outcomes = [MemberOutcome(base.pk, plan.base.name, target_name, status)]
    kept = {member.name: member for member in plan.members}
    for name in plan.target_names[1:]:
        if name in kept:
            outcomes.append(MemberOutcome(kept[name].pk, name, name, FamilyStatus.UNCHANGED))
            continue
        row = Interface(device=base.device, module=base.module, name=name, type=base.type, enabled=base.enabled)
        row.full_clean()
        row.save()
        outcomes.append(MemberOutcome(row.pk, name, name, FamilyStatus.CHANGED))
    return tuple(outcomes)


def _install_flat_family(plan):
    """Build the whole family in one transaction, or write nothing at all."""
    try:
        with atomic_with_events():
            rows = _locked_flat_rows(plan)
            if _is_flat_stale(plan, rows):
                return _flat_refused(plan, FamilyStatus.STALE, STALE_REASON)
            taken = _first_taken_name(plan, plan.member_pks)
            if taken is not None:
                return _flat_refused(plan, FamilyStatus.BLOCKED, f"{COLLISION_REASON}: {taken}")
            members = _build_flat_family(plan, rows[plan.base.pk])
    except ValidationError as error:
        return _flat_refused(plan, FamilyStatus.BLOCKED, " ".join(error.messages))
    except IntegrityError as error:
        if not is_name_collision(error):
            raise
        return _flat_refused(plan, FamilyStatus.BLOCKED, COLLISION_REASON)
    changed = any(member.status == FamilyStatus.CHANGED for member in members)
    return _flat_outcome(plan, FamilyStatus.CHANGED if changed else FamilyStatus.UNCHANGED, members)


def execute_flat_family(plan: FlatCreationPlan) -> FamilyOutcome:
    """Build the planned flat breakout family, or leave every row exactly as it was."""
    if not isinstance(plan, FlatCreationPlan):
        raise TypeError(f"{type(plan).__name__} is not an executable family plan")
    if plan.precondition_status is not None:
        return _flat_refused(plan, plan.precondition_status, plan.precondition_reason)
    return _install_flat_family(plan)
