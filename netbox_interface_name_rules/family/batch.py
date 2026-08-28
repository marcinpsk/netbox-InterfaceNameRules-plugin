# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Apply one rule to a batch of installed modules through family plans.

Every module in the batch is planned family by family and executed family by family, so a module
that cannot take its names costs the batch only that family.  The module rows, their interfaces and
each module type's templates are read once for the whole batch.
"""

import logging
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from ..naming import build_variables
from .domain import (
    FamilyOutcome,
    FamilyStatus,
    FamilyTopology,
    FlatCreationPlan,
    InstalledFamilyPlan,
    MemberOutcome,
    StructuralFamilyPlan,
)
from .execution import execute_installed_plan
from .installed import interfaces_by_module, plan_installed_families, plan_interface_rename
from .structural import execute_flat_family, execute_structural_family, plan_flat_family, plan_structural_family
from .targets import builds_channelized_family, intended_family_names, one_family_per_name_set
from .template_names import pinned_template_cache

logger = logging.getLogger(__name__)

# A member left with the name it had for a reason the operator can act on.  An unsupported topology
# is not one of them: the release cannot hold the family, so nothing was dropped by this batch.
_SKIPPED_STATUSES = (FamilyStatus.BLOCKED, FamilyStatus.STALE, FamilyStatus.FAILED)

_EXECUTORS = {
    InstalledFamilyPlan: execute_installed_plan,
    StructuralFamilyPlan: execute_structural_family,
    FlatCreationPlan: execute_flat_family,
}


@dataclass(frozen=True, slots=True)
class ModuleFamilyPlans:
    """The families a rule intends on one module, split by how each was found.

    *installed* are the families the module already carries; *leftover* are the plans for the
    interfaces no installed family claimed, whether the rule renames one or builds a family on it.
    """

    installed: tuple
    leftover: tuple

    @property
    def plans(self) -> tuple:
        """Return every plan, installed families first."""
        return (*self.installed, *self.leftover)


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Every family one batch operation planned, and what happened to it."""

    families: tuple[FamilyOutcome, ...]

    @property
    def changed_count(self) -> int:
        """Return the number of interfaces the batch renamed or created."""
        return sum(family.changed_count for family in self.families)

    @property
    def skipped_members(self) -> tuple[MemberOutcome, ...]:
        """Return every member a collision, a stale plan or a failure left as it was."""
        return tuple(
            member for family in self.families for member in family.members if member.status in _SKIPPED_STATUSES
        )

    @property
    def changed_families(self) -> tuple[FamilyOutcome, ...]:
        """Return every family this batch actually rewrote."""
        return tuple(family for family in self.families if family.status == FamilyStatus.CHANGED)

    @property
    def blocked_families(self) -> tuple[FamilyOutcome, ...]:
        """Return every family a collision, a stale plan or a failure left as it was."""
        return tuple(family for family in self.families if family.status in _SKIPPED_STATUSES)


def execute_family_plan(plan) -> FamilyOutcome:
    """Execute one planned family through the executor its plan kind owns.

    A plan carrying no live rows (a prospective plan above all) has no executor, so a preview
    object is refused here rather than locking anything.
    """
    executor = _EXECUTORS.get(type(plan))
    if executor is None:
        raise TypeError(f"{type(plan).__name__} is not an executable family plan")
    return executor(plan)


def _is_channel(interface) -> bool:
    """Return whether *interface* is bound to a parent channel."""
    return getattr(interface, "channel_id", None) is not None


def _creation_plan(module, rule, variables, base):
    """Return the plan that builds the family *rule* describes on one plain interface."""
    if builds_channelized_family(rule):
        return plan_structural_family(module, rule, variables, base)
    return plan_flat_family(module, rule, variables, base)


def _creation_plans(module, rule, variables, plain):
    """Return one creation plan per family, so two bases of one family never build it twice."""
    candidates = [(base, intended_family_names(rule, variables, base.name)) for base in plain]
    kept = one_family_per_name_set([(base.name, target_names) for base, target_names in candidates])
    return [_creation_plan(module, rule, variables, candidates[index][0]) for index in kept]


def plan_module_families(module, rule, variables, interfaces) -> ModuleFamilyPlans:
    """Return one executable plan for every family *rule* intends on *module*.

    Every interface belongs to at most one plan: an installed family claims its members first, and
    what is left over is planned as the family the rule would build on it.
    """
    installed = plan_installed_families(module, rule, variables, interfaces=interfaces)
    claimed = installed.member_pks
    plain = [interface for interface in interfaces if interface.pk not in claimed and not _is_channel(interface)]
    if rule.channel_count <= 0:
        leftover = tuple(plan_interface_rename(module, rule, variables, interface) for interface in plain)
    elif any(plan.topology == FamilyTopology.CHANNELIZED for plan in installed.plans):
        # A breakout rule renames the families the module already models; it never adds one beside them.
        leftover = ()  # pragma: no cover - requires channelization support
        for interface in plain:  # pragma: no cover - see above
            logger.debug(
                "Interface %r is not channelized; skipping it while rule '%s' breaks out this module's families.",
                interface.name,
                rule,
            )
    else:
        leftover = tuple(_creation_plans(module, rule, variables, plain))
    return ModuleFamilyPlans(installed=installed.plans, leftover=leftover)


def _selection_pks(plan):
    """Return the interface primary keys a selection reaches this family through.

    A channelized family is submitted through its parent alone, because a channel is not an
    independent candidate on any path.
    """
    if isinstance(plan, InstalledFamilyPlan):
        if plan.parent_pk is None:
            return plan.member_pks
        return (plan.parent_pk,)  # pragma: no cover - requires channelization support
    return (plan.base.pk,)


def _selected(plans, selected_pks):
    """Return the plans the operator's interface selection reaches."""
    if selected_pks is None:
        return plans
    return [plan for plan in plans if selected_pks.intersection(_selection_pks(plan))]


def execute_module_families(rule, module, plans):
    """Execute each planned family in order, keeping the ones after a failure."""
    return [outcome for plan in plans if (outcome := _execute(rule, module, plan)) is not None]


def _execute(rule, module, plan):
    """Execute one family, logging (never raising) so the batch keeps its later families."""
    try:
        return execute_family_plan(plan)
    except (ValueError, ValidationError, IntegrityError):
        logger.exception(
            "Failed to apply rule '%s' to a family on module '%s' (id=%s); skipping.", rule, module, module.pk
        )
        return None


def _apply_module(rule, module, interfaces, selected_pks):
    """Plan and execute every selected family on one module."""
    variables = build_variables(module.module_bay, device=module.device)
    plans = _selected(plan_module_families(module, rule, variables, interfaces).plans, selected_pks)
    return execute_module_families(rule, module, plans)


def apply_rule_to_modules(rule, modules, selected_pks=None, limit=None) -> BatchOutcome:
    """Apply *rule* to every module in *modules*, one family at a time.

    *selected_pks* limits the batch to the families those interfaces reach; *limit* stops it after
    the module that reached that many changed interfaces.
    """
    if not modules:
        return BatchOutcome(families=())
    by_module = interfaces_by_module(modules)
    families: list[FamilyOutcome] = []
    changed = 0
    with pinned_template_cache(modules):
        for module in modules:
            outcomes = _apply_module(rule, module, by_module[module.pk], selected_pks)
            families.extend(outcomes)
            changed += sum(outcome.changed_count for outcome in outcomes)
            if limit is not None and changed >= limit:
                break
    return BatchOutcome(families=tuple(families))
