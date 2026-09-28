# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Discover and plan installed interface families."""

import logging
import re
from typing import NamedTuple

from dcim.models import Interface

from ..choices import BreakoutModeChoices
from ..name_template import evaluate_name_template
from .claims import TemplateClaim, resolve_template_claims
from .domain import (
    FamilyStatus,
    FamilyTopology,
    InstalledFamilyPlan,
    InstalledFamilyPlanSet,
    InterfaceSnapshot,
    MemberRole,
    PlannedMember,
)
from .raw_bases import BASE_MARKER, GivenRawNames, RawBases
from .targets import (
    UNCLAIMED_BASE_REASON,
    channelized_family_targets,
    flat_family_names,
    lockstep_family_targets,
    template_channel_suffixes,
)
from .template_names import TemplateNames

logger = logging.getLogger(__name__)


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
            {**variables, "base": BASE_MARKER, "channel": str(rule.channel_start)},
        )
    except (TypeError, ValueError):
        return ()
    if BASE_MARKER not in marked:
        return ()
    head, _, tail = re.escape(marked).partition(re.escape(BASE_MARKER))
    tail = tail.replace(re.escape(BASE_MARKER), "(?P=base)")
    pattern = re.compile(f"{head}(?P<base>{template.historical_pattern.pattern}){tail}")
    bases = {
        match.group("base") for interface in interfaces if (match := pattern.fullmatch(interface.name)) is not None
    }
    return tuple(sorted(bases))


def flat_family_bases(module, rule, variables, interfaces, catalog):
    """Return ``(template base, source base)`` for every base a flat family could be named from.

    See ``_template_flat_family_bases``, which also names the template of each base.
    """
    return tuple(
        (base_name, source_base)
        for _template, base_name, source_base in _template_flat_family_bases(
            module, rule, variables, interfaces, catalog
        )
    )


def _template_flat_family_bases(module, rule, variables, interfaces, catalog):
    """Return ``(template, template base, source base)`` for every base a flat family could be named from.

    The template base is the name the rule resolves for this module now; the source base is the one
    an installed family still spells, which differs after a virtual-chassis renumber.
    A historical base that more than one template could claim is dropped.
    Every historical base of a template that claims multiple bases is also dropped.
    These claims do not identify one family with certainty, so the plugin does not rename or convert them.
    A historical base that some template resolves to now is dropped: the exact claim owns those
    rows, and keeping both would make the two claims cancel each other.
    """
    if rule.channel_count <= 0:
        return ()
    templates = catalog.get()
    historical_by_template = {
        template.pk: _historical_bases(rule, variables, template, interfaces) for template in templates
    }
    accepted, messages = resolve_template_claims(
        tuple(
            TemplateClaim(template.pk, template.template_name, historical_by_template[template.pk])
            for template in templates
        ),
        module=module,
        label_kind="family base",
    )
    for message in messages:
        logger.warning(message)
    accepted_by_template = {pk: (base,) for pk, base in accepted}
    current_bases = tuple((template, template.resolved, template.resolved) for template in templates)
    resolved_now = {template.resolved for template in templates}
    historical_bases = tuple(
        (template, template.resolved, source_base)
        for template in templates
        for source_base in accepted_by_template.get(template.pk, ())
        if source_base not in resolved_now
    )
    return current_bases + historical_bases


def family_names_for(rule, variables, base_name, source_base, source=None):
    """Return the names the rule intends for the family and the names it still spells, or None.

    *source* is the ``(rule, variables)`` pair that spelled the family, when not *rule* with *variables*.
    """
    source_rule, source_variables = source or (rule, variables)
    try:
        target_names = flat_family_names(rule, variables, base_name)
        source_names = flat_family_names(source_rule, source_variables, source_base)
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


class FlatFamily(NamedTuple):
    """One complete flat family found on a module, and the template it was found for."""

    template_pk: int
    base_name: str
    target_names: tuple[str, ...]
    members: tuple


def _previous_family_bases(catalog, previous_forms):
    """Return ``(template, template base, source base, source)`` for each flat family the previous rule named."""
    rule = None if previous_forms is None else previous_forms.rule
    if rule is None or rule.channel_count <= 0 or rule.breakout_mode != BreakoutModeChoices.FLAT:
        return ()
    return tuple(
        (template, template.resolved, previous_forms.templates[template.pk].resolved, (rule, previous_forms.variables))
        for template in catalog.get()
        if template.pk in previous_forms.templates
    )


