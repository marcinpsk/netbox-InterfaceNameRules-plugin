# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Discover and plan installed interface families."""

from dcim.models import Interface

from ..name_template import evaluate_name_template
from .domain import (
    FamilyStatus,
    FamilyTopology,
    InstalledFamilyPlan,
    InstalledFamilyPlanSet,
    InterfaceSnapshot,
    MemberRole,
    PlannedMember,
)
from .raw_bases import GivenRawNames, RawBases
from .targets import (
    UNCLAIMED_BASE_REASON,
    builds_flat_family,
    channelized_family_targets,
    flat_family_names,
    lockstep_family_targets,
    template_channel_suffixes,
)
from .template_names import TemplateNames


def is_plain_interface(interface) -> bool:
    """Return whether *interface* can be a flat-family member."""
    return (
        getattr(interface, "parent_id", None) is None
        and getattr(interface, "channel_id", None) is None
        and getattr(interface, "channels", None) is None
    )


def is_channelized_parent(interface) -> bool:  # pragma: no cover - requires channelization support
    """Return whether *interface* declares a channelized family."""
    return getattr(interface, "channels", None) is not None


def _is_channel(interface) -> bool:  # pragma: no cover - requires channelization support
    """Return whether *interface* is bound to a parent channel."""
    return getattr(interface, "channel_id", None) is not None


def _flat_plan(module, target_names, interfaces, status=None, reason=""):
    """Build one immutable plan for the flat family whose rows are *interfaces*."""
    members = tuple(
        PlannedMember(
            snapshot=InterfaceSnapshot.from_interface(interface),
            target_name=target_name,
            role=MemberRole.FLAT_MEMBER,
        )
        for interface, target_name in zip(interfaces, target_names, strict=True)
    )
    return InstalledFamilyPlan(
        family_id=f"flat:{members[0].snapshot.pk}",
        topology=FamilyTopology.FLAT,
        device_id=module.device_id,
        module_id=module.pk,
        members=members,
        precondition_status=status,
        precondition_reason=reason,
    )


def _claimed_flat_plan(module, rule, variables, family, interfaces):
    """Return the plan for a claimed flat family, or None when its first row builds it again (ADR 0013)."""
    kept = tuple(interface.name for interface in interfaces)
    try:
        target_names = flat_family_names(rule, variables, family.base_name)
    except (TypeError, ValueError) as error:
        return _flat_plan(
            module, kept, interfaces, FamilyStatus.FAILED, f"failed to evaluate the family names: {error}"
        )
    if len(interfaces) == len(family.names):
        return _flat_plan(module, target_names, interfaces)
    if target_names == family.names:
        return None
    missing = len(family.names) - len(interfaces)
    reason = f"the flat family is missing {missing} of its {len(family.names)} interfaces"
    return _flat_plan(module, kept, interfaces, FamilyStatus.BLOCKED, reason)


def _channelized_plan(device_id, module_id, parent, children, targets):  # pragma: no cover
    """Build one plan for an existing channelized family from the names *targets* intends."""
    members = [
        PlannedMember(
            snapshot=InterfaceSnapshot.from_interface(parent),
            target_name=targets.parent_name,
            role=MemberRole.PARENT,
        )
    ]
    members.extend(
        PlannedMember(
            snapshot=InterfaceSnapshot.from_interface(child),
            target_name=target,
            role=MemberRole.CHANNEL,
            reason=reason,
        )
        for child, (target, reason) in zip(children, targets.channels, strict=True)
    )
    return InstalledFamilyPlan(
        family_id=f"channelized:{parent.pk}",
        topology=FamilyTopology.CHANNELIZED,
        device_id=device_id,
        module_id=module_id,
        members=tuple(members),
        parent_pk=parent.pk,
        precondition_status=targets.status,
        precondition_reason=targets.reason,
    )


