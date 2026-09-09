# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Discover and plan installed interface families."""

import logging
import re

from dcim.models import Interface

from ..choices import BreakoutModeChoices
from ..naming import evaluate_name_template
from .domain import (
    FamilyStatus,
    FamilyTopology,
    InstalledFamilyPlan,
    InstalledFamilyPlanSet,
    InterfaceSnapshot,
    MemberRole,
    PlannedMember,
)
from .targets import (
    channelized_family_targets,
    flat_family_names,
    lockstep_family_targets,
    template_channel_suffixes,
)
from .template_names import resolved_template_names

logger = logging.getLogger(__name__)

_BASE_SENTINEL = "InrBaseSentinelEnd"


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


def _historical_bases(rule, variables, template, interfaces):  # pragma: no cover - requires VC token support
    """Return every historical base claimed by *template*."""
    if template.historical_pattern is None:
        return ()
    try:
        marked = evaluate_name_template(
            rule.name_template,
            {**variables, "base": _BASE_SENTINEL, "channel": str(rule.channel_start)},
        )
    except (TypeError, ValueError):
        return ()
    if _BASE_SENTINEL not in marked:
        return ()
    escaped = re.escape(marked)
    head, _, tail = escaped.partition(_BASE_SENTINEL)
    tail = tail.replace(_BASE_SENTINEL, "(?P=base)")
    pattern = re.compile(f"{head}(?P<base>{template.historical_pattern.pattern}){tail}")
    bases = {
        match.group("base") for interface in interfaces if (match := pattern.fullmatch(interface.name)) is not None
    }
    return tuple(sorted(bases))


def _ambiguous_bases(historical_by_template):
    """Return every historical base that more than one template could claim."""
    ambiguous = {base_name for bases in historical_by_template.values() if len(bases) > 1 for base_name in bases}
    single_claims: dict[str, int] = {}
    for bases in historical_by_template.values():
        if len(bases) == 1:  # pragma: no cover - historical matchers require VC token support
            single_claims[bases[0]] = single_claims.get(bases[0], 0) + 1
    ambiguous.update(base_name for base_name, count in single_claims.items() if count > 1)
    return ambiguous


def _source_bases(template, historical_bases, ambiguous_bases):
    """Return the template's own base and every historical base it alone claims."""
    unambiguous = tuple(
        base_name for base_name in historical_bases if len(historical_bases) == 1 and base_name not in ambiguous_bases
    )
    return (template.resolved, *unambiguous)


def flat_family_bases(rule, variables, interfaces, catalog):
    """Return ``(template base, source base)`` for every base a flat family could be named from.

    The template base is the name the rule resolves for this module now; the source base is the one
    an installed family still spells, which differs after a virtual-chassis renumber.  A historical
    base more than one template could claim is dropped: the rows it names are not certainly one
    family's, and neither renaming nor converting them is this plugin's guess to make.
    """
    if rule.channel_count <= 0:
        return ()
    templates = catalog.get()
    historical_by_template = {
        template.pk: _historical_bases(rule, variables, template, interfaces) for template in templates
    }
    ambiguous_bases = _ambiguous_bases(historical_by_template)
    return tuple(
        (template.resolved, source_base)
        for template in templates
        for source_base in _source_bases(template, historical_by_template[template.pk], ambiguous_bases)
    )


def family_names_for(rule, variables, base_name, source_base):
    """Return the names the rule intends for the family and the names it still spells, or None."""
    try:
        target_names = flat_family_names(rule, variables, base_name)
        source_names = flat_family_names(rule, variables, source_base)
    except (TypeError, ValueError):
        return None
    if len(set(source_names)) != len(source_names):
        return None
    return target_names, source_names


def _singly_claimed(candidates):
    """Return only the candidates whose members no other candidate also claims."""
    claims: dict[int, int] = {}
    for _base_name, _names, members in candidates:
        for member in members:
            claims[member.pk] = claims.get(member.pk, 0) + 1
    return [candidate for candidate in candidates if all(claims[member.pk] == 1 for member in candidate[2])]


