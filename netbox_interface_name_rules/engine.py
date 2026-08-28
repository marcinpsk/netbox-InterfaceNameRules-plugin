# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Core renaming engine — rule lookup and interface rename logic.

This module is imported lazily by signals.py so that model imports happen
after Django is fully initialised.
"""

import logging
import re
from collections import defaultdict, namedtuple

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from . import family as family_ops
from . import naming, rule_selection
from .choices import BreakoutModeChoices
from .family import targets as family_targets
from .family import template_names as family_template_names

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


def _is_channel_child(iface):
    """Return True when *iface* is a channel subinterface bound to a parent's channel.

    Structural, not name-based: a row imported without ``full_clean()`` may carry a ``channel_id``
    without the channel type.  ``channel_id`` does not exist before NetBox 4.7, so this is False
    on every older release and the family paths below stay dormant there.
    """
    return getattr(iface, "channel_id", None) is not None


def _is_channelized_parent(iface):
    """Return True when *iface* declares a channel count.

    A channelized parent owns a family even when no subinterface is bound yet — this, not the
    presence of children, is what disables flat channel creation.
    """
    return getattr(iface, "channels", None) is not None


def _partition_families(interfaces):
    """Split *interfaces* into ``(bases, children_by_parent_pk)``.

    Bases are the interfaces a rule may match on its own: standalone interfaces and channelized
    parents.  Channel subinterfaces are never independent candidates — they are renamed only by
    following their parent, so they are grouped under it instead.
    """
    bases = []
    children = defaultdict(list)
    for iface in interfaces:
        if not _is_channel_child(iface):
            bases.append(iface)
            continue
        children[iface.parent_id].append(iface)  # pragma: no cover - requires channelization support
    for group in children.values():  # pragma: no cover - no channel children exist without support
        group.sort(key=lambda child: child.channel_id)
    return bases, children


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
        base = i.name if _is_channelized_parent(i) else i.name.rsplit(":", 1)[0]
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


def _plan_base(plan):
    """Return the interface facts a leftover plan is anchored on."""
    if isinstance(plan, family_ops.InstalledFamilyPlan):
        return plan.members[0].snapshot
    return plan.base


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


def _admitted_leftover(plans, rule, raw_names, force_reapply, matchers, module):
    """Return the plans for leftover interfaces the raw-name guard still claims."""
    bases = [_plan_base(plan) for plan in plans]
    selected = {
        interface.pk for interface in _collect_unrenamed(bases, rule, raw_names, force_reapply, matchers, module)
    }
    return [plan for plan, base in zip(plans, bases, strict=True) if base.pk in selected]


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
    interfaces = list(
        Interface.objects.using(family_ops.module_db_alias(module)).filter(module_id=module.pk).order_by("pk")
    )
    planned = family_ops.plan_module_families(module, rule, variables, interfaces)
    installed = _admitted_installed(planned.installed, rule, raw_names, force_reapply, raw.matchers, module)
    leftover = _admitted_leftover(planned.leftover, rule, raw_names, force_reapply, raw.matchers, module)

    outcomes = family_ops.execute_module_families(rule, module, [*installed, *leftover])
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

    Read-only — saves and mutates nothing.  Used by external integrations (e.g.
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


def _try_rename_device_interface(rule, iface, vc_position, device, renamed_pks, conflicts=None):
    """Attempt to rename a single device-level interface using *rule*.

    Returns ``True`` if the interface was successfully renamed, ``False`` otherwise.
    Mutates ``renamed_pks`` on success.

    A computed name already taken by another interface on the device is skipped
    with a tidy WARNING (no traceback), mirroring the module-install path; pass a
    list as *conflicts* to also collect them.  ``full_clean()`` remains the
    backstop for the rarer cross-member (VC) uniqueness violation.
    """
    if iface.pk in renamed_pks:
        return False  # Already renamed by a higher-priority rule

    if rule.module_type_pattern:
        try:
            if not re.fullmatch(rule.module_type_pattern, iface.name):
                return False
        except re.error:
            return False

    port = iface.name.rsplit("/", 1)[-1] if "/" in iface.name else iface.name
    variables = {"vc_position": vc_position, "base": iface.name, "port": port}

    try:
        new_name = evaluate_name_template(rule.name_template, variables)
    except (ValueError, TypeError, re.error):
        logger.exception(
            "Failed to evaluate template %r for interface %s (rule %s)",
            rule.name_template,
            iface.name,
            rule.pk,
        )
        return False

    if new_name == iface.name:
        return False

    # Pre-check device-scope name uniqueness so an expected collision is a clean
    # WARNING + skip instead of an ERROR traceback out of full_clean().
    if _name_exists_on_device(device, new_name, exclude_pk=iface.pk):
        _record_conflict(conflicts, device, iface.name, new_name, iface.pk)
        return False

    old_name = iface.name
    iface.name = new_name
    try:
        iface.full_clean()
    except ValidationError as exc:
        logger.warning(
            "Validation failed renaming device interface %r → %r (rule %s, device %s); skipping: %s",
            old_name,
            new_name,
            rule.pk,
            device.pk,
            exc,
        )
        iface.name = old_name
        return False
    try:
        iface.save()
    except (IntegrityError, ValidationError):
        logger.exception(
            "DB save failed for device interface %s → %s (rule %s, device %s)",
            old_name,
            new_name,
            rule.pk,
            device.pk,
        )
        iface.name = old_name
        return False

    renamed_pks.add(iface.pk)
    logger.debug("Renamed device interface %s → %s (rule %s, device %s)", old_name, new_name, rule.pk, device.pk)
    return True


def _try_rename_device_family(rule, iface, children, vc_position, device, renamed_pks, conflicts=None):
    """Rename a device-level interface with *rule* and carry its channel subinterfaces along.

    Returns the number of interfaces renamed.  The whole family is claimed in *renamed_pks* the
    moment its parent is renamed, so a lower-priority rule can never rename the leftovers of a
    family a higher-priority rule already took.

    Healing is best-effort here: device-level interfaces have no module template family to recover
    a suffix from, so a child that lost its parent's prefix in an earlier run is left alone.
    """
    parent_before = iface.name
    if not _try_rename_device_interface(rule, iface, vc_position, device, renamed_pks, conflicts):
        return 0
    count = 1
    for child, target in _child_target_names(  # pragma: no cover - requires channelization support
        children, parent_before, iface.name, module=None
    ):
        renamed_pks.add(child.pk)
        if target is None:
            logger.warning(
                "Cannot derive a name for channel interface %r from parent %r; leaving it unchanged.",
                child.name,
                iface.name,
            )
            continue
        count += _rename_for_family(child, target, device, conflicts).count
    return count


def reapply_module_rules(device):
    """Re-apply module rules to every module on *device* after its virtual-chassis position changed.

    The whole device is one batch: its modules match against one enabled-rule snapshot, and one
    module type's interface templates are read once however many modules carry it.  A module that
    fails is logged and left behind, so the rest of the device still follows the new position.

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
            try:
                total += apply_interface_name_rules(module, module.module_bay, force_reapply=True) or 0
            except Exception:
                logger.exception(
                    "Failed to re-apply rules for %s in %s after a virtual-chassis change",
                    module.module_type,
                    module.module_bay.name,
                )
    return total