def _module_family_targets(rule, variables, parent, base_name, children, suffixes):  # pragma: no cover
    """Return the names *rule* intends for a channelized family a module carries."""
    return channelized_family_targets(
        rule,
        variables,
        parent.name,
        base_name,
        parent.channels,
        tuple((child.name, child.channel_id) for child in children),
        suffixes,
    )


def _channelized_plans(module, rule, variables, interfaces, bases):  # pragma: no cover
    """Return one plan for every structurally discovered channelized family."""
    parents = [interface for interface in interfaces if is_channelized_parent(interface)]
    if not parents:
        return []
    children_by_parent: dict[int, list] = {}
    for interface in interfaces:
        if _is_channel(interface) and interface.parent_id is not None:
            children_by_parent.setdefault(interface.parent_id, []).append(interface)
    suffixes = template_channel_suffixes(bases.catalog.get())
    plans = []
    for parent in parents:
        children = children_by_parent.get(parent.pk, [])
        children.sort(key=lambda child: (child.channel_id, child.pk))
        targets = _module_family_targets(rule, variables, parent, bases.base_for(parent.name), children, suffixes)
        plans.append(_channelized_plan(module.device_id, module.pk, parent, children, targets))
    return plans


def interfaces_by_module(modules):
    """Load every interface of a module batch in one query, in stable per-module order."""
    by_module: dict[int, list] = {module.pk: [] for module in modules}
    if not by_module:
        return by_module
    rows = Interface.objects.filter(module_id__in=list(by_module)).order_by("module_id", "name")
    for interface in rows:
        by_module[interface.module_id].append(interface)
    return by_module


def device_interface_families(interfaces):
    """Return each base interface outside a channel with its channel children, in channel order."""
    children_by_parent: dict[int, list] = {}
    for interface in interfaces:
        if _is_channel(interface) and interface.parent_id is not None:
            children_by_parent.setdefault(interface.parent_id, []).append(interface)
    for children in children_by_parent.values():
        children.sort(key=lambda child: (child.channel_id, child.pk))
    return tuple(
        (interface, tuple(children_by_parent.get(interface.pk, ())))
        for interface in interfaces
        if not _is_channel(interface)
    )


def plan_kept_interface(module, interface, reason) -> InstalledFamilyPlan:
    """Return a plan that keeps the name of one interface of *module* and reports *reason*."""
    return InstalledFamilyPlan(
        family_id=f"flat:{interface.pk}",
        topology=FamilyTopology.FLAT,
        device_id=module.device_id,
        module_id=module.pk,
        members=(
            PlannedMember(
                snapshot=InterfaceSnapshot.from_interface(interface),
                target_name=interface.name,
                role=MemberRole.FLAT_MEMBER,
            ),
        ),
        precondition_status=FamilyStatus.BLOCKED,
        precondition_reason=reason,
    )


def _interface_rename_plan(device_id, module_id, rule, variables, interface, base_name) -> InstalledFamilyPlan:
    """Return a plan that renames one interface which belongs to no family, from *base_name* as ``{base}``."""
    status, reason, target_name = None, "", interface.name
    if base_name is None:
        status, reason = FamilyStatus.BLOCKED, UNCLAIMED_BASE_REASON
    else:
        try:
            target_name = evaluate_name_template(rule.name_template, {**variables, "base": base_name})
        except (TypeError, ValueError) as error:
            status, reason = FamilyStatus.FAILED, f"failed to evaluate the interface name: {error}"
    return InstalledFamilyPlan(
        family_id=f"flat:{interface.pk}",
        topology=FamilyTopology.FLAT,
        device_id=device_id,
        module_id=module_id,
        members=(
            PlannedMember(
                snapshot=InterfaceSnapshot.from_interface(interface),
                target_name=target_name,
                role=MemberRole.FLAT_MEMBER,
            ),
        ),
        precondition_status=status,
        precondition_reason=reason,
    )


def plan_interface_rename(module, rule, variables, interface, bases) -> InstalledFamilyPlan:
    """Return the plan that renames one module interface which belongs to no family."""
    return _interface_rename_plan(
        module.device_id,
        module.pk,
        rule,
        variables,
        interface,
        bases.base_for(interface.name),
    )


