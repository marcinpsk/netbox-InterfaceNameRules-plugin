# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Plan and execute the creation of channelized interface families."""

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
    InterfaceSnapshot,
    MemberOutcome,
    PlannedChannel,
    StructuralFamilyPlan,
)
from .installed import module_db_alias
from .names import COLLISION_REASON, is_name_collision, name_is_taken, reconcile_after_parent_cascade
from .targets import channelized_family_names

logger = logging.getLogger(__name__)

UNSUPPORTED_REASON = "this NetBox release cannot model channelized interfaces"
STALE_REASON = "the base interface changed after planning"
MODULE_CHANGED_REASON = "the module's interfaces changed after planning"


def _flat_expansion(module_type_id, module_id, db_alias) -> bool:  # pragma: no cover - channelization only
    """Return whether the module carries more interfaces than its module type's templates describe."""
    templates = InterfaceTemplate.objects.using(db_alias).filter(module_type_id=module_type_id).count()
    return Interface.objects.using(db_alias).filter(module_id=module_id).count() > templates


def has_flat_expansion(module) -> bool:  # pragma: no cover - requires channelization support
    """Return whether *module* carries more interfaces than its module type's templates describe.

    A flat breakout leaves N-1 rows beyond the templates, so the surplus is the structural mark of a
    family an earlier apply installed.  Counting templates rather than their resolved names keeps
    two templates that resolve to the same string from reading as one.
    """
    return _flat_expansion(module.module_type_id, module.pk, module_db_alias(module))


def _plan(module, db_alias, base, parent_target_name, channels, status=None, reason=""):
    """Build one immutable structural plan for *base*."""
    return StructuralFamilyPlan(
        family_id=f"structural:{base.pk}",
        device_id=module.device_id,
        module_id=module.pk,
        module_type_id=module.module_type_id,
        db_alias=db_alias,
        base=InterfaceSnapshot.from_interface(base),
        parent_target_name=parent_target_name,
        channel_count=len(channels),
        channels=tuple(PlannedChannel(channel_id=channel_id, name=name) for channel_id, name in channels),
        precondition_status=status,
        precondition_reason=reason,
    )


def _modelled_plan(module, rule, variables, db_alias, base):  # pragma: no cover - channelization only
    """Return the plan for a NetBox release that can hold the family."""
    try:
        parent_target_name, channels = channelized_family_names(rule, base.name, variables)
    except (TypeError, ValueError) as error:
        reason = f"failed to evaluate the family names: {error}"
        return _plan(module, db_alias, base, base.name, (), FamilyStatus.FAILED, reason)
    if has_flat_expansion(module):
        # Converting one sibling into a parent would strand the others beside the new family.
        reason = f"module {module} already carries a flat breakout family"
        return _plan(module, db_alias, base, parent_target_name, channels, FamilyStatus.BLOCKED, reason)
    return _plan(module, db_alias, base, parent_target_name, channels)


def plan_structural_family(module, rule, variables, base) -> StructuralFamilyPlan:
    """Return the immutable plan for the channelized family *rule* builds on plain interface *base*."""
    db_alias = module_db_alias(module)
    if not supports_channelization():
        return _plan(module, db_alias, base, base.name, (), FamilyStatus.UNSUPPORTED, UNSUPPORTED_REASON)
    return _modelled_plan(module, rule, variables, db_alias, base)  # pragma: no cover - see above


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


def _locked_base(plan):  # pragma: no cover - requires channelization support
    """Lock and return the live base row, or None when it is gone."""
    return (
        Interface.objects.using(plan.db_alias)
        .select_for_update(of=("self",))
        .select_related("device", "module")
        .filter(pk=plan.base.pk)
        .first()
    )


def _first_taken_name(plan):  # pragma: no cover - requires channelization support
    """Return the first planned name another interface on the device already owns, or None."""
    for target_name in plan.target_names:
        if name_is_taken(plan.device_id, target_name, plan.db_alias, exclude_pk=plan.base.pk):
            return target_name
    return None


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
        row.save(using=plan.db_alias)
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
    base.save(using=plan.db_alias)
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
        plan.db_alias,
    )
    return (parent_member, *channel_members)


def _install_family(plan):  # pragma: no cover - requires channelization support
    """Create the whole family in one transaction, or write nothing at all."""
    try:
        with transaction.atomic(using=plan.db_alias):
            base = _locked_base(plan)
            if base is None or InterfaceSnapshot.from_interface(base) != plan.base:
                return _refused(plan, FamilyStatus.STALE, STALE_REASON, plan.parent_target_name)
            # A sibling added since planning would be stranded beside the family this plan builds.
            if _flat_expansion(plan.module_type_id, plan.module_id, plan.db_alias):
                return _refused(plan, FamilyStatus.STALE, MODULE_CHANGED_REASON, plan.parent_target_name)
            taken = _first_taken_name(plan)
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


def install_channelized_family(module, rule, variables, base) -> FamilyOutcome:
    """Build the channelized family *rule* describes on plain interface *base*."""
    return execute_structural_family(plan_structural_family(module, rule, variables, base))
