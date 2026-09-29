# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and execute the creation of interface families."""

import logging

from dcim.choices import InterfaceTypeChoices
from dcim.models import Interface, InterfaceTemplate
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

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
    first_taken_name,
    is_name_collision,
    name_owners,
    reconcile_after_parent_cascade,
)
from .targets import UNCLAIMED_BASE_REASON, channelized_family_names, flat_family_names

logger = logging.getLogger(__name__)

UNSUPPORTED_REASON = "this NetBox release cannot model channelized interfaces"
STALE_REASON = "the base interface changed after planning"
MODULE_CHANGED_REASON = "the module's interfaces changed after planning"


def _flat_expansion(module_type_id, module_id) -> bool:  # pragma: no cover - channelization only
    """Return whether the module carries more plain interfaces than its templates describe."""
    templates = InterfaceTemplate.objects.filter(module_type_id=module_type_id).count()
    plain = Interface.objects.filter(module_id=module_id, channel_id__isnull=True)
    return plain.count() > templates


def has_flat_expansion(module) -> bool:  # pragma: no cover - requires channelization support
    """Return whether *module* carries more plain interfaces than its module type's templates describe.

    A flat breakout leaves N-1 rows beyond the templates, so the surplus is the structural mark of a
    family an earlier apply installed.  A channel belongs to the parent that declares it, so it is
    never part of that surplus: counting one would stop a second port from gaining its own family.
    Counting templates rather than their resolved names keeps two templates that resolve to the same
    string from reading as one.
    """
    return _flat_expansion(module.module_type_id, module.pk)


def carries_flat_expansion(interfaces, templates) -> bool:
    """Return whether *interfaces* hold more rows outside a channel than *templates* has, as ``has_flat_expansion``."""
    return sum(1 for interface in interfaces if getattr(interface, "channel_id", None) is None) > len(templates)


def _plan(module, base, parent_target_name, channels, status=None, reason=""):
    """Build one immutable structural plan for *base*."""
    return StructuralFamilyPlan(
        family_id=f"structural:{base.pk}",
        device_id=module.device_id,
        module_id=module.pk,
        module_type_id=module.module_type_id,
        base=InterfaceSnapshot.from_interface(base),
        parent_target_name=parent_target_name,
        channel_count=len(channels),
        channels=tuple(PlannedChannel(channel_id=channel_id, name=name) for channel_id, name in channels),
        precondition_status=status,
        precondition_reason=reason,
    )


def _modelled_plan(module, rule, variables, base, base_name, flat_expansion):  # pragma: no cover - channelization only
    """Return the plan for a NetBox release that can hold the family."""
    if base_name is None:
        return _plan(module, base, base.name, (), FamilyStatus.BLOCKED, UNCLAIMED_BASE_REASON)
    try:
        parent_target_name, channels = channelized_family_names(rule, base.name, base_name, variables)
    except (TypeError, ValueError) as error:
        reason = f"failed to evaluate the family names: {error}"
        return _plan(module, base, base.name, (), FamilyStatus.FAILED, reason)
    if flat_expansion:
        # Converting one sibling into a parent would strand the others beside the new family.
        reason = f"module {module} already carries a flat breakout family"
        return _plan(module, base, parent_target_name, channels, FamilyStatus.BLOCKED, reason)
    return _plan(module, base, parent_target_name, channels)


def plan_structural_family(module, rule, variables, base, base_name, flat_expansion) -> StructuralFamilyPlan:
    """Return the plan for the channelized family *rule* builds on plain interface *base*.

    *base_name* is the value of ``{base}``, or None when no template claims *base*. *flat_expansion* is
    whether the module carries a flat breakout family, as ``carries_flat_expansion`` decides.
    """
    if not supports_channelization():
        return _plan(module, base, base.name, (), FamilyStatus.UNSUPPORTED, UNSUPPORTED_REASON)
    return _modelled_plan(module, rule, variables, base, base_name, flat_expansion)  # pragma: no cover - see above


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
    """Log why the family was not built and return an outcome that touched no row."""
    logger.warning("Cannot build a channelized family on interface %r: %s.", plan.base.name, reason)
    member = MemberOutcome(
        interface_pk=plan.base.pk,
        current_name=plan.base.name,
        target_name=target_name,
        status=status,
        reason=reason,
    )
    return _outcome(plan, status, (member,), reason)


def _locked_base(plan):
    """Lock and return the live base row, or None when it is gone."""
    return (
        Interface.objects.select_for_update(of=("self",))
        .select_related("device", "module")
        .filter(pk=plan.base.pk)
        .first()
    )


def _first_taken_name(plan, own_pks):
    """Return the first planned name an interface outside *own_pks* already owns on the device, or None.

    One query, because the caller holds the plan's row locks while this runs.
    """
    return first_taken_name(plan.target_names, name_owners(plan.device_id, plan.target_names), own_pks)


def _create_channels(plan, parent):  # pragma: no cover - requires channelization support
    """Create every planned channel under *parent* and return their outcomes."""
    members = []
    for channel in plan.channels:
        row = Interface(
            device=parent.device,
            module=parent.module,
            name=channel.name,
            type=InterfaceTypeChoices.TYPE_CHANNEL,
            parent=parent,
            channel_id=channel.channel_id,
            enabled=parent.enabled,
        )
        row.full_clean()
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


def _create_family(plan, base):  # pragma: no cover - requires channelization support
    """Rewrite *base* into the family parent, create its channels, and return every member outcome."""
    parent_status = FamilyStatus.CHANGED if plan.parent_target_name != base.name else FamilyStatus.UNCHANGED
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
    channel_members = _create_channels(plan, base)
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
        with transaction.atomic():
            base = _locked_base(plan)
            if base is None or InterfaceSnapshot.from_interface(base) != plan.base:
                return _refused(plan, FamilyStatus.STALE, STALE_REASON, plan.parent_target_name)
            # A sibling added since planning would be stranded beside the family this plan builds.
            if _flat_expansion(plan.module_type_id, plan.module_id):
                return _refused(plan, FamilyStatus.STALE, MODULE_CHANGED_REASON, plan.parent_target_name)
            taken = _first_taken_name(plan, (plan.base.pk,))
            if taken is not None:
                return _refused(plan, FamilyStatus.BLOCKED, f"{COLLISION_REASON}: {taken}", taken)
            members = _create_family(plan, base)
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
        with transaction.atomic():
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