def plan_device_interface_rename(device, rule, variables, interface, children=()) -> InstalledFamilyPlan:
    """Return the plan that renames one device-level interface family."""
    if not children and not is_channelized_parent(interface):
        return _interface_rename_plan(device.pk, None, rule, variables, interface, interface.name)
    # A device rule never builds a family, so its channel count says nothing about this one: the
    # members keep the suffixes they carry under whatever name the parent takes.  A device-level
    # interface has no module template family, so there is no suffix to recover from one either.
    targets = lockstep_family_targets(
        rule, variables, interface.name, interface.name, tuple((child.name, child.channel_id) for child in children), {}
    )
    return _channelized_plan(device.pk, None, interface, children, targets)


def module_raw_bases(
    module, rule, variables, interfaces, previous_forms=None, catalog=None, earlier_raw_names=None
) -> RawBases:
    """Return the claim over the names of *module*'s interfaces outside a channel.

    *previous_forms* holds what named the templates before a move, and *earlier_raw_names* the raw
    names the templates gave earlier; see ``RawBases``. *catalog* has ``get()`` for the resolved
    templates, and reads the module's templates when it is not given.
    """
    families = {
        interface.name: tuple((child.name, child.channel_id) for child in children)
        for interface, children in device_interface_families(interfaces)
    }
    plain = [interface.name for interface in interfaces if is_plain_interface(interface)]
    catalog = TemplateNames(module) if catalog is None else catalog
    return RawBases(module, rule, variables, families, plain, catalog, previous_forms, earlier_raw_names)


def given_raw_names(module, rule, variables, names) -> GivenRawNames:
    """Return bases for *names* a caller gives as the module's raw template names, without channels."""
    return GivenRawNames(RawBases(module, rule, variables, dict.fromkeys(names, ()), names, TemplateNames(module)))


def plan_installed_flat_families(module, rule, variables, interfaces, bases) -> list[InstalledFamilyPlan]:
    """Return a plan for each flat family that one template alone claims, when *rule* is a flat breakout rule.

    A move recognises no flat family (ADR 0015).
    """
    if not builds_flat_family(rule):
        return []
    plain = {interface.name: interface for interface in interfaces if is_plain_interface(interface)}
    plans = (
        _claimed_flat_plan(
            module, rule, variables, family, tuple(plain[name] for name in family.names if name in plain)
        )
        for family in bases.flat_families()
    )
    return [plan for plan in plans if plan is not None]


def half_built_members(rule, interfaces, bases) -> dict:
    """Return, by the name of its first row, the other rows of each half-built flat family the claim accepted."""
    if not builds_flat_family(rule):
        return {}
    rows = {interface.name: interface for interface in interfaces if is_plain_interface(interface)}
    return {
        family.names[0]: tuple(rows[name] for name in family.names[1:] if name in rows)
        for family in bases.flat_families()
    }


def plan_installed_families(module, rule, variables) -> InstalledFamilyPlanSet:
    """Return immutable plans for the installed families owned by *module*, reading its interfaces."""
    interfaces = list(Interface.objects.filter(module_id=module.pk).order_by("pk"))
    return plan_installed_families_from(
        module, rule, variables, interfaces, module_raw_bases(module, rule, variables, interfaces)
    )


def plan_installed_families_from(module, rule, variables, interfaces, bases) -> InstalledFamilyPlanSet:
    """Return the installed family plans for *module*, reading ``{base}`` and templates from *bases*.

    A batch that already holds the module's interface rows passes them in, so planning a fleet
    reads them once rather than once per module.
    """
    plans = _channelized_plans(module, rule, variables, interfaces, bases)
    plans.extend(plan_installed_flat_families(module, rule, variables, interfaces, bases))
    plans.sort(key=lambda plan: plan.member_pks[0])
    return InstalledFamilyPlanSet(module_id=module.pk, plans=tuple(plans))
