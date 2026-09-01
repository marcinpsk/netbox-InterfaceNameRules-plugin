# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Convert installed flat breakout families into the channelized topology.

An earlier flat apply leaves N sibling interfaces where NetBox 4.7+ models a channelized parent
with N channel subinterfaces.  Converting one rewrites rows an operator owns (cables, addresses,
tags), so it is never a side effect of applying a rule: the operator confirms it per family.

A family is identified by its ch-0 row, the way the flat apply that installed it named that row.
A family whose siblings the module no longer carries whole is still offered, and refused with the
row it is missing: silence would read as "nothing here to convert".
"""

import logging
from dataclasses import replace

from dcim.choices import InterfaceTypeChoices
from dcim.models import Interface
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from ..naming import build_variables
from .batch import BatchOutcome
from .capabilities import supports_channelization
from .domain import (
    ConversionCandidate,
    ConversionMember,
    ConversionPlan,
    ConversionPreview,
    FamilyOutcome,
    FamilyStatus,
    FamilyTopology,
    InterfaceSnapshot,
    MemberOutcome,
)
from .installed import (
    TemplateNames,
    family_names_for,
    flat_family_bases,
    interfaces_by_module,
    is_plain_interface,
)
from .names import COLLISION_REASON, is_name_collision, name_is_taken
from .targets import builds_channelized_family, channelized_family_names
from .template_names import pinned_template_cache

logger = logging.getLogger(__name__)

STALE_REASON = "the flat family changed after it was scanned"
INCOMPLETE_REASON = "this module carries no complete flat family"
UNSUPPORTED_REASON = "this NetBox release cannot model channelized interfaces"


def conversion_offered(rule) -> bool:
    """Return whether *rule* describes a topology an installed flat family could be converted into.

    A disabled rule renames nothing on any apply path, so it converts nothing either.  A flat
    family has no parent row (its ch-0 interface *is* the base), so without a parent name there is
    nowhere for that base to go and the conversion is not offered at all.
    """
    return rule.enabled and builds_channelized_family(rule) and bool(rule.parent_name_template)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _conversion_plan(
    module, parent_name, channel_names, rows, channelization_supported
):  # pragma: no cover - channelization only
    """Build one immutable conversion plan from the rows a module carries for one family."""
    base, *siblings = rows
    plan = ConversionPlan(
        family_id=f"conversion:{base.pk}",
        device_id=module.device_id,
        module_id=module.pk,
        base=InterfaceSnapshot.from_interface(base),
        parent_target_name=parent_name,
        channel_names=channel_names,
        siblings=tuple(
            ConversionMember(snapshot=InterfaceSnapshot.from_interface(sibling), channel_id=channel_id)
            for channel_id, sibling in siblings
        ),
    )
    if not channelization_supported:
        return replace(
            plan,
            precondition_status=FamilyStatus.UNSUPPORTED,
            precondition_reason=UNSUPPORTED_REASON,
        )
    missing = plan.missing_names
    if not missing:
        return plan
    reason = f"{missing[0]!r} is missing: {INCOMPLETE_REASON}"
    return replace(plan, precondition_status=FamilyStatus.BLOCKED, precondition_reason=reason)


def _family_rows(by_name, channel_names):  # pragma: no cover - requires channelization support
    """Return the ch-0 row and every sibling row this module still carries, with its channel id.

    A sibling is taken whatever it has become, so a row that now belongs to another parent is
    reported against this family instead of quietly leaving a gap where it used to be.
    """
    base = by_name.get(channel_names[0])
    if base is None or not is_plain_interface(base):
        return None
    siblings = [
        (channel_id, by_name[name]) for channel_id, name in enumerate(channel_names[1:], start=2) if name in by_name
    ]
    return [base, *siblings]


def plan_module_conversions(
    module, rule, variables, interfaces
) -> tuple[ConversionPlan, ...]:  # pragma: no cover - requires channelization support
    """Return one plan for every flat family *rule* names on *module*, complete or not.

    The parent takes the name the rule resolves for this module now, so a family named before a
    virtual-chassis renumber converts to the parent an apply would give it; the channels keep the
    names they already carry, because converting retypes those rows in place.
    """
    catalog = TemplateNames(module)
    by_name = {interface.name: interface for interface in interfaces}
    channelization_supported = supports_channelization()
    plans = []
    claimed = set()
    for base_name, source_base in flat_family_bases(rule, variables, interfaces, catalog):
        names = family_names_for(rule, variables, base_name, source_base)
        if names is None:
            continue
        _target_names, channel_names = names
        rows = _family_rows(by_name, channel_names)
        if rows is None or rows[0].pk in claimed:
            continue
        claimed.add(rows[0].pk)
        parent_name, _channels = channelized_family_names(rule, base_name, variables)
        plans.append(
            _conversion_plan(
                module,
                parent_name,
                channel_names,
                rows,
                channelization_supported,
            )
        )
    return tuple(plans)


def _planned_families(rule, modules):  # pragma: no cover - requires channelization support
    """Yield every convertible family in the batch as ``(module, plan, base interface)``.

    The module rows, their interfaces and each module type's templates are read once for the whole
    batch, so a second module of the same type costs the scan no extra query.
    """
    by_module = interfaces_by_module(modules)
    for module in modules:
        interfaces = by_module[module.pk]
        rows_by_pk = {interface.pk: interface for interface in interfaces}
        variables = build_variables(module.module_bay, device=module.device)
        for plan in plan_module_conversions(module, rule, variables, interfaces):
            yield module, plan, rows_by_pk[plan.base.pk]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _outcome(plan, status, members, reason=""):  # pragma: no cover - requires channelization support
    """Build the immutable outcome of one conversion."""
    return FamilyOutcome(
        family_id=plan.family_id,
        topology=FamilyTopology.CHANNELIZED,
        status=status,
        members=members,
        reason=reason,
    )


def _refused(plan, status, reason):  # pragma: no cover - requires channelization support
    """Log why the family was not converted and return an outcome that touched no row."""
    logger.warning(
        "Cannot convert the flat family of interface %r (device %s, module %s) into the channelized "
        "parent %r: %s. Skipping.",
        plan.base.name,
        plan.device_id,
        plan.module_id,
        plan.parent_target_name,
        reason,
    )
    targets = (plan.parent_target_name, *plan.sibling_target_names)
    members = tuple(
        MemberOutcome(
            interface_pk=pk,
            current_name=current_name,
            target_name=target_name,
            status=status,
            reason=reason,
        )
        for pk, current_name, target_name in zip(plan.member_pks, plan.current_names, targets, strict=True)
    )
    return _outcome(plan, status, members, reason)


def _validate_or_block(interface, role):  # pragma: no cover - requires channelization support
    """Run NetBox's own validation on *interface*, restating a rejection as this family's reason."""
    try:
        interface.full_clean()
    except ValidationError as error:
        raise ValidationError(f"{role} {interface.name!r}: {' '.join(error.messages)}") from error


def _locked_family(plan):  # pragma: no cover - requires channelization support
    """Lock every planned row for the transaction and return the live rows by primary key."""
    return {
        interface.pk: interface
        for interface in (
            Interface.objects.select_for_update(of=("self",))  # module is nullable, so locking the join is refused
            .select_related("device", "module")
            .filter(pk__in=plan.member_pks)
            .order_by("pk")
        )
    }


def _is_stale(plan, live) -> bool:  # pragma: no cover - requires channelization support
    """Return whether live identity, names, membership or topology changed since the scan."""
    planned = {plan.base.pk: plan.base, **{member.snapshot.pk: member.snapshot for member in plan.siblings}}
    return planned != {pk: InterfaceSnapshot.from_interface(interface) for pk, interface in live.items()}


def _blocking_reason(plan, live) -> str:  # pragma: no cover - requires channelization support
    """Return why this family cannot become a channelized family, or an empty string.

    Only what upstream cannot decide for us is checked here; everything else is left to
    ``full_clean()`` on each prospective row, which inherits upstream's rules as they grow.
    """
    if name_is_taken(plan.device_id, plan.parent_target_name, exclude_pk=plan.base.pk):
        return f"the parent name {plan.parent_target_name!r} is already taken on this device"
    for member in plan.siblings:
        sibling = live[member.snapshot.pk]
        # Rebinding it validates cleanly, so only this check keeps the other family whole.
        if sibling.channel_id is not None:
            owner = sibling.parent.name if sibling.parent_id else "another parent"
            return (
                f"{sibling.name!r} is already channel {sibling.channel_id} of {owner}; "
                f"converting would take it out of that family"
            )
        # TYPE_CHANNEL is nonconnectable but not virtual, so Interface.clean() accepts a cable on one.
        if sibling.cable_id:
            return f"{sibling.name!r} has a cable attached; a channel takes its cable from the parent"
    return ""


def _carry_assignments(plan, base, channel):  # pragma: no cover - requires channelization support
    """Move the ch-0 row's addresses and first-hop groups onto the channel that took its name.

    Saved one row at a time so each carried object is validated, and recorded in the changelog,
    like every other row this conversion writes.  A model save writes back every field it read, so
    each row is locked first: an edit landing between the read and the save would otherwise be
    overwritten by the values this transaction started with.
    """
    addresses = base.ip_addresses.select_for_update().order_by("pk")
    for address in addresses:
        address.assigned_object = channel
        address.full_clean()
        address.save()
    assignments = base.fhrp_group_assignments.select_for_update().order_by("pk")
    for assignment in assignments:
        assignment.interface = channel
        assignment.full_clean()
        assignment.save()


def _split_base(plan, base):  # pragma: no cover - requires channelization support
    """Make *base* the family parent and move its logical identity onto a new channel-1 row.

    Everything an operator configured on the ch-0 row described a channel, not the cage carrying
    it, so addresses, VLANs, MTU, description and tags move; custom fields can mean either thing
    and are copied.  The physical row keeps its pk, cable, type, module link and mark_connected.
    """
    carried = {
        "description": base.description,
        "mtu": base.mtu,
        "mode": base.mode,
        "untagged_vlan_id": base.untagged_vlan_id,
        "vrf_id": base.vrf_id,
    }
    tagged_vlans = list(base.tagged_vlans.all())
    tags = list(base.tags.all())
    channel_name = base.name

    base.name = plan.parent_target_name
    base.channels = plan.channel_count
    base.description = ""
    base.mtu = None
    base.mode = ""
    base.untagged_vlan = None
    base.vrf = None
    _validate_or_block(base, "parent")
    base.save()  # BaseInterface.save() drops the tagged VLANs of a row that no longer tags
    base.tags.clear()

    channel = Interface(
        device=base.device,
        module=base.module,
        name=channel_name,
        type=InterfaceTypeChoices.TYPE_CHANNEL,
        parent=base,
        channel_id=1,
        enabled=base.enabled,
        custom_field_data=dict(base.custom_field_data or {}),
        **carried,
    )
    _validate_or_block(channel, "channel")
    channel.save()
    channel.tagged_vlans.set(tagged_vlans)
    channel.tags.set(tags)
    _carry_assignments(plan, base, channel)
    return channel


def _rewrite(plan, live):  # pragma: no cover - requires channelization support
    """Rewrite the whole family and return one outcome per row it wrote."""
    base = live[plan.base.pk]
    previous_name = base.name
    channel = _split_base(plan, base)
    members = [
        MemberOutcome(base.pk, previous_name, plan.parent_target_name, FamilyStatus.CHANGED),
        MemberOutcome(channel.pk, channel.name, channel.name, FamilyStatus.CHANGED),
    ]
    for member in plan.siblings:
        sibling = live[member.snapshot.pk]
        sibling.type = InterfaceTypeChoices.TYPE_CHANNEL
        sibling.parent = base
        sibling.channel_id = member.channel_id
        _validate_or_block(sibling, "channel")
        sibling.save()
        members.append(MemberOutcome(sibling.pk, sibling.name, sibling.name, FamilyStatus.CHANGED))
    return tuple(members)


def _convert(plan, commit):  # pragma: no cover - requires channelization support
    """Convert one family inside a single transaction, or leave every row exactly as it was.

    A dry run (*commit* False) and a family that turns out to be unconvertible both roll the whole
    rewrite back, so a family is never half converted and a scan writes nothing at all.
    """
    try:
        with transaction.atomic():
            live = _locked_family(plan)
            if _is_stale(plan, live):
                return _refused(plan, FamilyStatus.STALE, STALE_REASON)
            reason = _blocking_reason(plan, live)
            if reason:
                return _refused(plan, FamilyStatus.BLOCKED, reason)
            members = _rewrite(plan, live)
            if not commit:
                transaction.set_rollback(True)
    except ValidationError as error:
        return _refused(plan, FamilyStatus.BLOCKED, " ".join(error.messages))
    except IntegrityError as error:
        if not is_name_collision(error):
            raise
        return _refused(plan, FamilyStatus.BLOCKED, COLLISION_REASON)
    return _outcome(plan, FamilyStatus.CHANGED, members)


def _dry_run(plan) -> FamilyOutcome:  # pragma: no cover - requires channelization support
    """Report what converting *plan* would do, having written and rolled back every row of it."""
    if plan.precondition_status is not None:
        return _refused(plan, plan.precondition_status, plan.precondition_reason)
    return _convert(plan, commit=False)


def execute_conversion(plan: ConversionPlan) -> FamilyOutcome:  # pragma: no cover - requires channelization support
    """Convert the planned flat family, or leave every row exactly as it was.

    Only a conversion plan names the family rows to rewrite, so anything else (a prospective plan
    above all) is refused before a single row is locked.
    """
    if not isinstance(plan, ConversionPlan):
        raise TypeError(f"{type(plan).__name__} is not an executable conversion plan")
    if plan.precondition_status is not None:
        return _refused(plan, plan.precondition_status, plan.precondition_reason)
    return _convert(plan, commit=True)


# ---------------------------------------------------------------------------
# Batch entry points
# ---------------------------------------------------------------------------


def preview_rule_conversions(rule, modules, limit=None) -> ConversionPreview:
    """Return what converting each of *rule*'s flat families would do, convertible or not.

    Nothing is written: every family is converted inside a savepoint that is rolled back again, so
    each candidate carries the reason NetBox itself would refuse the family rather than a guess at
    its rules.  That dry run is what the scan costs, and a blocked family costs it too, so *limit*
    caps the families examined rather than the convertible ones among them.
    """
    if not conversion_offered(rule):
        return ConversionPreview(candidates=())
    return _preview(rule, modules, limit)  # pragma: no cover - requires channelization support


def _preview(rule, modules, limit) -> ConversionPreview:  # pragma: no cover - requires channelization support
    """Dry-run at most *limit* of the rule's flat families; see ``preview_rule_conversions``."""
    candidates = []
    with pinned_template_cache(modules):
        for module, plan, base in _planned_families(rule, modules):
            if limit is not None and len(candidates) >= limit:
                return ConversionPreview(candidates=tuple(candidates), has_more=True)
            outcome = _dry_run(plan)
            candidates.append(ConversionCandidate(plan=plan, module=module, interface=base, reason=outcome.reason))
    return ConversionPreview(candidates=tuple(candidates))


def convert_rule_families(rule, modules, selected_pks=None) -> BatchOutcome:
    """Convert the confirmed flat families of *rule*; return one explicit outcome per family.

    *selected_pks* is the set of ch-0 interface primary keys the operator confirmed: ``None``
    converts every convertible family (the batch the background job runs), an empty collection
    converts none.  A family that cannot be converted is reported and passed over, so it is never
    half converted and never costs the rest of the batch.
    """
    return _convert_families(rule, modules, selected_pks)  # pragma: no cover - see above


def _convert_families(rule, modules, selected_pks) -> BatchOutcome:  # pragma: no cover - channelization only
    """Convert the confirmed flat families of *rule*; see ``convert_rule_families``."""
    if not conversion_offered(rule) or (selected_pks is not None and not selected_pks):
        return BatchOutcome(families=())
    families = []
    with pinned_template_cache(modules):
        for _module, plan, _base in _planned_families(rule, modules):
            if selected_pks is not None and plan.base.pk not in selected_pks:
                continue
            families.append(execute_conversion(plan))
    return BatchOutcome(families=tuple(families))
