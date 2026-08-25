# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Discover and plan installed interface families."""

import logging
import re

from dcim.models import Interface
from django.db import DEFAULT_DB_ALIAS

from ..choices import BreakoutModeChoices
from ..naming import evaluate_name_template
from .domain import (
    FamilyTopology,
    InstalledFamilyPlan,
    InstalledFamilyPlanSet,
    InterfaceSnapshot,
    MemberRole,
    PlannedMember,
)
from .targets import channelized_family_targets, flat_family_names, template_channel_suffixes
from .template_names import resolved_template_names

logger = logging.getLogger(__name__)

_BASE_SENTINEL = "InrBaseSentinelEnd"


def module_db_alias(module) -> str:
    """Return the database alias that *module* was loaded from."""
    return module._state.db or DEFAULT_DB_ALIAS


def _is_plain_interface(interface) -> bool:
    """Return whether *interface* can be a flat-family member."""
    return (
        getattr(interface, "parent_id", None) is None
        and getattr(interface, "channel_id", None) is None
        and getattr(interface, "channels", None) is None
    )


def _is_channelized_parent(interface) -> bool:  # pragma: no cover - requires channelization support
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


def _template_candidates(rule, variables, by_name, template, source_bases):
    """Return every complete flat family that *template* recovers from *source_bases*."""
    try:
        target_names = flat_family_names(rule, variables, template.resolved)
    except (TypeError, ValueError):
        return []
    candidates = []
    for source_base in source_bases:
        try:
            source_names = flat_family_names(rule, variables, source_base)
        except (TypeError, ValueError):
            continue
        if len(set(source_names)) != len(source_names) or not all(name in by_name for name in source_names):
            continue
        candidates.append((template.resolved, target_names, tuple(by_name[name] for name in source_names)))
    return candidates


def _singly_claimed(candidates):
    """Return only the candidates whose members no other candidate also claims."""
    claims: dict[int, int] = {}
    for _base_name, _names, members in candidates:
        for member in members:
            claims[member.pk] = claims.get(member.pk, 0) + 1
    return [candidate for candidate in candidates if all(claims[member.pk] == 1 for member in candidate[2])]


def _flat_candidates(rule, variables, interfaces, templates):
    """Return complete, unambiguous flat-family candidates for *module*."""
    if rule.channel_count <= 0 or rule.breakout_mode != BreakoutModeChoices.FLAT:
        return []

    by_name = {interface.name: interface for interface in interfaces if _is_plain_interface(interface)}
    if not by_name:
        return []
    historical_by_template = {
        template.pk: _historical_bases(rule, variables, template, interfaces) for template in templates
    }
    ambiguous_bases = _ambiguous_bases(historical_by_template)

    candidates = []
    for template in templates:
        source_bases = _source_bases(template, historical_by_template[template.pk], ambiguous_bases)
        for candidate in _template_candidates(rule, variables, by_name, template, source_bases):
            if candidate not in candidates:  # pragma: no branch - duplicates require historical matchers
                candidates.append(candidate)
    return _singly_claimed(candidates)


def _flat_plan(module, db_alias, target_names, interfaces):
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
        db_alias=db_alias,
        members=members,
    )


def _channelized_plan(module, rule, variables, db_alias, parent, children, suffixes):  # pragma: no cover
    """Build one plan for an existing channelized family."""
    targets = channelized_family_targets(
        rule,
        variables,
        parent.name,
        parent.channels,
        tuple((child.name, child.channel_id) for child in children),
        suffixes,
    )
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
        device_id=module.device_id,
        module_id=module.pk,
        db_alias=db_alias,
        members=tuple(members),
        parent_pk=parent.pk,
        precondition_status=targets.status,
        precondition_reason=targets.reason,
    )


def _channelized_plans(module, rule, variables, db_alias, interfaces, templates):  # pragma: no cover
    """Return one plan for every structurally discovered channelized family."""
    parents = [interface for interface in interfaces if _is_channelized_parent(interface)]
    children_by_parent: dict[int, list] = {}
    for interface in interfaces:
        if _is_channel(interface) and interface.parent_id is not None:
            children_by_parent.setdefault(interface.parent_id, []).append(interface)
    suffixes = template_channel_suffixes(templates)
    plans = []
    for parent in parents:
        children = children_by_parent.get(parent.pk, [])
        children.sort(key=lambda child: (child.channel_id, child.pk))
        plans.append(_channelized_plan(module, rule, variables, db_alias, parent, children, suffixes))
    return plans


def plan_installed_families(module, rule, variables) -> InstalledFamilyPlanSet:
    """Return immutable plans for the installed families owned by *module*."""
    db_alias = module_db_alias(module)
    interfaces = list(Interface.objects.using(db_alias).filter(module_id=module.pk).order_by("pk"))
    templates = resolved_template_names(module)
    plans = _channelized_plans(module, rule, variables, db_alias, interfaces, templates)
    plans.extend(
        _flat_plan(module, db_alias, target_names, members)
        for _base_name, target_names, members in _flat_candidates(rule, variables, interfaces, templates)
    )
    plans.sort(key=lambda plan: plan.member_pks[0])
    return InstalledFamilyPlanSet(module_id=module.pk, plans=tuple(plans))
