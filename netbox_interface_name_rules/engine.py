# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Core renaming engine — rule lookup and interface rename logic.

This module is imported lazily by signals.py so that model imports happen
after Django is fully initialised.
"""

import logging
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from . import family as family_ops
from . import naming, rule_selection
from .family import targets as family_targets
from .family import template_names as family_template_names
from .regex_safety import compile_module_type_pattern

logger = logging.getLogger(__name__)


def pinned_rule_cache():
    """Return the lower-level rule cache pinning context."""
    return rule_selection.pinned_rule_cache()


def find_matching_rule(module_type, parent_module_type, device_type, platform=None):
    """Delegate rule selection while preserving the engine entry point."""
    return rule_selection.find_matching_rule(module_type, parent_module_type, device_type, platform)


def _extract_trailing_digits(value: str) -> str:
    """Delegate trailing-digit extraction while preserving the engine helper."""
    return naming._extract_trailing_digits(value)


def _resolve_bay_position(module_bay):
    """Delegate bay-position resolution while preserving the engine helper."""
    return naming._resolve_bay_position(module_bay)


def _resolve_slot(module_bay, bay_position_num, parent_bay_position):
    """Delegate slot resolution while preserving the engine helper."""
    return naming._resolve_slot(module_bay, bay_position_num, parent_bay_position)


def build_variables(module_bay, device=None):
    """Delegate naming-variable construction while preserving the engine entry point."""
    return naming.build_variables(module_bay, device=device)


def evaluate_name_template(template: str, variables: dict) -> str:
    """Delegate template evaluation while preserving the engine entry point."""
    return naming.evaluate_name_template(template, variables)


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


def _unambiguous_claims(candidates, matchers, module):  # pragma: no cover - requires vc_position token support
    """Return the labels of *candidates* that exactly one drifted ``{vc_position}`` template claims.

    *candidates* pairs a label with the name forms it is compared under.  Both sides of the claim
    have to be unique: a template matching two labels, or a label matched by two templates,
    disqualifies everything involved with a warning rather than renaming a guess.
    """
    claims = defaultdict(list)
    claimants = defaultdict(list)
    for index, matcher in enumerate(matchers):
        for label, forms in candidates:
            if any(matcher.pattern.fullmatch(form) for form in forms):
                claims[index].append(label)
                claimants[label].append(index)

    ambiguous = {index for index, claimed in claims.items() if len(claimed) > 1}
    for index in sorted(ambiguous):
        logger.warning(
            "Interface template %r of %s could name any of %s since this device's virtual-chassis "
            "position changed; skipping them all rather than renaming a guess.",
            matchers[index].template_name,
            module,
            sorted(claims[index]),
        )
    for label, indexes in claimants.items():
        if len(indexes) > 1:
            logger.warning(
                "Interface %r on %s could be the drifted name of any of the templates %s; "
                "skipping it rather than renaming a guess.",
                label,
                module,
                sorted(matchers[index].template_name for index in indexes),
            )
            ambiguous.update(indexes)
    return [claims[index][0] for index in sorted(claims) if index not in ambiguous]


def _drifted_candidates(interfaces, matchers, module):  # pragma: no cover - requires vc_position token support
    """Return the interfaces a single drifted ``{vc_position}`` template unambiguously claims.

    *interfaces* and *matchers* are what the exact pass left unclaimed.
    """
    by_name = {iface.name: iface for iface in interfaces}
    claimed = _unambiguous_claims([(iface.name, (iface.name,)) for iface in interfaces], matchers, module)
    return [by_name[label] for label in claimed]


def _forced_channel_bases(interfaces, raw_names, matchers, module):
    """Return one interface per base a forced breakout rule should process, preferring the ":0" one.

    A base is claimed exactly when either comparison form — the full base or its last path segment,
    the latter covering already-renamed bases — is a raw name now; otherwise a token template's
    matcher may claim it, under the same one-to-one policy ``_drifted_candidates`` applies.  Two
    bases with distinct rule outputs never collide downstream, so an ambiguous claim has to be
    stopped here or it is not stopped at all.
    """
    seen_bases: dict = {}
    forms_by_base: dict = {}
    for i in interfaces:
        # A channelized parent is its own base: its channels are separate rows, so the name needs no
        # ":"-splitting to find them.
        base = i.name if family_ops.is_channelized_parent(i) else i.name.rsplit(":", 1)[0]
        forms = (base, base.rsplit("/", 1)[-1])
        if not any(form in raw_names for form in forms) and not any(
            matcher.pattern.fullmatch(form) for matcher in matchers for form in forms
        ):
            continue
        forms_by_base[base] = forms
        if base not in seen_bases or i.name.endswith(":0"):
            seen_bases[base] = i

    exact_forms = {form for forms in forms_by_base.values() for form in forms if form in raw_names}
    drifted = {base: forms for base, forms in forms_by_base.items() if not exact_forms & set(forms)}
    if not drifted:
        return list(seen_bases.values())
    kept = set(  # pragma: no cover - requires vc_position token support
        _unambiguous_claims(drifted.items(), [m for m in matchers if m.resolved not in exact_forms], module)
    )
    return [i for base, i in seen_bases.items() if base not in drifted or base in kept]  # pragma: no cover - see above


def _collect_unrenamed(interfaces, rule, raw_names, force_reapply, matchers=(), module=None):
    """Return the subset of *interfaces* that should be processed by the rule.

    Normal (non-force) mode: only interfaces whose current name is still in the
    raw template names (idempotency guard), plus the ones a *matchers* entry
    claims as its own drifted name (see ``_drifted_candidates``).

    force_reapply, non-channel: all interfaces (e.g. vc_position changed).

    force_reapply, channel rule: one interface per base name (see
    ``_forced_channel_bases``).
    """
    if not force_reapply:
        exact = [i for i in interfaces if i.name in raw_names]
        if not matchers:
            return exact
        claimed = {i.name for i in exact}  # pragma: no cover - requires vc_position token support
        return exact + _drifted_candidates(  # pragma: no cover - see above
            [i for i in interfaces if i.name not in claimed],
            [m for m in matchers if m.resolved not in claimed],
            module,
        )
    if rule.channel_count == 0:
        return interfaces
    return _forced_channel_bases(interfaces, raw_names, matchers, module)


def _touches_a_family(plan) -> bool:
    """Return whether *plan* acts on a family rather than one standalone interface."""
    if isinstance(plan, family_ops.InstalledFamilyPlan):
        return plan.parent_pk is not None or len(plan.members) > 1
    return True


def _admitted_installed(plans, rule, raw_names, force_reapply, matchers, module):
    """Return the installed families this install path should execute.

    A channelized family is always executed: its parent decides the family's names, and the raw-name
    guard describes flat rows.  A flat family is executed while the guard still claims a member of it.
    """
    flat = [plan for plan in plans if plan.topology == family_ops.FamilyTopology.FLAT]
    snapshots = [member.snapshot for plan in flat for member in plan.members]
    selected = {
        interface.pk for interface in _collect_unrenamed(snapshots, rule, raw_names, force_reapply, matchers, module)
    }
    return [
        plan
        for plan in plans
        if plan.topology == family_ops.FamilyTopology.CHANNELIZED or selected.intersection(plan.member_pks)
    ]


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
    device_type = module.device.device_type if module.device else None
    platform = module.device.platform if module.device else None
    rule = find_matching_rule(module.module_type, _get_parent_module_type(module_bay), device_type, platform)

    if not rule:
        return 0
    # One pin for the module: the raw-name matchers and the family planner resolve its templates once.
    with family_ops.pinned_template_cache():
        return _apply_rule_to_module(rule, module, module_bay, force_reapply)


def _apply_rule_to_module(rule, module, module_bay, force_reapply):
    """Plan and execute every family *rule* intends on *module*; see ``apply_interface_name_rules``."""
    from dcim.models import Interface

    variables = build_variables(module_bay, device=module.device)
    raw = _raw_name_matchers(module)
    raw_names = raw.names or {variables["bay_position"]}
    interfaces = list(Interface.objects.filter(module_id=module.pk).order_by("pk"))
    planned = family_ops.plan_module_families(
        module,
        rule,
        variables,
        interfaces,
        # The guard runs while it can still see every claimed row, before two of them that intend
        # one family are collapsed into it.
        admit_leftover=lambda plain: _collect_unrenamed(plain, rule, raw_names, force_reapply, raw.matchers, module),
    )
    installed = _admitted_installed(planned.installed, rule, raw_names, force_reapply, raw.matchers, module)
    leftover = planned.leftover

    outcomes = family_ops.execute_module_families([*installed, *leftover])
    renamed = sum(outcome.changed_count for outcome in outcomes)
    blocked = [
        member for outcome in outcomes for member in outcome.members if member.status == family_ops.FamilyStatus.BLOCKED
    ]
    families_seen = bool(installed) or any(_touches_a_family(plan) for plan in leftover)

    if leftover and renamed == 0 and not blocked and not families_seen:
        # All interfaces already have the names the rule would produce — flag as
        # potentially obsolete (e.g., newer NetBox generates correct names natively).
        # Skipped when the 0-count was caused by name collisions (a different reason
        # than a no-op rule), so a collision never mislabels the rule as deprecated.
        # Skipped for families too: a structural skip, or a family whose parent deliberately
        # keeps its raw name, says nothing about the rule being obsolete.
        _flag_rule_potentially_deprecated(rule)

    return renamed


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
    device_type = module.device.device_type if module.device else None
    platform = module.device.platform if module.device else None
    rule = find_matching_rule(module.module_type, _get_parent_module_type(module_bay), device_type, platform)
    if not rule:
        return list(raw_names)

    plan_set = family_ops.plan_prospective_families(
        module,
        rule,
        build_variables(module_bay, device=module.device),
        family_ops.describe_module_interfaces(module, raw_names),
    )
    return [name for raw_name in raw_names for name in plan_set.predicted_names(raw_name)]


def reapply_module_rules(device):
    """Re-apply module rules to every module on *device* after its virtual-chassis position changed.

    The whole device is one batch: its modules match against one enabled-rule snapshot, and one
    module type's interface templates are read once however many modules carry it. An unexpected
    failure stops the module batch and propagates to the deferred callback boundary.

    Returns the number of interfaces renamed across the device's modules.
    """
    from dcim.models import Module

    modules = list(
        Module.objects.filter(device=device).select_related(
            "module_type",
            "device__device_type",
            "device__platform",
            *family_template_names.BAY_CHAIN_RELATIONS,
        )
    )
    total = 0
    with pinned_rule_cache(), family_ops.pinned_template_cache(modules):
        for module in modules:
            if not module.module_bay:
                continue
            total += apply_interface_name_rules(module, module.module_bay, force_reapply=True) or 0
    return total


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
    """Return whether one device-interface rule matches one family parent."""
    if not rule.module_type_pattern:
        return True
    try:
        compiled = compile_module_type_pattern(rule.module_type_pattern)
    except ValidationError:
        return False
    return compiled.fullmatch(interface.name) is not None


def _apply_device_rule_to_families(device, vc_position, rule, families, claimed_pks):
    """Apply one rule to each eligible device-interface family."""
    total = 0
    for interface, children in families:
        if interface.pk in claimed_pks or not _matches_device_interface(rule, interface):
            continue
        port = interface.name.rsplit("/", 1)[-1]
        variables = {"vc_position": vc_position, "base": interface.name, "port": port}
        plan = family_ops.plan_device_interface_rename(device, rule, variables, interface, children)
        try:
            outcome = family_ops.execute_installed_plan(plan)
        except (IntegrityError, ValidationError):
            logger.exception(
                "Failed to apply rule %s to device interface %r on device %s; skipping.",
                rule.pk,
                interface.name,
                device.pk,
            )
            continue
        total += outcome.changed_count
        if outcome.status in {family_ops.FamilyStatus.CHANGED, family_ops.FamilyStatus.UNCHANGED}:
            claimed_pks.update(plan.member_pks)
    return total


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
    from dcim.models import Interface

    if not getattr(device, "virtual_chassis_id", None):
        return 0  # Only rename for VC members (vc_position must be set)

    if device.vc_position is None:
        return 0  # vc_position unset (e.g. VC master before position assigned)

    vc_position = str(device.vc_position)
    rules = _device_interface_rules(device)
    if not rules:
        return 0

    interfaces = list(Interface.objects.filter(device=device, module=None).order_by("pk"))
    if not interfaces:
        return 0

    families = family_ops.device_interface_families(interfaces)
    claimed_pks: set[int] = set()
    total = 0
    for rule in rules:
        total += _apply_device_rule_to_families(device, vc_position, rule, families, claimed_pks)

    return total


def _raw_name_matchers(module):
    """Delegate current and historical raw name resolution."""
    return family_template_names.raw_name_matchers(module)


def _get_raw_interface_names(module):
    """Return the original interface names NetBox assigned from templates."""
    return _raw_name_matchers(module).names


def _raw_name_patterns(module):
    """Delegate historical raw-name pattern construction."""
    return family_template_names.raw_name_patterns(module)


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
    kept = family_targets.one_family_per_name_set([(plan.base_name, plan.target_names) for plan in creations])
    return [creations[index] for index in kept]


def _process_module(rule, module, ifaces, variables, limit, results, module_qs, processed_pks):
    """Preview one module from its family plans.  Returns (checked_count, should_stop)."""
    plan_set = family_ops.plan_prospective_families(module, rule, variables, family_ops.describe_interfaces(ifaces))
    checked = len(plan_set.plans)
    if not checked:
        return 0, False
    rows_by_name = {iface.name: iface for iface in ifaces}
    existing_names = frozenset(rows_by_name)
    for plan in _preview_plans(rule, plan_set):
        entry = _plan_entry(module, plan, rows_by_name[_plan_root_name(plan)], existing_names)
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
        "device",
        "device__device_type",
        "device__platform",
        "device__virtual_chassis",
        "module_bay",
        "module_bay__parent",
    )
    # Batch-load all interfaces for matching modules to avoid N+1 queries.
    ifaces_by_module = defaultdict(list)
    for iface in Interface.objects.filter(module__in=module_qs).order_by("module_id", "name"):
        ifaces_by_module[iface.module_id].append(iface)

    processed_pks = set()
    results = []
    total_checked = 0
    for module in module_qs:
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