def apply_device_interface_rules(device):
    """Rename device-level interfaces (module=None) when a device joins/changes position in a VC.

    Finds all enabled rules with ``applies_to_device_interfaces=True`` that match the device's
    type and platform, then renames any matching interfaces using the name_template.

    Template variables available: ``{vc_position}``, ``{base}`` (full current name),
    ``{port}`` (segment after the last ``/``, or the full name if no ``/`` present).

    Channel subinterfaces are not matched independently — they follow the parent whose family
    a rule wins, so a template like ``eth{vc_position}`` cannot collapse a whole family onto
    one name.

    Returns the number of interfaces renamed.
    """
    from dcim.models import Interface

    from .models import InterfaceNameRule

    if not getattr(device, "virtual_chassis_id", None):
        return 0  # Only rename for VC members (vc_position must be set)

    if device.vc_position is None:
        return 0  # vc_position unset (e.g. VC master before position assigned)

    vc_position = str(device.vc_position)
    device_type = getattr(device, "device_type", None)
    platform = getattr(device, "platform", None)

    from django.db.models import Q

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
            -(len(r.module_type_pattern or "") if r.applies_to_device_interfaces else 0),
            r.pk,
        )
    )

    if not rules:
        return 0

    interfaces = list(Interface.objects.filter(device=device, module=None))
    if not interfaces:
        return 0

    bases, children_by_parent = _partition_families(interfaces)
    total = 0
    renamed_pks: set[int] = set()
    for rule in rules:
        for iface in bases:
            total += _try_rename_device_family(
                rule, iface, children_by_parent.get(iface.pk, ()), vc_position, device, renamed_pks
            )

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


