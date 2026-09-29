# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Apply one rule to a batch of installed modules through family plans.

Every module in the batch is planned family by family and executed family by family, so a module
that cannot take its names costs the batch only that family.  The module rows, their interfaces and
each module type's templates are read once for the whole batch.
"""

import logging
from dataclasses import dataclass

from ..naming import build_variables
from .domain import (
    FamilyOutcome,
    FamilyStatus,
    FamilyTopology,
    FlatCreationPlan,
    InstalledFamilyPlan,
    MemberOutcome,
    RunScope,
    StructuralFamilyPlan,
)
from .execution import execute_installed_plan
from .installed import (
    half_built_members,
    interfaces_by_module,
    module_raw_bases,
    plan_installed_families_from,
    plan_interface_rename,
    plan_kept_interface,
)
from .names import first_taken_name, name_owners
from .structural import (
    carries_flat_expansion,
    execute_flat_family,
    execute_structural_family,
    plan_flat_family,
    plan_structural_family,
)
from .targets import (
    UNCLAIMED_BASE_REASON,
    breaks_out,
    builds_channelized_family,
)
from .template_names import pinned_template_cache

logger = logging.getLogger(__name__)

NOT_RENAMED_REASON = "the module is not renamed while one of its interfaces is unclaimed"
CHANNELIZED_MODULE_REASON = "the module already models channelized families, so no family is added beside them"

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


def _is_top_level(interface) -> bool:
    """Return whether *interface* is neither a channel nor a subinterface of another interface."""
    return not _is_channel(interface) and getattr(interface, "parent_id", None) is None


def _creation_plan(module, rule, variables, base, base_name, flat_expansion, members):
    """Return the plan that builds the family *rule* describes on one plain interface."""
    if builds_channelized_family(rule):
        return plan_structural_family(module, rule, variables, base, base_name, flat_expansion)
    return plan_flat_family(module, rule, variables, base, base_name, members)


def _creation_plans(module, rule, variables, plain, bases, selected_pks, flat_expansion, members):
    """Return a plan for each selected interface; a row that a half-built family keeps is in that family's plan."""
    plans = [
        _creation_plan(
            module, rule, variables, base, bases.builds_on(base.name), flat_expansion, members.get(base.name, ())
        )
        for base in plain
    ]
    plans = [plan for plan in plans if _reaches(plan, selected_pks)]
    kept = {member.pk for plan in plans if isinstance(plan, FlatCreationPlan) for member in plan.members}
    return [plan for plan in plans if plan.base.pk not in kept]


def _is_candidate(interface, rule, bases, previous_forms):
    """Return whether a leftover *interface* is a candidate of its own; a subinterface is not always one."""
    if _is_top_level(interface):
        return True
    # After a move, or under a breakout rule unless a template claims it, a subinterface belongs to its parent.
    return previous_forms is None and not (breaks_out(rule) and not bases.claim(interface.name).claimed)


def _in_scope_installed(plans, bases, scope):
    """Return the installed families *scope* reaches: a channelized one always, a flat one on install by a raw name."""
    if scope == RunScope.FORCED:
        return plans
    return tuple(
        plan
        for plan in plans
        if plan.topology == FamilyTopology.CHANNELIZED
        or any(bases.claim(member.snapshot.name).raw for member in plan.members)
    )


