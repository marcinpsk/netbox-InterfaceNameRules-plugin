# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Execute installed interface-family plans."""

import logging

from dcim.models import Interface
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q

from .domain import (
    FamilyOutcome,
    FamilyStatus,
    InstalledFamilyPlan,
    InstalledFamilyPlanSet,
    InstalledPlanSetOutcome,
    InterfaceSnapshot,
    MemberOutcome,
    MemberRole,
)

logger = logging.getLogger(__name__)

STALE_REASON = "installed family changed after planning"
COLLISION_REASON = "target name is already in use"
INTERFACE_NAME_CONSTRAINT = "dcim_interface_unique_device_name"
PARENT_BLOCKED_REASON = "family parent was blocked"


def _lock_family(plan: InstalledFamilyPlan):
    """Lock and return the current family rows in stable primary-key order."""
    queryset = Interface.objects.using(plan.db_alias)
    if plan.parent_pk is None:
        queryset = queryset.filter(pk__in=plan.member_pks)
    else:  # pragma: no cover - requires channelization support
        queryset = queryset.filter(Q(pk=plan.parent_pk) | Q(parent_id=plan.parent_pk))
    return list(queryset.select_for_update().order_by("pk"))


def _is_stale(plan: InstalledFamilyPlan, interfaces) -> bool:
    """Return whether live identity, names, membership, or topology changed."""
    planned = {member.snapshot.pk: member.snapshot for member in plan.members}
    live = {interface.pk: InterfaceSnapshot.from_interface(interface) for interface in interfaces}
    return planned != live


def _member_outcome(member, status, reason=""):
    """Build one immutable member outcome from its plan facts."""
    return MemberOutcome(
        interface_pk=member.snapshot.pk,
        current_name=member.snapshot.name,
        target_name=member.target_name,
        status=status,
        reason=reason,
    )


def _name_is_taken(interface, target_name, db_alias):
    """Return whether another interface on the device owns *target_name*."""
    return (
        Interface.objects.using(db_alias)
        .filter(device_id=interface.device_id, name=target_name)
        .exclude(pk=interface.pk)
        .exists()
    )


def _is_name_collision(error: IntegrityError) -> bool:
    """Return whether PostgreSQL identified NetBox's interface-name constraint."""
    cause = error.__cause__
    diagnostics = getattr(cause, "diag", None)
    return getattr(diagnostics, "constraint_name", None) == INTERFACE_NAME_CONSTRAINT


def _blocked_member(member, reason):
    """Return a blocked member outcome."""
    return _member_outcome(member, FamilyStatus.BLOCKED, reason)


def _rename_member(member, interface, db_alias):
    """Apply one planned name inside a savepoint and return its explicit outcome."""
    target_name = member.target_name
    if target_name is None:
        logger.warning(
            "Cannot derive a name for channel interface %r; leaving it unchanged.",
            member.snapshot.name,
        )
        return _blocked_member(member, member.reason)
    if target_name == interface.name:
        return _member_outcome(member, FamilyStatus.UNCHANGED)
    if _name_is_taken(interface, target_name, db_alias):
        logger.warning(
            "Interface name %r already exists on device %s; skipping rename of %r to %r.",
            target_name,
            interface.device_id,
            interface.name,
            target_name,
        )
        return _blocked_member(member, COLLISION_REASON)

    previous_name = interface.name
    try:
        with transaction.atomic(using=db_alias):
            interface.name = target_name
            interface.full_clean()
            interface.save(using=db_alias)
    except ValidationError as error:
        interface.name = previous_name
        logger.warning("NetBox rejected rename of %r to %r: %s", previous_name, target_name, error)
        return _blocked_member(member, " ".join(error.messages))
    except IntegrityError as error:
        interface.name = previous_name
        if _is_name_collision(error):
            logger.warning(
                "Interface name %r became occupied while renaming %r; skipping.",
                target_name,
                previous_name,
            )
            return _blocked_member(member, COLLISION_REASON)
        raise
    return _member_outcome(member, FamilyStatus.CHANGED)


def _family_status(members):
    """Summarize member outcomes without hiding partial success."""
    statuses = {member.status for member in members}
    if FamilyStatus.CHANGED in statuses:
        return FamilyStatus.CHANGED
    if FamilyStatus.BLOCKED in statuses:
        return FamilyStatus.BLOCKED
    return FamilyStatus.UNCHANGED