def _recovered_suffix(child, suffixes):  # pragma: no cover - requires channelization support
    """Return the template suffix for *child*'s channel, or None when it is not unambiguous.

    Once a parent has been renamed there is no reliable way back from a stranded child to the family
    it belongs to, so a channel spelled differently by two families is left alone rather than guessed.
    """
    candidates = suffixes.get(child.channel_id) or set()
    if len(candidates) == 1:
        return next(iter(candidates))
    if candidates:
        logger.warning(
            "Channel %s is spelled %s by different families of this module type; "
            "cannot recover a name for interface %r.",
            child.channel_id,
            sorted(candidates),
            child.name,
        )
    return None


def _child_target_names(children, parent_before, parent_after, module):
    """Pair every child with the name it takes when its parent is renamed to *parent_after*.

    The suffix is read from the child's own name against *parent_before* (the parent's name before
    this run's rename); when the child no longer carries that prefix the suffix is recovered from
    the module's template family instead.  A child that neither shares the prefix nor has an
    unambiguous template pairing is returned with a None target — the engine leaves it alone rather
    than guessing at a free-form name.
    """
    suffixes = None
    targets = []
    for child in children:  # pragma: no cover - requires channelization support
        suffix = family_targets.child_name_suffix(child.name, parent_before)
        if suffix is None and module is not None:
            if suffixes is None:
                suffixes = family_ops.template_channel_suffixes(family_ops.resolved_template_names(module))
            suffix = _recovered_suffix(child, suffixes)
        targets.append((child, None if suffix is None else parent_after + suffix))
    return targets


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


def _name_exists_on_device(device, name, exclude_pk=None):
    """Return True if another interface on *device* already uses *name*.

    Pre-checks the per-device interface-name uniqueness NetBox enforces so a
    rename/create that would collide is skipped cleanly instead of raising
    mid-transaction.  (VC-wide uniqueness is not pre-checked here; full_clean()
    remains the authoritative validator for that rarer cross-member case.)
    """
    from dcim.models import Interface

    qs = Interface.objects.filter(device=device, name=name)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _record_skip(conflicts, device, current_name, attempted_name, interface_pk=None):
    """Append a skipped rename to *conflicts* when the caller is collecting them.

    The caller has already logged why it skipped; this only lets the interactive Apply view report
    how many renames were dropped.
    """
    if conflicts is not None:
        conflicts.append(
            {
                "device": str(device),
                "current_name": current_name,
                "attempted_name": attempted_name,
                "interface_pk": interface_pk,
            }
        )


def _record_conflict(conflicts, device, current_name, attempted_name, interface_pk=None):
    """Log a name collision at WARNING and record it as a skipped rename.

    Collisions are expected during automatic renaming (module install, type
    change, VC change) when the computed name is already taken on the device;
    they must never abort the batch, so callers skip the rename and carry on.
    """
    logger.warning(
        "Interface name %r already exists on device %s — skipping rename of %r → %r",
        attempted_name,
        device,
        current_name,
        attempted_name,
    )
    _record_skip(conflicts, device, current_name, attempted_name, interface_pk)