def plan_module_families(
    module, rule, variables, interfaces, bases, scope=None, selected_pks=None
) -> ModuleFamilyPlans:
    """Return one plan for every family *rule* intends on *module* that *selected_pks* reaches.

    A *scope* limits an automatic run before interfaces that intend one family collapse (ADR 0013).
    """
    previous_forms = bases.previous_forms
    if previous_forms is not None and any(
        _is_top_level(interface) and bases.base_for(interface.name) is None for interface in interfaces
    ):
        # An unclaimed interface may belong to a family, so its module keeps every name.
        return ModuleFamilyPlans(
            installed=(),
            leftover=tuple(
                plan_interface_rename(module, rule, variables, interface, bases)
                if _is_top_level(interface) and bases.base_for(interface.name) is None
                else plan_kept_interface(module, interface, NOT_RENAMED_REASON)
                for interface in interfaces
            ),
        )
    installed = plan_installed_families_from(module, rule, variables, interfaces, bases)
    claimed = installed.member_pks
    plain = [
        interface
        for interface in interfaces
        if interface.pk not in claimed
        and not _is_channel(interface)
        and _is_candidate(interface, rule, bases, previous_forms)
        and (scope != RunScope.INSTALL or bases.claim(interface.name).raw)
    ]
    if not breaks_out(rule):
        leftover = _selected(
            [plan_interface_rename(module, rule, variables, interface, bases) for interface in plain], selected_pks
        )
    elif any(plan.topology == FamilyTopology.CHANNELIZED for plan in installed.plans):
        # A breakout rule renames these families and adds none beside them; it reports a claimed one when selected.
        leftover = _selected(  # pragma: no cover - requires channelization support
            [
                plan_kept_interface(
                    module,
                    interface,
                    UNCLAIMED_BASE_REASON if bases.builds_on(interface.name) is None else CHANNELIZED_MODULE_REASON,
                )
                for interface in plain
                if selected_pks is not None or bases.builds_on(interface.name) is None
            ],
            selected_pks,
        )
        for interface in plain:  # pragma: no cover - see above
            logger.debug(
                "Interface %r is not channelized; skipping it while rule '%s' breaks out this module's families.",
                interface.name,
                rule,
            )
    else:
        flat_expansion = builds_channelized_family(rule) and carries_flat_expansion(interfaces, bases.catalog.get())
        members = half_built_members(rule, interfaces, bases)
        leftover = _creation_plans(module, rule, variables, plain, bases, selected_pks, flat_expansion, members)
    installed_plans = installed.plans if scope is None else _in_scope_installed(installed.plans, bases, scope)
    return ModuleFamilyPlans(installed=tuple(_selected(installed_plans, selected_pks)), leftover=tuple(leftover))


def creation_names_in_use(module, rule, interfaces, bases, plans) -> dict[str, str]:
    """Return, by base name, the first name in use on the device outside the rows each creation in *plans* keeps.

    *plans* are prospective plans. Each creation is checked as its executor checks it at execution,
    so a preview offers no family that the apply refuses because one of its names is in use.
    """
    creations = [plan for plan in plans if plan.base_name is not None and plan.precondition_status is None]
    if not breaks_out(rule) or not creations:
        return {}
    pks = {interface.name: interface.pk for interface in interfaces}
    members = half_built_members(rule, interfaces, bases)
    owners = name_owners(module.device_id, {name for plan in creations for name in plan.target_names})
    in_use = {}
    for plan in creations:
        own_pks = {pks[plan.base_name], *(member.pk for member in members.get(plan.base_name, ()))}
        taken = first_taken_name(plan.target_names, owners, own_pks)
        if taken is not None:
            in_use[plan.base_name] = taken
    return in_use


def _selection_pks(plan):
    """Return the interface primary keys a selection reaches this family through.

    A channelized family is submitted through its parent alone, because a channel is not an
    independent candidate on any path.
    """
    if isinstance(plan, InstalledFamilyPlan):
        if plan.parent_pk is None:
            return plan.member_pks
        return (plan.parent_pk,)  # pragma: no cover - requires channelization support
    if isinstance(plan, FlatCreationPlan):
        return plan.member_pks
    return (plan.base.pk,)


def _reaches(plan, selected_pks):
    """Return whether the operator's interface selection reaches *plan*; no selection reaches every plan."""
    return selected_pks is None or bool(selected_pks.intersection(_selection_pks(plan)))


def _selected(plans, selected_pks):
    """Return the plans the operator's interface selection reaches."""
    return [plan for plan in plans if _reaches(plan, selected_pks)]


def execute_module_families(plans):
    """Execute each planned family in order, and yield its outcome before the next family runs."""
    for plan in plans:
        yield execute_family_plan(plan)


def _apply_module(rule, module, interfaces, selected_pks):
    """Plan and execute every selected family on one module."""
    variables = build_variables(module.module_bay, device=module.device)
    bases = module_raw_bases(module, rule, variables, interfaces)
    plans = plan_module_families(module, rule, variables, interfaces, bases, selected_pks=selected_pks).plans
    return list(execute_module_families(plans))


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