def _restore_deferred_channel_names(reconciliations, db_alias):  # pragma: no cover - channelization only
    """Restore plugin-owned names that NetBox's parent cascade changed after commit."""
    child_pks = [child_pk for child_pk, _final_name, _cascade_name in reconciliations]
    with transaction.atomic(using=db_alias):
        children = Interface.objects.using(db_alias).select_for_update().in_bulk(child_pks)
        for child_pk, final_name, cascade_name in reconciliations:
            child = children.get(child_pk)
            if child is None or child.name == final_name:
                continue
            if child.name != cascade_name:
                logger.warning(
                    "Channel interface %s changed to unexpected name %r before deferred reconciliation; "
                    "leaving it unchanged.",
                    child_pk,
                    child.name,
                )
                continue
            previous_name = child.name
            try:
                with transaction.atomic(using=db_alias):
                    child.name = final_name
                    child.full_clean()
                    child.save(using=db_alias)
            except ValidationError:
                child.name = previous_name
                logger.exception(
                    "Failed to restore channel interface %s from NetBox's deferred name %r to %r; skipping.",
                    child_pk,
                    cascade_name,
                    final_name,
                )
            except IntegrityError as error:
                child.name = previous_name
                if not _is_name_collision(error):
                    raise
                logger.warning(
                    "Channel interface %s could not reclaim name %r after NetBox's deferred rename; skipping.",
                    child_pk,
                    final_name,
                )


def _preserve_names_across_parent_cascade(plan, member_outcomes):  # pragma: no cover - channelization only
    """Schedule restoration of blocked conventional channel names after a parent cascade."""
    if plan.parent_pk is None:
        return
    parent_member = next(member for member in plan.members if member.role == MemberRole.PARENT)
    parent_outcome = next(outcome for outcome in member_outcomes if outcome.interface_pk == plan.parent_pk)
    if parent_outcome.status != FamilyStatus.CHANGED:
        return
    final_parent_name = parent_member.target_name
    outcomes_by_pk = {outcome.interface_pk: outcome for outcome in member_outcomes}
    reconciliations = []
    for member in plan.members:
        if member.role != MemberRole.CHANNEL:
            continue
        channel_id = member.snapshot.channel_id
        final_name = (
            member.target_name
            if outcomes_by_pk[member.snapshot.pk].status in (FamilyStatus.CHANGED, FamilyStatus.UNCHANGED)
            else member.snapshot.name
        )
        old_conventional_name = f"{parent_member.snapshot.name}:{channel_id}"
        cascade_name = f"{final_parent_name}:{channel_id}"
        if final_name == old_conventional_name and final_name != cascade_name:
            reconciliations.append((member.snapshot.pk, final_name, cascade_name))
    if reconciliations:
        transaction.on_commit(
            lambda: _restore_deferred_channel_names(tuple(reconciliations), plan.db_alias),
            using=plan.db_alias,
        )


def _execute_channelized_members(plan, live_by_pk):  # pragma: no cover - requires channelization support
    """Execute a channelized family after its parent succeeds."""
    parent_member = next(member for member in plan.members if member.role == MemberRole.PARENT)
    parent_outcome = _rename_member(parent_member, live_by_pk[parent_member.snapshot.pk], plan.db_alias)
    if parent_outcome.status == FamilyStatus.BLOCKED:
        return (
            parent_outcome,
            *(
                _blocked_member(member, PARENT_BLOCKED_REASON)
                for member in plan.members
                if member.role != MemberRole.PARENT
            ),
        )
    return (
        parent_outcome,
        *(
            _rename_member(member, live_by_pk[member.snapshot.pk], plan.db_alias)
            for member in plan.members
            if member.role != MemberRole.PARENT
        ),
    )


def _execute_members(plan, live_by_pk):
    """Execute members while enforcing parent-first family semantics."""
    if plan.blocked_reason:  # pragma: no cover - channelized mismatch only
        logger.warning(
            "Installed family of interface %r is blocked: %s.",
            plan.members[0].snapshot.name,
            plan.blocked_reason,
        )
        return tuple(_blocked_member(member, plan.blocked_reason) for member in plan.members)
    if plan.parent_pk is None:
        return tuple(_rename_member(member, live_by_pk[member.snapshot.pk], plan.db_alias) for member in plan.members)
    return _execute_channelized_members(plan, live_by_pk)  # pragma: no cover - channelization only


def _stale_outcome(plan: InstalledFamilyPlan) -> FamilyOutcome:
    """Return a stale result without applying any planned member."""
    return FamilyOutcome(
        family_id=plan.family_id,
        topology=plan.topology,
        status=FamilyStatus.STALE,
        members=tuple(_member_outcome(member, FamilyStatus.STALE, STALE_REASON) for member in plan.members),
        reason=STALE_REASON,
    )


def _execute_plan(plan: InstalledFamilyPlan) -> FamilyOutcome:
    """Execute one family plan in its own transaction."""
    with transaction.atomic(using=plan.db_alias):
        interfaces = _lock_family(plan)
        if _is_stale(plan, interfaces):
            return _stale_outcome(plan)
        live_by_pk = {interface.pk: interface for interface in interfaces}
        member_outcomes = _execute_members(plan, live_by_pk)
        _preserve_names_across_parent_cascade(plan, member_outcomes)
        return FamilyOutcome(
            family_id=plan.family_id,
            topology=plan.topology,
            status=_family_status(member_outcomes),
            members=member_outcomes,
            reason=plan.blocked_reason,
        )


def execute_installed_plan_set(plan_set: InstalledFamilyPlanSet) -> InstalledPlanSetOutcome:
    """Execute each installed family in a separate transaction."""
    return InstalledPlanSetOutcome(families=tuple(_execute_plan(plan) for plan in plan_set.plans))