def flat_family_candidates(rule, variables, interfaces, catalog):
    """Return complete, unambiguous flat-family candidates on this module.

    A flat family carries the names the rule's channel range spells, and a flat rule and the
    channelized rule it later became spell those identically, so the caller decides whether the
    rule's current breakout mode makes these families its own to rename or its own to convert.
    """
    by_name = {interface.name: interface for interface in interfaces if is_plain_interface(interface)}
    if not by_name:
        return []
    candidates = []
    for base_name, source_base in flat_family_bases(rule, variables, interfaces, catalog):
        names = family_names_for(rule, variables, base_name, source_base)
        if names is None:
            continue
        target_names, source_names = names
        if not all(name in by_name for name in source_names):
            continue
        candidate = (base_name, target_names, tuple(by_name[name] for name in source_names))
        if candidate not in candidates:  # pragma: no branch - duplicates require historical matchers
            candidates.append(candidate)
    return _singly_claimed(candidates)


def _flat_candidates(rule, variables, interfaces, catalog):
    """Return the flat families a flat-mode rule owns on this module."""
    if rule.breakout_mode != BreakoutModeChoices.FLAT:
        return []
    return flat_family_candidates(rule, variables, interfaces, catalog)


def _flat_plan(module, target_names, interfaces):
    """Build one immutable plan from a complete flat-family candidate."""
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
    )


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


def _module_family_targets(rule, variables, parent, children, suffixes):  # pragma: no cover
    """Return the names *rule* intends for a channelized family a module carries."""
    return channelized_family_targets(
        rule,
        variables,
        parent.name,
        parent.channels,
        tuple((child.name, child.channel_id) for child in children),
        suffixes,
    )


def _channelized_plans(module, rule, variables, interfaces, catalog):  # pragma: no cover
    """Return one plan for every structurally discovered channelized family."""
    parents = [interface for interface in interfaces if is_channelized_parent(interface)]
    if not parents:
        return []
    children_by_parent: dict[int, list] = {}
    for interface in interfaces:
        if _is_channel(interface) and interface.parent_id is not None:
            children_by_parent.setdefault(interface.parent_id, []).append(interface)
    suffixes = template_channel_suffixes(catalog.get())
    plans = []
    for parent in parents:
        children = children_by_parent.get(parent.pk, [])
        children.sort(key=lambda child: (child.channel_id, child.pk))
        targets = _module_family_targets(rule, variables, parent, children, suffixes)
        plans.append(_channelized_plan(module.device_id, module.pk, parent, children, targets))
    return plans


class TemplateNames:
    """The module type's resolved template names, read only where a plan needs them."""

    def __init__(self, module):
        self._module = module
        self._templates = None

    def get(self):
        """Return every resolved template name for the module, loading them once."""
        if self._templates is None:
            self._templates = resolved_template_names(self._module)
        return self._templates


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
    """Return each device-level base interface with its channel children."""
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


def _interface_rename_plan(device_id, module_id, rule, variables, interface) -> InstalledFamilyPlan:
    """Return a plan that renames one interface which belongs to no family."""
    status, reason, target_name = None, "", interface.name
    try:
        target_name = evaluate_name_template(rule.name_template, {**variables, "base": interface.name})
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


def plan_interface_rename(module, rule, variables, interface) -> InstalledFamilyPlan:
    """Return the plan that renames one interface which belongs to no family."""
    return _interface_rename_plan(
        module.device_id,
        module.pk,
        rule,
        variables,
        interface,
    )


def plan_device_interface_rename(device, rule, variables, interface, children=()) -> InstalledFamilyPlan:
    """Return the plan that renames one device-level interface family."""
    if not children and not is_channelized_parent(interface):
        return _interface_rename_plan(device.pk, None, rule, variables, interface)
    # A device rule never builds a family, so its channel count says nothing about this one: the
    # members keep the suffixes they carry under whatever name the parent takes.  A device-level
    # interface has no module template family, so there is no suffix to recover from one either.
    targets = lockstep_family_targets(
        rule, variables, interface.name, tuple((child.name, child.channel_id) for child in children), {}
    )
    return _channelized_plan(device.pk, None, interface, children, targets)


def plan_installed_families(module, rule, variables, interfaces=None) -> InstalledFamilyPlanSet:
    """Return immutable plans for the installed families owned by *module*.

    A batch that already holds the module's interface rows passes them in, so planning a fleet
    reads them once rather than once per module.
    """
    if interfaces is None:
        interfaces = list(Interface.objects.filter(module_id=module.pk).order_by("pk"))
    catalog = TemplateNames(module)
    plans = _channelized_plans(module, rule, variables, interfaces, catalog)
    plans.extend(
        _flat_plan(module, target_names, members)
        for _base_name, target_names, members in _flat_candidates(rule, variables, interfaces, catalog)
    )
    plans.sort(key=lambda plan: plan.member_pks[0])
    return InstalledFamilyPlanSet(module_id=module.pk, plans=tuple(plans))