def _template_flat_families(module, rule, variables, interfaces, catalog, previous_forms=None):
    """Return every complete flat family on this module that a template's bases spell, once each.

    *previous_forms* adds the families a flat rule named in the state before a move.
    """
    by_name = {interface.name: interface for interface in interfaces if is_plain_interface(interface)}
    if not by_name:
        return []
    bases = [
        (template, base_name, source_base, None)
        for template, base_name, source_base in _template_flat_family_bases(
            module, rule, variables, interfaces, catalog
        )
    ]
    bases.extend(_previous_family_bases(catalog, previous_forms))
    families = []
    for template, base_name, source_base, source in bases:
        names = family_names_for(rule, variables, base_name, source_base, source)
        if names is None:
            continue
        target_names, source_names = names
        if not all(name in by_name for name in source_names):
            continue
        family = FlatFamily(template.pk, base_name, target_names, tuple(by_name[name] for name in source_names))
        if family not in families:
            families.append(family)
    return families


def flat_family_candidates(module, rule, variables, interfaces, catalog):
    """Return complete, unambiguous flat-family candidates on this module.

    A flat family carries the names the rule's channel range spells, and a flat rule and the
    channelized rule it later became spell those identically, so the caller decides whether the
    rule's current breakout mode makes these families its own to rename or its own to convert.
    """
    families = _template_flat_families(module, rule, variables, interfaces, catalog)
    candidates = []
    for family in families:
        candidate = (family.base_name, family.target_names, family.members)
        if candidate not in candidates:  # pragma: no branch - duplicates require historical matchers
            candidates.append(candidate)
    return _singly_claimed(candidates)


def _flat_candidates(module, rule, variables, interfaces, bases):
    """Return the flat families a flat-mode rule owns on this module.

    After a move, a family is renamed only when its template alone claims every one of its members.
    """
    if rule.breakout_mode != BreakoutModeChoices.FLAT:
        return []
    if bases.previous_forms is None:
        return flat_family_candidates(module, rule, variables, interfaces, bases.catalog)
    return [
        (family.base_name, family.target_names, family.members)
        for family in bases.flat_families
        if all(bases.claimant_for(member.name) == family.template_pk for member in family.members)
    ]


def _flat_plan(module, target_names, interfaces):
    """Build one immutable plan from a complete flat-family candidate.

    A family that a move brings under a rule with another channel count keeps its names, blocked.
    """
    status, reason = None, ""
    if len(target_names) != len(interfaces):
        status = FamilyStatus.BLOCKED
        reason = f"installed family has {len(interfaces)} channels but the rule defines {len(target_names)}"
        target_names = tuple(interface.name for interface in interfaces)
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


def module_raw_bases(module, rule, variables, interfaces, previous_forms=None) -> RawBases:
    """Return the raw template name behind each of *module*'s interfaces outside a channel.

    *previous_forms* holds what named the templates before a move; see ``RawBases``. A flat-mode rule
    then also claims the flat families found for each template.
    """
    families = {
        interface.name: tuple((child.name, child.channel_id) for child in children)
        for interface, children in device_interface_families(interfaces)
    }
    catalog = TemplateNames(module)
    flat_families = ()
    if previous_forms is not None and rule.channel_count > 0 and rule.breakout_mode == BreakoutModeChoices.FLAT:
        flat_families = _template_flat_families(module, rule, variables, interfaces, catalog, previous_forms)
    return RawBases(module, rule, variables, families, catalog, previous_forms, flat_families)


def given_raw_names(module, rule, variables, names) -> GivenRawNames:
    """Return bases for *names* a caller gives as the module's raw template names, without channels."""
    return GivenRawNames(RawBases(module, rule, variables, dict.fromkeys(names, ()), TemplateNames(module)))


def plan_installed_flat_families(module, rule, variables, interfaces, bases) -> list[InstalledFamilyPlan]:
    """Return a plan for every installed flat family a flat-mode rule renames on *module*."""
    return [
        _flat_plan(module, target_names, members)
        for _base_name, target_names, members in _flat_candidates(module, rule, variables, interfaces, bases)
    ]


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
