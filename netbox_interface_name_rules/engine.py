# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Core renaming engine — rule lookup and interface rename logic.

This module is imported lazily by rename_triggers.py so that model imports happen
after Django is fully initialised.
"""

import contextlib
import logging
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, replace

from django.core.exceptions import ValidationError

from . import family as family_ops
from . import name_template, naming, rule_selection
from .family import template_names as family_template_names
from .regex_safety import compile_module_type_pattern
from .rename_outcomes import OutcomeKind, RenameOutcome, renamed_count

logger = logging.getLogger(__name__)

NO_RULE_REASON = "no rule matches the module after the change"
FLAT_REASON = "a flat breakout family is not renamed after a move, a bay edit or a parent module type change"
ELSEWHERE_REASON = "the interface is not on the device of its module"
STALE_BAY_REASON = "the module bay still has the parent bay it had before its module moved"


def pinned_rule_cache():
    """Return the lower-level rule cache pinning context."""
    return rule_selection.pinned_rule_cache()


def find_matching_rule(module_type, parent_module_type, device_type, platform=None):
    """Delegate rule selection while preserving the engine entry point."""
    return rule_selection.find_matching_rule(module_type, parent_module_type, device_type, platform)


def build_variables(module_bay, device=None):
    """Delegate naming-variable construction while preserving the engine entry point."""
    return naming.build_variables(module_bay, device=device)


def evaluate_name_template(template: str, variables: dict) -> str:
    """Delegate template evaluation while preserving the engine entry point."""
    return name_template.evaluate_name_template(template, variables)


def _get_parent_module_type(module_bay):
    """Return the module type of the module installed in the parent bay, or None.

    Used by ``apply_interface_name_rules`` to scope rules to a specific parent
    module type (e.g., SFP inside a CVR-X2-SFP converter).
    """
    if module_bay.parent:
        parent_bay = module_bay.parent
        if hasattr(parent_bay, "installed_module") and parent_bay.installed_module:
            return parent_bay.installed_module.module_type
    return None


def _selected_rule(module, module_bay):
    """Return the rule that *module* in *module_bay* selects now, or None."""
    device_type = module.device.device_type if module.device else None
    platform = module.device.platform if module.device else None
    return find_matching_rule(module.module_type, _get_parent_module_type(module_bay), device_type, platform)


def supports_channelization():
    """Delegate the channelization capability check while preserving the engine entry point."""
    return family_ops.supports_channelization()


def _vc_position_re():
    """Delegate virtual-chassis token detection to template-name resolution."""
    return family_template_names.vc_position_re()


def supports_vc_position_token():
    """Return True when this NetBox resolves ``{vc_position}`` in component template names (4.6+).

    Probed from the constant that carries the token rather than a version comparison, so a backport
    or an upstream removal is detected by what NetBox actually provides.
    """
    return _vc_position_re() is not None


def _touches_a_family(plan) -> bool:
    """Return whether *plan* acts on a family rather than one standalone interface."""
    if isinstance(plan, family_ops.InstalledFamilyPlan):
        return plan.parent_pk is not None or len(plan.members) > 1
    return True


def _run_scope(force_reapply, previous_forms):
    """Return the scope of an automatic run; after a move the claim decides alone, without one."""
    if previous_forms is not None:
        return None
    return family_ops.RunScope.FORCED if force_reapply else family_ops.RunScope.INSTALL


_MEMBER_OUTCOME_KINDS = {
    family_ops.FamilyStatus.CHANGED: OutcomeKind.RENAMED,
    family_ops.FamilyStatus.BLOCKED: OutcomeKind.BLOCKED,
    family_ops.FamilyStatus.STALE: OutcomeKind.BLOCKED,
    family_ops.FamilyStatus.UNSUPPORTED: OutcomeKind.BLOCKED,
    family_ops.FamilyStatus.FAILED: OutcomeKind.FAILED,
}


def _rename_outcome(member) -> RenameOutcome:
    """Return the outcome fact of one executed family member."""
    kind = _MEMBER_OUTCOME_KINDS[member.status]
    if kind == OutcomeKind.BLOCKED and member.reason == family_ops.UNCLAIMED_BASE_REASON:
        kind = OutcomeKind.UNCLAIMED
    return RenameOutcome(kind, member.current_name, member.reason, member.target_name)


def _rename_outcomes(family_outcomes) -> tuple[RenameOutcome, ...]:
    """Return the outcome facts of executed families; a member that kept its correct name has none."""
    return tuple(
        _rename_outcome(member)
        for family in family_outcomes
        for member in family.members
        if member.status != family_ops.FamilyStatus.UNCHANGED
    )


def _unresolved_outcomes(missing, interface_names) -> tuple[RenameOutcome, ...]:
    """Return one unresolved-variable fact per interface that keeps its name for lack of *missing*."""
    reason = f"{', '.join(name_template.variable_token(name) for name in missing)} is not available on this device"
    return tuple(RenameOutcome(OutcomeKind.UNRESOLVED_VARIABLE, name, reason) for name in interface_names)


def apply_interface_name_rules(module, module_bay, force_reapply=False):
    """Apply InterfaceNameRule rename after module installation.

    Looks up a matching rule for (module_type, parent_module_type, device_type, platform)
    and renames interfaces created by NetBox's template instantiation.

    Only processes interfaces whose name still matches the raw bay position
    (i.e., haven't been renamed yet), ensuring idempotency.  Pass
    ``force_reapply=True`` to skip this check and re-apply rules to ALL
    module interfaces (used when vc_position or other variables change).

    Every rename and every creation goes through the family package, so this path builds and names
    exactly what retroactive apply and the preview describe.

    Returns:
        Number of interfaces renamed/created, or 0 if no rule matched.

    """
    return renamed_count(module_rule_outcomes(module, module_bay, force_reapply))


def module_rule_outcomes(
    module, module_bay, force_reapply=False, report_only=False, naming=None
) -> Iterator[RenameOutcome]:
    """Apply the module's rule as ``apply_interface_name_rules`` does, and yield its outcome facts.

    Each family's facts are yielded before the next family runs, so a caller keeps them when a later
    family fails. With *report_only*, nothing is renamed: only a rule that needs a variable the
    device lacks gives facts.

    *naming* is the module's ``ModuleNaming`` read before a move, a bay edit or a type change of its
    parent module. The rule then renames every name one template claims through its current or
    previous forms, and reports every other interface. Without a rule now, each interface the previous
    rule named keeps its name and is reported. When the previous rule is a flat breakout rule, nothing
    on the module is renamed and every interface is reported: NetBox keeps no link to a family, so it
    could be recognised by name only (ADR 0015). When an interface of the module is on another device,
    or the module's bay has a parent bay that does not hold the module that owns the bay, nothing is
    renamed and every interface is reported.
    """
    rule = _selected_rule(module, module_bay)
    previous_forms = None if naming is None else naming.previous_forms()

    if (
        previous_forms is not None
        and previous_forms.rule is not None
        and family_ops.builds_flat_family(previous_forms.rule)
    ):
        yield from _kept_flat_family(module)
        return
    if not rule:
        if previous_forms is not None and previous_forms.rule is not None:
            yield from _left_without_a_rule(module, previous_forms)
        return
    # One pin for the module: the claim and the family planner resolve its templates once.
    with family_ops.pinned_template_cache():
        yield from _apply_rule_to_module(rule, module, module_bay, force_reapply, report_only, previous_forms)


def _kept_flat_family(module) -> Iterator[RenameOutcome]:
    """Yield a blocked fact for every interface of *module*, which a flat breakout rule before the save leaves alone."""
    from dcim.models import Interface

    for name in Interface.objects.filter(module_id=module.pk).order_by("pk").values_list("name", flat=True):
        yield RenameOutcome(OutcomeKind.BLOCKED, name, FLAT_REASON)


def _left_without_a_rule(module, previous_forms) -> Iterator[RenameOutcome]:
    """Yield a blocked fact for each interface the previous rule named, now that no rule matches the module."""
    from dcim.models import Interface

    interfaces = list(Interface.objects.filter(module_id=module.pk).order_by("pk"))
    for name in family_ops.names_the_previous_rule_gave(previous_forms, interfaces):
        yield RenameOutcome(OutcomeKind.BLOCKED, name, NO_RULE_REASON)


def _has_stale_parent_bay(module_bay) -> bool:
    """Return whether *module_bay* belongs to a module but its parent bay does not hold that module.

    NetBox sets a bay's parent to the bay of the module that owns it; rule selection read both already.
    """
    if module_bay.module_id is None:
        return False
    installed = getattr(module_bay.parent, "installed_module", None)
    return installed is None or installed.pk != module_bay.module_id


def _unavailable_rule_variables(rule, variables) -> tuple[str, ...]:
    """Return the variables either template of the module *rule* reads that *variables* lack."""
    return tuple(
        dict.fromkeys(
            name
            for template in (rule.name_template, rule.parent_name_template)
            for name in naming.unavailable_variables(template, variables)
        )
    )


def _acted_on_names(rule, plans):
    """Return the name of every interface the admitted *plans* act on, in plan order."""
    names = []
    for plan in plans:
        if isinstance(plan, family_ops.InstalledFamilyPlan):
            keeps_parent = plan.parent_pk is not None and not family_ops.names_installed_parent(rule)
            names.extend(
                member.snapshot.name
                for member in plan.members
                if not (keeps_parent and member.role == family_ops.MemberRole.PARENT)
            )
            continue
        names.append(plan.base.name)
        if isinstance(plan, family_ops.FlatCreationPlan):
            names.extend(member.name for member in plan.members)
    return tuple(dict.fromkeys(names))


def _apply_rule_to_module(rule, module, module_bay, force_reapply, report_only=False, previous_forms=None):
    """Plan and execute every family *rule* intends on *module*; see ``module_rule_outcomes``."""
    from dcim.models import Interface

    variables = build_variables(module_bay, device=module.device)
    missing = _unavailable_rule_variables(rule, variables)
    if report_only and not missing:
        return
    interfaces = list(Interface.objects.filter(module_id=module.pk).order_by("pk"))
    # NetBox before 4.7 moves only the module row: its interfaces and nested bays keep the old placement.
    if any(interface.device_id != module.device_id for interface in interfaces):
        yield from (RenameOutcome(OutcomeKind.BLOCKED, i.name, ELSEWHERE_REASON) for i in interfaces)
        return
    if _has_stale_parent_bay(module_bay):
        yield from (RenameOutcome(OutcomeKind.BLOCKED, i.name, STALE_BAY_REASON) for i in interfaces)
        return
    bases = family_ops.module_raw_bases(module, rule, variables, interfaces, previous_forms)
    planned = family_ops.plan_module_families(
        module, rule, variables, interfaces, bases, _run_scope(force_reapply, previous_forms)
    )
    installed, leftover = planned.installed, planned.leftover
    plans = [*installed, *leftover]

    if missing:
        yield from _unresolved_outcomes(missing, _acted_on_names(rule, plans))
        return

    any_outcome = False
    for family in family_ops.execute_module_families(plans):
        for outcome in _rename_outcomes((family,)):
            any_outcome = True
            yield outcome
    families_seen = bool(installed) or any(_touches_a_family(plan) for plan in leftover)

    if not force_reapply and previous_forms is None and leftover and not any_outcome and not families_seen:
        # Nothing renamed, skipped or built as a family: NetBox may already give these names.
        _flag_rule_potentially_deprecated(rule)


def predict_rule_output(module, module_bay, raw_names):
    """Predict the names apply_interface_name_rules would produce for raw_names.

    Read-only: saves and mutates nothing.  Used by external integrations (e.g.
    netbox-librenms-plugin) that need to know the post-rename names without applying any rule.

    The names are planned by the family module from the module type's templates, so prediction
    describes the same families installed execution builds: a breakout rule expands one plain name
    into the family it creates, a name the templates describe as part of a channelized family
    follows that family instead, and a family the apply path refuses to touch predicts unchanged.
    Returns raw_names unchanged when no rule matches.

    Precondition: *raw_names* are resolved by the caller at call time.  A name captured before the
    device's virtual-chassis position changed is predicted from itself, not corrected to the name
    the templates resolve to now — this function maps the names it is given.
    """
    rule = _selected_rule(module, module_bay)
    if not rule:
        return list(raw_names)

    variables = build_variables(module_bay, device=module.device)
    described = family_ops.describe_module_interfaces(module, raw_names)
    channel_names = {interface.name for interface in described if interface.channel_id is not None}
    base_names = [name for name in raw_names if name not in channel_names]
    plan_set = family_ops.plan_prospective_families(
        module, rule, variables, described, family_ops.given_raw_names(module, rule, variables, base_names)
    )
    return [name for raw_name in raw_names for name in plan_set.predicted_names(raw_name)]


def reapply_module_rules(device):
    """Re-apply module rules to every module on *device* after its virtual-chassis position changed.

    The whole device is one batch: its modules match against one enabled-rule snapshot, and one
    module type's interface templates are read once however many modules carry it. An unexpected
    failure stops the module batch and propagates to the deferred callback boundary.

    Returns the number of interfaces renamed across the device's modules.
    """
    return renamed_count(device_module_rule_outcomes(device))


def device_module_rule_outcomes(device, report_only=False, excluded=()) -> Iterator[RenameOutcome]:
    """Reapply the rules of every module on *device* as ``reapply_module_rules`` does, and yield the outcome facts.

    Each module's facts are yielded before the next module runs, so a caller keeps them when a later
    module fails. *report_only* is passed to ``module_rule_outcomes``. The modules whose primary keys
    are in *excluded* are left out.
    """
    from dcim.models import Module

    modules = list(
        Module.objects.filter(device=device)
        .exclude(pk__in=excluded)
        .select_related(
            "module_type",
            "device__device_type",
            "device__platform",
            *family_template_names.BAY_CHAIN_RELATIONS,
        )
    )
    with pinned_rule_cache(), family_ops.pinned_template_cache(modules):
        for module in modules:
            yield from module_rule_outcomes(module, module.module_bay, force_reapply=True, report_only=report_only)


_NAMING_RELATIONS = (
    "module_type",
    "device__device_type",
    "device__platform",
    "module_bay__parent__installed_module__module_type",
    *family_template_names.BAY_CHAIN_RELATIONS,
)


@dataclass(frozen=True, eq=False)
class ModuleNaming:
    """What named one module's interfaces at the time it was read: a move, a bay edit or a type change reads it.

    The module type and the scope select the rule that state gave the module. The template variables
    and the templates as they resolved then rebuild the names that rule gave. ``device_pk`` is the
    device the module was on then. ``bay_values`` are the ``naming.bay_naming_values`` of the module's
    bay then. ``raw_only`` is set when no rule has named the module's interfaces yet, because the
    module was installed in the same transaction: they carry raw template names only.
    """

    module_pk: int
    device_pk: int
    module_type: object
    parent_module_type: object | None
    device_type: object | None
    platform: object | None
    variables: dict
    templates: tuple
    bay_values: tuple
    raw_only: bool = False

    @classmethod
    def of(cls, module):
        """Return the naming of *module*, which carries ``_NAMING_RELATIONS``, as it is now."""
        device = module.device
        module_bay = module.module_bay
        return cls(
            module_pk=module.pk,
            device_pk=device.pk,
            module_type=module.module_type,
            parent_module_type=_get_parent_module_type(module_bay),
            device_type=device.device_type,
            platform=device.platform,
            variables=build_variables(module_bay, device=device),
            templates=family_ops.resolved_template_names(module),
            bay_values=naming.bay_naming_values(module_bay.position, module_bay.name),
        )

    def at_chassis_position(self, vc_position):
        """Return this naming with *vc_position* in its variables; None leaves ``{vc_position}`` out."""
        return replace(self, variables=naming.with_chassis_position(self.variables, vc_position))

    def rule(self):
        """Return the rule that the module type and the scope of this naming select now, or None."""
        return find_matching_rule(self.module_type, self.parent_module_type, self.device_type, self.platform)

    def selects_another_rule(self, module) -> bool:
        """Return whether *module* now selects another rule than this naming; it reads the rule cache.

        A module that ``committed_modules`` returned needs no other query.
        """
        return _selected_rule(module, module.module_bay) != self.rule()

    def previous_forms(self) -> family_ops.PreviousForms:
        """Return what rebuilds the names this naming gave, under the rule it selects now; no rule when ``raw_only``."""
        rule = None if self.raw_only else self.rule()
        return family_ops.PreviousForms(rule, self.variables, {template.pk: template for template in self.templates})


def read_subtree_naming(module_pk) -> tuple[ModuleNaming, ...]:
    """Return the naming of the module and of every module nested in it, the module itself first.

    A move reads this before its save: in the same save NetBox can re-resolve the position and name
    of each bay the module holds, so the nested modules' naming is gone afterwards. A bay edit reads
    it before its save, because the save changes what the names in the bay are built from. A type
    change reads it before its save, because the nested modules can then select another rule.
    """
    from dcim.models import Module

    modules = []
    level = list(Module.objects.filter(pk=module_pk).select_related(*_NAMING_RELATIONS))
    while level:
        modules.extend(level)
        level = list(
            Module.objects.filter(module_bay__module__in=level)
            .exclude(pk__in=[module.pk for module in modules])
            .select_related(*_NAMING_RELATIONS)
            .order_by("pk")
        )
    with family_ops.pinned_template_cache(modules):
        return tuple(ModuleNaming.of(module) for module in modules)


def committed_modules(module_pks) -> dict:
    """Return the committed modules that *module_pks* name, by primary key, with every relation a reapply reads."""
    from dcim.models import Module

    return Module.objects.select_related(*_NAMING_RELATIONS).in_bulk(module_pks)


@contextlib.contextmanager
def pinned_reapply(modules):
    """Share one enabled-rule snapshot, and the resolved templates of *modules*, across one reapply."""
    with pinned_rule_cache(), family_ops.pinned_template_cache(modules):
        yield


def _device_interface_rules(device):
    """Return enabled device-interface rules in matching priority order."""
    from django.db.models import Q

    from .models import InterfaceNameRule

    device_type = getattr(device, "device_type", None)
    platform = getattr(device, "platform", None)
    rules = list(
        InterfaceNameRule.objects.filter(
            applies_to_device_interfaces=True,
            enabled=True,
        )
        .filter(Q(device_type=device_type) | Q(device_type__isnull=True))
        .filter(Q(platform=platform) | Q(platform__isnull=True))
    )
    # Sort Python-side: specificity_score descending, then module_type_pattern length
    # descending (for device-interface rules with ties), then pk ascending for stability.
    # (InterfaceNameRule has no DB 'priority' field; specificity_score is a property.)
    rules.sort(
        key=lambda r: (
            -r.specificity_score,
            -len(r.module_type_pattern or ""),
            r.pk,
        )
    )
    return rules


def _matches_device_interface(rule, interface):
    """Return whether one device-interface rule matches one device interface."""
    if not rule.module_type_pattern:
        return True
    try:
        compiled = compile_module_type_pattern(rule.module_type_pattern)
    except ValidationError:
        return False
    return compiled.fullmatch(interface.name) is not None


def _apply_device_rule_to_families(device, vc_position, rule, families, claimed_pks, report_only, by_family):
    """Apply one rule to each eligible device-interface family and record its outcome facts in *by_family*.

    A rule that renames a family, or finds it correct, replaces the skips earlier rules left on it;
    a failure stays reported.
    """
    for interface, children in families:
        if interface.pk in claimed_pks or not _matches_device_interface(rule, interface):
            continue
        variables = naming.build_device_interface_variables(interface.name, vc_position)
        missing = naming.unavailable_variables(rule.name_template, variables)
        if missing:
            names = (interface.name, *(child.name for child in children))
            by_family.setdefault(interface.pk, []).extend(_unresolved_outcomes(missing, names))
            claimed_pks.update((interface.pk, *(child.pk for child in children)))
            continue
        if report_only:
            claimed_pks.update((interface.pk, *(child.pk for child in children)))
            continue
        plan = family_ops.plan_device_interface_rename(device, rule, variables, interface, children)
        outcome = family_ops.execute_installed_plan(plan)
        if outcome.status in {family_ops.FamilyStatus.CHANGED, family_ops.FamilyStatus.UNCHANGED}:
            claimed_pks.update(plan.member_pks)
            failures = [earlier for earlier in by_family.get(interface.pk, ()) if earlier.kind == OutcomeKind.FAILED]
            by_family[interface.pk] = [*failures, *_rename_outcomes((outcome,))]
        else:
            by_family.setdefault(interface.pk, []).extend(_rename_outcomes((outcome,)))


def apply_device_interface_rules(device):
    """Rename device-level interfaces (module=None) when a device joins/changes position in a VC.

    Finds all enabled rules with ``applies_to_device_interfaces=True`` that match the device's
    type and platform, then renames any matching interfaces using the name_template.

    Template variables available: ``{vc_position}``, ``{base}`` (full current name),
    ``{port}`` (segment after the last ``/``, or the full name if no ``/`` present).

    Channel subinterfaces are not matched independently. They follow the parent whose family
    a rule wins, so a template like ``eth{vc_position}`` cannot collapse a whole family onto
    one name.

    Returns the number of interfaces renamed.
    """
    if not getattr(device, "virtual_chassis_id", None):
        return 0  # Only rename for VC members (vc_position must be set)

    if device.vc_position is None:
        return 0  # vc_position unset (e.g. VC master before position assigned)

    return renamed_count(device_interface_rule_outcomes(device))


def device_interface_rule_outcomes(device, report_only=False) -> Iterator[RenameOutcome]:
    """Apply the device-interface rules and yield the outcome facts.

    Unlike ``apply_device_interface_rules``, this also runs for a device without a virtual-chassis
    position: a rule that reads ``{vc_position}`` then renames nothing and reports each interface it
    matches as unresolved. With *report_only*, no rule renames anything.

    The facts are yielded after the last rule, because a later rule can replace a family's facts.
    When a rule fails, the facts recorded before the failure are yielded before the error propagates.
    """
    from dcim.models import Interface

    vc_position = naming.chassis_position(device)
    rules = _device_interface_rules(device)
    if not rules:
        return

    interfaces = list(Interface.objects.filter(device=device, module=None).order_by("pk"))
    if not interfaces:
        return

    families = family_ops.device_interface_families(interfaces)
    claimed_pks: set[int] = set()
    by_family: dict[int, list] = {}
    try:
        for rule in rules:
            _apply_device_rule_to_families(device, vc_position, rule, families, claimed_pks, report_only, by_family)
    finally:
        # A caller that extends a list keeps these facts when a later family raises.
        for outcomes in by_family.values():
            yield from outcomes


def _flag_rule_potentially_deprecated(rule):
    """Tag a rule as 'potentially-deprecated' when its rename is a no-op.

    Called from apply_interface_name_rules when a matching rule produces no
    renames because NetBox already generates the correct interface names.  This
    may indicate the rule is no longer needed (e.g. after a NetBox upgrade that
    improved template resolution), or only needed for a subset of module types.

    Adds a NetBox Tag 'potentially-deprecated' so the rule is visually flagged
    in the UI for operator review.  Failures are logged but never re-raised so
    the install path is not disrupted.
    """
    try:
        from extras.models import Tag

        tag, _ = Tag.objects.get_or_create(
            slug="potentially-deprecated",
            defaults={"name": "potentially-deprecated", "color": "ffc107"},
        )
        rule.tags.add(tag)
        logger.info(
            "Rule '%s' flagged as potentially-deprecated: NetBox already generates the correct interface names.",
            rule,
        )
    except Exception:
        logger.exception("Failed to flag rule '%s' as potentially-deprecated.", rule)


def _matching_moduletype_pks(module_type_pattern):
    """Return PKs of ModuleTypes whose model name matches the given RE2 pattern.

    Raises ValueError for invalid patterns, mirroring evaluate_name_template's
    error-handling convention so callers can treat both as ValueError.
    """
    from dcim.models import ModuleType

    try:
        compiled = compile_module_type_pattern(module_type_pattern)
    except ValidationError as exc:
        raise ValueError(exc.messages[0]) from exc
    return [mt.pk for mt in ModuleType.objects.only("pk", "model") if compiled.fullmatch(mt.model)]


def has_applicable_interfaces(rule) -> bool:
    """Check whether applying this rule right now would rename at least one interface.

    Calls find_interfaces_for_rule(limit=1) to determine if any currently installed
    interface would receive a new name.  Returns False when:
      - no matching modules/interfaces are installed, OR
      - all matching interfaces are already correctly named.

    This is more expensive than a plain EXISTS query but ensures the Applicable
    column in the Apply Rules list accurately reflects "would something change?"
    rather than the misleading "do interfaces exist?".
    """
    try:
        results, _ = find_interfaces_for_rule(rule, limit=1)
        return len(results) > 0
    except ValueError:
        return False


def _build_module_qs(rule):
    """Return a Module queryset filtered to the rule's scope (module type, parent, device, platform).

    Shared by ``find_interfaces_for_rule`` and ``apply_rule_to_existing`` to avoid
    duplicating the filtering logic.
    """
    from dcim.models import Module

    if rule.module_type_is_regex:
        qs = Module.objects.filter(module_type__in=_matching_moduletype_pks(rule.module_type_pattern))
    else:
        qs = Module.objects.filter(module_type=rule.module_type)
    if rule.parent_module_type:
        qs = qs.filter(module_bay__parent__installed_module__module_type=rule.parent_module_type)
    if rule.device_type:
        qs = qs.filter(device__device_type=rule.device_type)
    if rule.platform:
        qs = qs.filter(device__platform=rule.platform)
    return qs


_PREVIEW_ROLES = {
    family_ops.MemberRole.PARENT: "parent",
    family_ops.MemberRole.CHANNEL: "channel",
}


def _member_detail(plan, member) -> family_ops.PlannedName:
    """Describe one planned member so the UI can render a family as a family.

    A flat family's members are the channels a breakout rule spells out; a plan that holds only
    one of them is a plain rename, not a family.
    """
    role = _PREVIEW_ROLES.get(member.role) or ("channel" if len(plan.members) > 1 else "interface")
    return family_ops.PlannedName(member.target_name, role, member.channel_id)


def _plan_details(plan) -> list:
    """Describe every name the plan intends, or the error that stopped it from naming them."""
    if plan.precondition_status != family_ops.FamilyStatus.FAILED:
        return [_member_detail(plan, member) for member in plan.members]
    root = _member_detail(plan, plan.members[0])
    return [family_ops.PlannedName(f"<error: {plan.precondition_reason}>", root.role, root.channel_id)]


def _plan_changes_names(plan, existing_names) -> bool:
    """Return whether the planned family would rename or create anything.

    A plan that renames members compares intent with the names they carry now.  A plan that builds
    a family out of one base compares intent with the names the module already holds, so a family
    an earlier apply already installed previews as no change.
    """
    if plan.base_name is None:  # pragma: no cover - requires channelization support
        return plan.target_names != plan.source_names
    return plan.target_names[0] != plan.base_name or any(
        target_name not in existing_names for target_name in plan.target_names
    )


def _plan_entry(module, plan, interface, existing_names) -> dict | None:
    """Build the preview entry for one family plan, or None when it would change nothing.

    The entry stays keyed on the interface the Apply view submits, and lists the family's names in
    ``new_names``, so the existing template loop keeps working unchanged.  A plan the live topology
    blocks previews nothing, because the apply path would build nothing either.
    """
    failed = plan.precondition_status == family_ops.FamilyStatus.FAILED
    if plan.precondition_status is not None and not failed:
        return None
    if not failed and not _plan_changes_names(plan, existing_names):
        return None
    details = _plan_details(plan)
    return {
        "module": module,
        "interface": interface,
        "current_name": interface.name,
        "new_names": [detail.name for detail in details],
        "name_details": details,
    }


def _plan_root_name(plan) -> str:
    """Return the name of the interface a plan is submitted through."""
    return plan.base_name if plan.base_name is not None else plan.members[0].source_name


def _preview_plans(rule, plan_set) -> list:
    """Return the plans this preview reports.

    A breakout rule on a module that already models channelized families renames those families and
    adds none beside them.  Anywhere else it builds one family per base, and two bases that intend
    the same names are the one family an earlier apply already started, so it is offered once: the
    same family the apply path would build.
    """
    if rule.channel_count <= 0:
        return list(plan_set.plans)
    installed = [plan for plan in plan_set.plans if plan.base_name is None]
    if installed:  # pragma: no cover - requires a NetBox that models channelization
        return installed
    creations = [plan for plan in plan_set.plans if plan.base_name is not None]
    kept = family_ops.one_family_per_name_set([(plan.base_name, plan.target_names) for plan in creations])
    return [creations[index] for index in kept]


def _installed_flat_entry(module, plan, interface) -> dict | None:
    """Build the preview entry for an installed flat family, or None when it keeps its names."""
    if all(member.target_name == member.snapshot.name for member in plan.members):
        return None
    role = "channel" if len(plan.members) > 1 else "interface"
    details = [family_ops.PlannedName(member.target_name, role) for member in plan.members]
    return {
        "module": module,
        "interface": interface,
        "current_name": interface.name,
        "new_names": [detail.name for detail in details],
        "name_details": details,
    }


def _process_module(rule, module, ifaces, variables, limit, results, module_qs, processed_pks):
    """Preview one module from its family plans.  Returns (checked_count, should_stop).

    An installed flat family is previewed from the plan Apply Rules executes, because its members
    carry names no template describes.
    """
    bases = family_ops.module_raw_bases(module, rule, variables, ifaces)
    installed = family_ops.plan_installed_flat_families(module, rule, variables, ifaces, bases)
    installed_pks = {pk for plan in installed for pk in plan.member_pks}
    rows = [iface for iface in ifaces if iface.pk not in installed_pks]
    plan_set = family_ops.plan_prospective_families(
        module, rule, variables, family_ops.describe_interfaces(rows), bases
    )
    # A flat family counts its members, as the scan of unvisited modules counts interfaces.
    checked = sum(len(plan.members) for plan in installed) + len(plan_set.plans)
    if not checked:
        return 0, False
    rows_by_pk = {iface.pk: iface for iface in ifaces}
    rows_by_name = {iface.name: iface for iface in ifaces}
    existing_names = frozenset(rows_by_name)
    entries = [_installed_flat_entry(module, plan, rows_by_pk[plan.member_pks[0]]) for plan in installed]
    offered = _preview_plans(rule, plan_set)
    # The apply refuses a family whose names are in use, so the preview offers no change there.
    in_use = family_ops.creation_names_in_use(module, rule, ifaces, bases, offered)
    entries.extend(
        _plan_entry(module, plan, rows_by_name[_plan_root_name(plan)], existing_names)
        for plan in offered
        if plan.base_name not in in_use
    )
    for entry in entries:
        if entry is None:
            continue
        results.append(entry)
        if limit is not None and len(results) >= limit:
            return checked + _count_remaining_interfaces(module_qs, processed_pks), True
    return checked, False


def _count_remaining_interfaces(module_qs, processed_pks) -> int:
    """Count the rule candidates in modules not yet visited during a find_interfaces_for_rule scan."""
    from dcim.models import Interface

    qs = Interface.objects.filter(module__in=module_qs.exclude(pk__in=processed_pks))
    if supports_channelization():  # pragma: no cover - the column exists only on NetBox 4.7+
        qs = qs.filter(channel_id__isnull=True)  # a family counts once, through its parent
    return qs.count()


def find_interfaces_for_rule(rule, limit=None):
    """Find interfaces that would be renamed by applying the given rule retroactively.

    Searches for all Module instances matching the rule's criteria and computes
    what their interfaces would be renamed to.

    Returns a tuple ``(results, total_checked)`` where *results* is a list of dicts::

        {
            "module":       Module instance,
            "interface":    Interface instance,
            "current_name": str,
            "new_names":    list[str],    # one entry per channel, or single-element
            "name_details": list[PlannedName],  # name, role and channel id per new_names entry
        }

    Only includes entries where at least one new_name differs from current_name.
    A channelized parent is reported once, with its channels' names in the same entry, so the
    caller can act on the family through the parent PK it already submits.  If *limit* is set the
    list is truncated after that many changed entries, but *total_checked* always reflects the full
    count of families examined (a family counts once, however many channels it has).
    """
    from dcim.models import Interface

    module_qs = _build_module_qs(rule).select_related(
        "module_type",
        "device__device_type",
        "device__platform",
        *family_template_names.BAY_CHAIN_RELATIONS,
    )
    # Batch-load all interfaces for matching modules to avoid N+1 queries.
    ifaces_by_module = defaultdict(list)
    for iface in Interface.objects.filter(module__in=module_qs).order_by("module_id", "name"):
        ifaces_by_module[iface.module_id].append(iface)

    modules = list(module_qs)
    processed_pks = set()
    results = []
    total_checked = 0
    with family_ops.pinned_template_cache(modules):
        for module in modules:
            processed_pks.add(module.pk)
            variables = build_variables(module.module_bay, device=module.device)
            ifaces = ifaces_by_module.get(module.pk, [])
            checked, stop = _process_module(rule, module, ifaces, variables, limit, results, module_qs, processed_pks)
            total_checked += checked
            if stop:
                return results, total_checked

    return results, total_checked


def _batch_modules(rule):
    """Return the rule's modules with every relation planning and template resolution dereference."""
    return list(
        _build_module_qs(rule).select_related(
            "module_type",
            "device__device_type",
            "device__platform",
            *family_template_names.BAY_CHAIN_RELATIONS,
        )
    )


def apply_rule_to_existing(rule, limit=None, interface_ids=None) -> family_ops.BatchOutcome:
    """Apply a rule retroactively to all matching installed modules.

    Unlike apply_interface_name_rules(), this does not skip already-renamed interfaces: it plans
    every family each matching module carries or would gain, and executes each in its own
    transaction, so one blocked family costs the batch only that family.

    If *interface_ids* is provided (list/set of Interface PKs), only the families those interfaces
    reach are applied; an empty collection touches the database not at all.  Selecting a
    channelized parent brings its channel subinterfaces along; selecting a channel subinterface on
    its own does nothing, because it is not an independent candidate.  If *limit* is set the batch
    stops after the module that reached that many changed interfaces.

    Returns the batch outcome: one explicit family result per family it planned.
    """
    id_set = frozenset(interface_ids) if interface_ids is not None else None
    if not rule.enabled or (id_set is not None and not id_set):
        return family_ops.BatchOutcome(families=())
    return family_ops.apply_rule_to_modules(rule, _batch_modules(rule), selected_pks=id_set, limit=limit)


# ---------------------------------------------------------------------------
# Assisted flat → channelized conversion
# ---------------------------------------------------------------------------
# An earlier flat apply leaves N sibling interfaces where NetBox 4.7+ models a channelized parent
# with N channel subinterfaces.  Converting one rewrites rows an operator owns — cables, addresses,
# tags — so it is never a side effect of applying a rule: the operator confirms it per family.


def find_convertible_families(rule, limit=None) -> family_ops.ConversionPreview:
    """Return the preview of the flat families *rule* could convert, convertible or not.

    Each candidate names the ch-0 row the confirm form submits, the family's current names, the
    names it would carry, and where the ch-0 row's configuration lands.  A family beyond *limit* is
    never dry-run; the preview reports that one was left unexamined.
    """
    # Only the cheap half of the guard, so a rule that offers no conversion never reads its modules;
    # whether this release can hold a family is the family package's call.
    if not family_ops.conversion_offered(rule):
        return family_ops.ConversionPreview(candidates=())
    return family_ops.preview_rule_conversions(rule, _batch_modules(rule), limit=limit)


def convert_flat_families(rule, base_pks=None) -> family_ops.BatchOutcome:
    """Convert *rule*'s installed flat families to the channelized topology.

    *base_pks* is the set of ch-0 interface pks the operator confirmed: ``None`` converts every
    convertible family (the batch the background job runs), an empty collection converts none.

    Returns the batch outcome: one explicit family result per family it planned.
    """
    selected = None if base_pks is None else frozenset(base_pks)
    return family_ops.convert_rule_families(rule, _batch_modules(rule), selected_pks=selected)
