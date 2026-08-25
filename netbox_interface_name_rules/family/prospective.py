# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Plan interface families for interfaces described by name alone.

A prospective plan says what a rule intends, using the same naming rules installed execution
follows.  It carries no row snapshot, so it can describe a family NetBox has not created yet and
the executors refuse to take one.
"""

import logging
from dataclasses import dataclass

from ..choices import BreakoutModeChoices
from ..naming import evaluate_name_template
from .capabilities import supports_channelization
from .domain import (
    FamilyStatus,
    FamilyTopology,
    MemberRole,
    ProspectiveFamilyPlan,
    ProspectiveFamilyPlanSet,
    ProspectiveMember,
)
from .names import COLLISION_REASON
from .structural import UNSUPPORTED_REASON, has_flat_expansion
from .targets import channelized_family_names, channelized_family_targets, flat_family_names, template_channel_suffixes
from .template_names import resolved_template_names

logger = logging.getLogger(__name__)

FLAT_EXPANSION_REASON = "the module already carries a flat breakout family"


@dataclass(frozen=True, slots=True)
class ProspectiveInterface:
    """One interface a prospective plan reasons about, described without a row."""

    name: str
    parent_name: str | None = None
    channel_id: int | None = None
    channels: int | None = None


def describe_interfaces(interfaces) -> tuple[ProspectiveInterface, ...]:
    """Describe live interface rows for prospective planning."""
    names_by_pk = {interface.pk: interface.name for interface in interfaces}
    return tuple(
        ProspectiveInterface(
            name=interface.name,
            parent_name=names_by_pk.get(getattr(interface, "parent_id", None)),
            channel_id=getattr(interface, "channel_id", None),
            channels=getattr(interface, "channels", None),
        )
        for interface in interfaces
    )


def describe_template_interfaces(templates, names=()) -> tuple[ProspectiveInterface, ...]:
    """Describe the interfaces a module type's templates produce, plus any extra *names*.

    A channel is paired with its parent only where that parent declares a channel count, because a
    template family is what the parent's capacity defines.  A name no template resolves to is
    described as a plain interface.
    """
    parents = {template.pk: template for template in templates if template.channels is not None}
    described = {}
    for template in templates:
        parent = parents.get(template.parent_id) if template.channel_id is not None else None
        described[template.resolved] = ProspectiveInterface(
            name=template.resolved,
            parent_name=None if parent is None else parent.resolved,
            channel_id=None if parent is None else template.channel_id,
            channels=template.channels,
        )
    described.update(
        {name: ProspectiveInterface(name=name) for name in names if name not in described},
    )
    return tuple(described.values())


def describe_module_interfaces(module, names=()) -> tuple[ProspectiveInterface, ...]:
    """Describe the interfaces *module*'s templates produce, plus any extra *names*.

    A release that cannot model channelized families describes the names alone: its templates carry
    no family structure, so reading them would buy nothing.
    """
    if not supports_channelization():
        return tuple(ProspectiveInterface(name=name) for name in names)
    return describe_template_interfaces(  # pragma: no cover - requires channelization support
        resolved_template_names(module), names
    )


def _partition(interfaces):
    """Split described interfaces into ``(roots, children_by_parent_name)``.

    A channel is never an independent candidate: it belongs to the interface it names as its
    parent, and one whose parent is not described here is planned by nothing at all.
    """
    described = {interface.name for interface in interfaces}
    roots = []
    children: dict[str, list] = {}
    for interface in interfaces:
        if interface.channel_id is None:
            roots.append(interface)
        elif interface.parent_name in described:  # pragma: no cover - requires channelization support
            children.setdefault(interface.parent_name, []).append(interface)
    return roots, children


def _is_family_root(rule, interface, children):
    """Return whether *interface* owns a family the rule renames as a unit.

    A breakout rule renames only a family NetBox models with a channel count; on anything else it
    builds a new family from the interface itself.  Any other rule carries whatever channels the
    interface has along with it.
    """
    if rule.channel_count > 0:
        return interface.channels is not None
    return bool(children)


def _rename_plan(rule, variables, parent, children, suffixes):  # pragma: no cover - channelization only
    """Plan the rename of a family the templates or rows already describe."""
    children = sorted(children, key=lambda child: child.channel_id)
    targets = channelized_family_targets(
        rule,
        variables,
        parent.name,
        parent.channels,
        tuple((child.name, child.channel_id) for child in children),
        suffixes,
    )
    members = [ProspectiveMember(source_name=parent.name, target_name=targets.parent_name, role=MemberRole.PARENT)]
    members.extend(
        ProspectiveMember(
            # A channel whose target cannot be derived keeps the name it has.
            source_name=child.name,
            target_name=child.name if target is None else target,
            role=MemberRole.CHANNEL,
            channel_id=child.channel_id,
            reason=reason,
        )
        for child, (target, reason) in zip(children, targets.channels, strict=True)
    )
    return ProspectiveFamilyPlan(
        family_id=f"channelized:{parent.name}",
        topology=FamilyTopology.CHANNELIZED,
        base_name=None,
        members=tuple(members),
        precondition_status=targets.status,
        precondition_reason=targets.reason,
    )


def _refused_creation(base_name, topology, role, status, reason):
    """Plan a family that will not be built, so the base keeps the name it has."""
    return ProspectiveFamilyPlan(
        family_id=f"{topology}:{base_name}",
        topology=topology,
        base_name=base_name,
        members=(ProspectiveMember(source_name=base_name, target_name=base_name, role=role),),
        precondition_status=status,
        precondition_reason=reason,
    )


def _simple_plan(rule, variables, base_name):
    """Plan the rename of one interface that owns no family."""
    try:
        target_name = evaluate_name_template(rule.name_template, {**variables, "base": base_name})
    except (TypeError, ValueError) as error:
        return _refused_creation(
            base_name, FamilyTopology.FLAT, MemberRole.FLAT_MEMBER, FamilyStatus.FAILED, str(error)
        )
    return ProspectiveFamilyPlan(
        family_id=f"flat:{base_name}",
        topology=FamilyTopology.FLAT,
        base_name=base_name,
        members=(ProspectiveMember(source_name=base_name, target_name=target_name, role=MemberRole.FLAT_MEMBER),),
    )


def _flat_creation_plan(rule, variables, base_name):
    """Plan the flat sibling family a breakout rule creates on one plain interface."""
    try:
        target_names = flat_family_names(rule, variables, base_name)
    except (TypeError, ValueError) as error:
        return _refused_creation(
            base_name, FamilyTopology.FLAT, MemberRole.FLAT_MEMBER, FamilyStatus.FAILED, str(error)
        )
    members = tuple(
        ProspectiveMember(
            source_name=base_name if offset == 0 else None,
            target_name=target_name,
            role=MemberRole.FLAT_MEMBER,
        )
        for offset, target_name in enumerate(target_names)
    )
    return ProspectiveFamilyPlan(
        family_id=f"flat:{base_name}",
        topology=FamilyTopology.FLAT,
        base_name=base_name,
        members=members,
    )


def _structural_refusal(base_name, flat_expansion, taken_names, target_names):  # pragma: no cover - see below
    """Return why the planned family cannot be built here, or None when nothing refuses it."""
    if flat_expansion:
        # Converting one sibling into a parent would strand the others beside the new family.
        return FLAT_EXPANSION_REASON
    collisions = [name for name in target_names if name != base_name and name in taken_names]
    if collisions:
        return f"{COLLISION_REASON}: {collisions[0]}"
    return None


def _modelled_structural_plan(rule, variables, base_name, context):  # pragma: no cover - see below
    """Plan the channelized family for a NetBox release that can hold it."""
    try:
        parent_name, channels = channelized_family_names(rule, base_name, variables)
    except (TypeError, ValueError) as error:
        return _refused_creation(
            base_name, FamilyTopology.CHANNELIZED, MemberRole.PARENT, FamilyStatus.FAILED, str(error)
        )
    target_names = (parent_name, *(name for _channel_id, name in channels))
    refusal = _structural_refusal(base_name, context.flat_expansion, context.taken_names, target_names)
    if refusal is not None:
        return _refused_creation(
            base_name, FamilyTopology.CHANNELIZED, MemberRole.PARENT, FamilyStatus.BLOCKED, refusal
        )
    members = (
        ProspectiveMember(source_name=base_name, target_name=parent_name, role=MemberRole.PARENT),
        *(
            ProspectiveMember(source_name=None, target_name=name, role=MemberRole.CHANNEL, channel_id=channel_id)
            for channel_id, name in channels
        ),
    )
    return ProspectiveFamilyPlan(
        family_id=f"channelized:{base_name}",
        topology=FamilyTopology.CHANNELIZED,
        base_name=base_name,
        members=members,
    )


def _structural_plan(rule, variables, base_name, context):
    """Plan the channelized family a rule builds on one plain interface."""
    if not supports_channelization():
        return _refused_creation(
            base_name, FamilyTopology.CHANNELIZED, MemberRole.PARENT, FamilyStatus.UNSUPPORTED, UNSUPPORTED_REASON
        )
    return _modelled_structural_plan(rule, variables, base_name, context)  # pragma: no cover - see above


def _plain_plan(rule, variables, base_name, context):
    """Plan the family *rule* intends on one plain interface."""
    if rule.channel_count <= 0:
        return _simple_plan(rule, variables, base_name)
    if rule.breakout_mode == BreakoutModeChoices.CHANNELIZED:
        return _structural_plan(rule, variables, base_name, context)
    return _flat_creation_plan(rule, variables, base_name)


@dataclass(frozen=True, slots=True)
class _CreationContext:
    """The module-wide facts every creation plan on one module shares."""

    taken_names: frozenset
    flat_expansion: bool


def _creation_context(module, rule, interfaces, plain):
    """Read the module-wide facts once, and only where a creation plan can use them."""
    builds_channels = rule.channel_count > 0 and rule.breakout_mode == BreakoutModeChoices.CHANNELIZED
    flat_expansion = bool(plain) and builds_channels and supports_channelization() and has_flat_expansion(module)
    return _CreationContext(
        taken_names=frozenset(interface.name for interface in interfaces),
        flat_expansion=flat_expansion,
    )


class _TemplateSuffixes:
    """The module type's per-channel name suffixes, read only if a channel name needs recovering."""

    def __init__(self, module):
        self._module = module
        self._suffixes = None

    def get(self, channel_id, default=None):  # pragma: no cover - requires channelization support
        """Return the suffixes the templates give *channel_id*, reading them on first use."""
        if self._suffixes is None:
            self._suffixes = template_channel_suffixes(resolved_template_names(self._module))
        return self._suffixes.get(channel_id, default)


def plan_prospective_families(module, rule, variables, interfaces) -> ProspectiveFamilyPlanSet:
    """Return one plan for each family *rule* intends on the described *interfaces*.

    *interfaces* are ``ProspectiveInterface`` values, so a name NetBox has not created yet is
    planned exactly like one it has and no row is written.  Collision checking covers the names
    described here, because a prospective plan knows only the interfaces it was given.
    """
    roots, children = _partition(interfaces)
    families = [(root, children.get(root.name, ())) for root in roots]
    context = _creation_context(
        module, rule, interfaces, [root for root, family in families if not _is_family_root(rule, root, family)]
    )
    suffixes = _TemplateSuffixes(module)
    plans = tuple(
        _rename_plan(rule, variables, root, family, suffixes)
        if _is_family_root(rule, root, family)
        else _plain_plan(rule, variables, root.name, context)
        for root, family in families
    )
    return ProspectiveFamilyPlanSet(module_id=module.pk, plans=plans)