# What a family-aware rename did, so the children can act on their parent's outcome rather than on
# its computed target name (which says nothing about whether the parent actually took it).
_RENAMED = "renamed"
_UNCHANGED = "unchanged"
_COLLISION = "collision"
_ERROR = "error"

_RenameResult = namedtuple("_RenameResult", ("target_name", "outcome", "count"))


def _rename_in_place(iface, new_name, device, conflicts):
    """Rename *iface* to *new_name*; return 1 if renamed, 0 if no-op or collision."""
    if new_name == iface.name:
        return 0
    if _name_exists_on_device(device, new_name, exclude_pk=iface.pk):
        _record_conflict(conflicts, device, iface.name, new_name, iface.pk)
        return 0
    iface.name = new_name
    iface.full_clean()
    iface.save()
    return 1


def _rename_for_family(iface, new_name, device, conflicts):  # pragma: no cover - requires channelization support
    """Rename *iface* as part of a family walk, reporting the outcome instead of raising.

    The save runs in its own savepoint so an unexpected failure on one member leaves the
    surrounding transaction usable: family processing is best-effort per interface, and only a
    failed *parent* stops the rest of its family.
    """
    if new_name == iface.name:
        return _RenameResult(new_name, _UNCHANGED, 0)
    old_name = iface.name
    try:
        with transaction.atomic():
            renamed = _rename_in_place(iface, new_name, device, conflicts)
    except (ValueError, ValidationError, IntegrityError):
        logger.exception("Failed to rename interface %r → %r on device %s; skipping.", old_name, new_name, device)
        iface.name = old_name
        return _RenameResult(new_name, _ERROR, 0)
    return _RenameResult(new_name, _RENAMED, 1) if renamed else _RenameResult(new_name, _COLLISION, 0)


def _is_channelized_rule(rule):
    """Return True when *rule* asks for the channelized topology instead of flat sibling interfaces."""
    return rule.breakout_mode == BreakoutModeChoices.CHANNELIZED


def _matching_moduletype_pks(module_type_pattern):
    """Return PKs of ModuleTypes whose model name matches the given regex pattern.

    Raises ValueError for invalid regex patterns, mirroring evaluate_name_template's
    error-handling convention so callers can treat both as ValueError.
    """
    from dcim.models import ModuleType

    try:
        compiled = re.compile(module_type_pattern)
    except re.error as exc:
        raise ValueError(f"Invalid module_type_pattern regex '{module_type_pattern}': {exc}") from exc
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
    except (ValueError, re.error):
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


def _name_detail(name, role, channel_id=None) -> dict:
    """Describe one previewed name so the UI can render a family as a family.

    *role* is ``interface`` (a plain rename), ``parent`` (the family's physical interface) or
    ``channel``; *channel_id* is the parent channel a channel name is bound to, when known.
    """
    return {"name": name, "role": role, "channel_id": channel_id}


_PREVIEW_ROLES = {
    family_ops.MemberRole.PARENT: "parent",
    family_ops.MemberRole.CHANNEL: "channel",
}


def _member_detail(plan, member) -> dict:
    """Describe one planned member so the UI can render a family as a family.

    A flat family's members are the channels a breakout rule spells out; a plan that holds only
    one of them is a plain rename, not a family.
    """
    role = _PREVIEW_ROLES.get(member.role) or ("channel" if len(plan.members) > 1 else "interface")
    return _name_detail(member.target_name, role, member.channel_id)


def _plan_details(plan) -> list:
    """Describe every name the plan intends, or the error that stopped it from naming them."""
    if plan.precondition_status != family_ops.FamilyStatus.FAILED:
        return [_member_detail(plan, member) for member in plan.members]
    root = _member_detail(plan, plan.members[0])
    return [_name_detail(f"<error: {plan.precondition_reason}>", root["role"], root["channel_id"])]


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
        "new_names": [detail["name"] for detail in details],
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
            "name_details": list[dict],   # {"name", "role", "channel_id"} per new_names entry
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
