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
from .family import template_names as family_template_names
from .rule_selection import _compile_pattern

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
    """Return True when this NetBox models channelized subinterfaces (NetBox 4.7+).

    Probed from the Interface model rather than a version comparison, so a backport or a
    development build is detected by what it actually provides.
    """
    from dcim.models import Interface
    from django.core.exceptions import FieldDoesNotExist

    try:
        Interface._meta.get_field("channel_id")
    except FieldDoesNotExist:
        return False
    return True  # pragma: no cover - only reachable on a NetBox that models channelization


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


def _child_name_suffix(child_name, parent_name):  # pragma: no cover - requires channelization support
    """Return the suffix *child_name* adds to *parent_name*, or None when it adds none.

    The first character must be non-alphanumeric so ``et0``/``et01`` is never mistaken for a
    family; the punctuation itself is free-form (``:``, ``-``, ``_`` and ``@`` all occur in the
    wild), so it is not restricted to a fixed separator.
    """
    if not parent_name or not child_name.startswith(parent_name):
        return None
    suffix = child_name[len(parent_name) :]
    if not suffix or suffix[0].isalnum():
        return None
    return suffix


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


def apply_interface_name_rules(module, module_bay, force_reapply=False):
    """Apply InterfaceNameRule rename after module installation.

    Looks up a matching rule for (module_type, parent_module_type, device_type, platform)
    and renames interfaces created by NetBox's template instantiation.

    Only processes interfaces whose name still matches the raw bay position
    (i.e., haven't been renamed yet), ensuring idempotency.  Pass
    ``force_reapply=True`` to skip this check and re-apply rules to ALL
    module interfaces (used when vc_position or other variables change).

    A channelized parent and its channel subinterfaces are processed as one family: the parent
    decides, the children follow it (see ``_apply_rule_with_family``).

    Returns:
        Number of interfaces renamed/created, or 0 if no rule matched.

    """
    from dcim.models import Interface

    device_type = module.device.device_type if module.device else None
    platform = module.device.platform if module.device else None
    rule = find_matching_rule(module.module_type, _get_parent_module_type(module_bay), device_type, platform)

    if not rule:
        return 0

    variables = build_variables(module_bay, device=module.device)
    plan_set = family_ops.InstalledFamilyPlanSet(module_id=module.pk, plans=())
    family_outcome = family_ops.InstalledPlanSetOutcome(families=())
    if supports_channelization() or (force_reapply and rule.channel_count > 0):
        try:
            plan_set = family_ops.plan_installed_families(module, rule, variables)
        except (TypeError, ValueError, re.error):
            logger.exception("Failed to plan installed families for module %s; using the legacy plain path.", module)
        else:
            family_outcome = family_ops.execute_installed_plan_set(plan_set)

    interfaces = list(Interface.objects.filter(module=module).exclude(pk__in=plan_set.member_pks))

    if not interfaces:
        return family_outcome.changed_count

    # Only bases are rule candidates; the idempotency guard therefore looks at them alone.
    bases, children_by_parent = _partition_families(interfaces)
    # Determine raw names NetBox assigned from templates; fall back to bay_position.
    raw = _raw_name_matchers(module)
    raw_names = raw.names or {variables["bay_position"]}
    unrenamed = _collect_unrenamed(bases, rule, raw_names, force_reapply, raw.matchers, module)

    if not unrenamed:
        return family_outcome.changed_count  # Already renamed (idempotent guard)

    # A breakout rule on a module that has channelized families processes only those families —
    # the same rule the preview and bulk-apply paths follow.
    installed_channelized = any(plan.topology == family_ops.FamilyTopology.CHANNELIZED for plan in plan_set.plans)
    families_only = rule.channel_count > 0 and (
        installed_channelized or any(_is_channelized_parent(base) for base in bases)
    )

    renamed = family_outcome.changed_count
    families_seen = bool(plan_set.plans) or families_only
    conflicts: list = [
        member
        for result in family_outcome.families
        for member in result.members
        if member.status == family_ops.FamilyStatus.BLOCKED
    ]
    for iface in unrenamed:
        children = children_by_parent.get(iface.pk, ())
        if families_only and not _is_channelized_parent(iface):  # pragma: no cover - see families_only above
            logger.debug(
                "Interface %r is not channelized; skipping it while rule '%s' breaks out this module's families.",
                iface.name,
                rule,
            )
            continue
        families_seen = families_seen or bool(children) or _is_channelized_parent(iface)
        try:
            count = _apply_rule_with_family(rule, iface, children, variables, module, conflicts)
        except (ValueError, ValidationError, IntegrityError):
            # The collision pre-check closes the common case, but a concurrent
            # insert can still win between that check and the save — surfacing
            # here as IntegrityError/ValidationError out of the per-interface
            # atomic block (which has already rolled back cleanly).  Log and keep
            # going so one racing interface never aborts the whole install batch,
            # mirroring apply_rule_to_existing().
            logger.exception(
                "Failed to apply rule '%s' to interface '%s' (id=%s); skipping.",
                rule,
                iface.name,
                iface.pk,
            )
            continue
        if count is None:
            # A structural skip (unsupported topology, channel-count mismatch) says nothing about the rule.
            families_seen = True
            continue
        renamed += count

    if unrenamed and renamed == 0 and not conflicts and not families_seen:
        # All interfaces already have the names the rule would produce — flag as
        # potentially obsolete (e.g., newer NetBox generates correct names natively).
        # Skipped when the 0-count was caused by name collisions (a different reason
        # than a no-op rule), so a collision never mislabels the rule as deprecated.
        # Skipped for channelized families too: a structural skip, or a family whose parent
        # deliberately keeps its raw name, says nothing about the rule being obsolete.
        _flag_rule_potentially_deprecated(rule)

    return renamed


def _predicted_channel_name(rule, raw_name, variables, parents, children):  # pragma: no cover - channelized only
    """Return the name the channel template named *raw_name* takes under *rule*."""
    parent_name, channel_id = children[raw_name]
    if rule.channel_count > 0:
        if parents.get(parent_name) != rule.channel_count:
            return raw_name  # channel-count mismatch: the apply path skips the whole family
        channel = str(rule.channel_start + channel_id - 1)
        return evaluate_name_template(rule.name_template, {**variables, "base": parent_name, "channel": channel})
    # Simple rule: the channel follows its parent, keeping the suffix it adds to the parent's name.
    parent_target = evaluate_name_template(rule.name_template, {**variables, "base": parent_name})
    suffix = _child_name_suffix(raw_name, parent_name)
    return raw_name if suffix is None else parent_target + suffix


def _predicted_family_parent_name(rule, raw_name, variables, parents):  # pragma: no cover - channelized only
    """Return the name the parent template named *raw_name* takes under *rule*.

    Only a channelized rule that names its parent renames it, and only when the family's channel
    count is the one the rule describes — the same two conditions the apply path applies.
    """
    if not (_is_channelized_rule(rule) and rule.parent_name_template):
        return raw_name
    if parents.get(raw_name) != rule.channel_count:
        return raw_name  # channel-count mismatch: the apply path skips the whole family
    return evaluate_name_template(rule.parent_name_template, {**variables, "base": raw_name})


def _predicted_names(rule, raw_name, variables, parents, children, family_blocked=False):
    """Return the names *raw_name* predicts to under *rule*.

    A name the module type's templates describe as a channelized parent or channel follows its
    family; a channelized rule on a plain name predicts the family it would build there, unless
    *family_blocked* says the apply path refuses to build it; anything else keeps the per-name
    prediction, expanding once per channel for a breakout rule and once for a simple one.
    """
    if raw_name in parents:  # pragma: no cover - requires a NetBox that models channelization
        if rule.channel_count > 0:
            # The rule renames the family's existing channels; only a parent template moves the parent.
            return [_predicted_family_parent_name(rule, raw_name, variables, parents)]
        return [evaluate_name_template(rule.name_template, {**variables, "base": raw_name})]
    if raw_name in children:  # pragma: no cover - requires a NetBox that models channelization
        return [_predicted_channel_name(rule, raw_name, variables, parents, children)]
    if rule.channel_count > 0 and _is_channelized_rule(rule):
        if family_blocked or not supports_channelization():
            return [raw_name]  # the apply path builds nothing here
        parent_name, channels = _channelized_family_names(rule, raw_name, variables)  # pragma: no cover - see above
        return [parent_name, *(name for _, name in channels)]  # pragma: no cover - see above
    vars_copy = {**variables, "base": raw_name}
    if rule.channel_count > 0:
        return [
            evaluate_name_template(rule.name_template, {**vars_copy, "channel": str(rule.channel_start + ch)})
            for ch in range(rule.channel_count)
        ]
    return [evaluate_name_template(rule.name_template, vars_copy)]


def predict_rule_output(module, module_bay, raw_names):
    """Predict the names apply_interface_name_rules would produce for raw_names.

    Read-only — saves and mutates nothing.  A channelized rule additionally counts the module's
    interfaces, because the apply path refuses to convert a module that already carries a flat
    breakout family and the prediction has to say the same.  Used by external integrations (e.g.,
    netbox-librenms-plugin) that need to know the post-rename names without applying any rule.

    For breakout rules (channel_count > 0), each raw name expands to
    channel_count predicted names. For simple renames, one name in → one name
    out. Returns raw_names unchanged when no rule matches or evaluation fails.

    A name the module type's interface templates describe as part of a channelized family is
    predicted as the apply path treats it instead: the family's channels are renamed in place, so a
    breakout rule leaves the parent's name alone and maps each channel through its ``channel_id``
    rather than expanding one name into a flat set.  Names no template claims keep the per-name
    prediction, so a module type without channelized templates is unaffected.

    Precondition: *raw_names* are resolved by the caller at call time.  A name captured before the
    device's virtual-chassis position changed is predicted from itself, not corrected to the name
    the templates resolve to now — this function maps the names it is given.
    """
    device_type = module.device.device_type if module.device else None
    platform = module.device.platform if module.device else None
    rule = find_matching_rule(module.module_type, _get_parent_module_type(module_bay), device_type, platform)
    if not rule:
        return list(raw_names)

    variables = build_variables(module_bay, device=module.device)
    parents, children = _template_families(module)
    # Costs one count pair, and only where a channelized rule could otherwise predict a family.
    family_blocked = (
        rule.channel_count > 0
        and _is_channelized_rule(rule)
        and supports_channelization()
        and _has_flat_expansion(module)
    )

    output = []
    for raw_name in raw_names:
        try:
            output.extend(_predicted_names(rule, raw_name, variables, parents, children, family_blocked))
        except (ValueError, TypeError, re.error):
            # Template eval failed; apply path would also fail and leave the
            # interface alone, so the predicted name is the raw name.
            output.append(raw_name)

    return output


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


# The bay chain InterfaceTemplate.resolve_name() dereferences while resolving {module}.
_BAY_CHAIN_RELATIONS = family_template_names.BAY_CHAIN_RELATIONS
_RawMatcher = family_template_names.RawMatcher
_RawNames = family_template_names.RawNames

# Brace-free stand-in used to recover a flat family's historical base from rule-output names.
_BASE_SENTINEL = "InrBaseSentinelEnd"


def _module_with_bay_chain(module):
    """Delegate template-resolution loading while preserving the engine helper."""
    return family_template_names.module_with_bay_chain(module)


def _raw_matchers(templates, module):
    """Delegate raw template resolution while preserving the engine helper."""
    return family_template_names.raw_matchers(templates, module)


def _raw_name_matchers(module):
    """Delegate current and historical raw name resolution."""
    return family_template_names.raw_name_matchers(module)


def _get_raw_interface_names(module):
    """Return the original interface names NetBox assigned from templates."""
    return _raw_name_matchers(module).names


def _raw_name_patterns(module):
    """Delegate historical raw-name pattern construction."""
    return family_template_names.raw_name_patterns(module)


def _raw_names_by_module(modules):  # pragma: no cover - only the conversion scan batches names
    """Delegate batched raw-name resolution."""
    return family_template_names.raw_names_by_module(modules)


def _template_families(module):
    """Return ``(parents, children)`` describing *module*'s channelized interface templates.

    *parents* maps a channelized parent template's resolved name to its channel count; *children*
    maps each channel template's resolved name to ``(parent_name, channel_id)``.  Both are empty
    where nothing can be channelized, so callers keep their pre-channelization behaviour without
    paying for a template scan.
    """
    if not supports_channelization():
        return {}, {}
    return _resolve_template_families(module)  # pragma: no cover - requires channelization support


def _resolve_template_families(module):  # pragma: no cover - requires a NetBox that models channelization
    """Resolve *module*'s interface templates into the channelized families they describe.

    Pairing through ``InterfaceTemplate.parent`` (rather than matching against the flat set of raw
    names) keeps ambiguous prefixes like ``xe``/``xe-0`` apart, and a channel template whose parent
    declares no channel count is not a family at all.
    """
    from dcim.models import InterfaceTemplate

    module_fresh = _module_with_bay_chain(module)
    templates = list(InterfaceTemplate.objects.filter(module_type=module_fresh.module_type))
    resolved = {tmpl.pk: tmpl.resolve_name(module_fresh) for tmpl in templates}
    parents_by_pk = {
        tmpl.pk: (resolved[tmpl.pk], tmpl.channels) for tmpl in templates if getattr(tmpl, "channels", None) is not None
    }
    children = {}
    for tmpl in templates:
        channel_id = getattr(tmpl, "channel_id", None)
        parent = parents_by_pk.get(getattr(tmpl, "parent_id", None))
        if channel_id is None or parent is None:
            continue
        parent_name, _channels = parent
        children[resolved[tmpl.pk]] = (parent_name, channel_id)
    return dict(parents_by_pk.values()), children


def _template_channel_suffixes(module):  # pragma: no cover - requires a NetBox that models channelization
    """Map ``channel_id`` → the set of name suffixes *module*'s interface templates give that channel.

    The suffix comes from the template family itself — each channel template's resolved name minus
    its parent template's resolved name — so a child that lost its parent's prefix in an earlier
    partial rename can still be repaired.  A module type with several families may spell the same
    channel differently in each (``et0:2`` vs ``sw0.2``), so the suffixes are collected per channel
    rather than overwritten: the recovery only uses one when every family agrees on it.
    """
    suffixes = defaultdict(set)
    for child_name, (parent_name, channel_id) in _template_families(module)[1].items():
        suffix = _child_name_suffix(child_name, parent_name)
        if suffix is not None:
            suffixes[channel_id].add(suffix)
    return suffixes


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
        suffix = _child_name_suffix(child.name, parent_before)
        if suffix is None and module is not None:
            if suffixes is None:
                suffixes = _template_channel_suffixes(module)
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


def _restore_deferred_channel_names(reconciliations, db_alias):  # pragma: no cover - channelization only
    """Restore plugin-owned names that NetBox's parent cascade changed after commit."""
    from dcim.models import Interface

    child_pks = [child_pk for child_pk, _final_name, _cascade_name in reconciliations]
    with transaction.atomic(using=db_alias):
        children = Interface.objects.using(db_alias).select_for_update().select_related("device").in_bulk(child_pks)
        for child_pk, final_name, cascade_name in reconciliations:
            child = children.get(child_pk)
            if child is None or child.name == final_name:
                continue
            if child.name != cascade_name:
                logger.warning(
                    "Channel interface %s changed to unexpected name %r before deferred reconciliation; "
                    "leaving it unchanged.",
                    child_pk,
                    child.name,
                )
                continue
            previous_name = child.name
            try:
                with transaction.atomic(using=db_alias):
                    child.name = final_name
                    child.full_clean()
                    child.save(using=db_alias)
            except (ValueError, ValidationError, IntegrityError):
                child.name = previous_name
                logger.exception(
                    "Failed to restore channel interface %s from NetBox's deferred name %r to %r; skipping.",
                    child_pk,
                    cascade_name,
                    final_name,
                )


def _preserve_names_across_parent_cascade(parent, parent_before, final_names):  # pragma: no cover
    """Run after NetBox's deferred cascade when a rule intentionally keeps old-parent child names."""
    if parent.name == parent_before:
        return

    reconciliations = []
    for child, final_name in final_names:
        old_conventional_name = f"{parent_before}:{child.channel_id}"
        cascade_name = f"{parent.name}:{child.channel_id}"
        if final_name == old_conventional_name and final_name != cascade_name:
            reconciliations.append((child.pk, final_name, cascade_name))
    if not reconciliations:
        return

    reconciliations = tuple(reconciliations)
    db_alias = parent._state.db
    transaction.on_commit(
        lambda: _restore_deferred_channel_names(reconciliations, db_alias),
        using=db_alias,
    )


def _rename_channel_children(parent, parent_before, children, module, conflicts):  # pragma: no cover - see above
    """Carry the parent's new name onto its channel subinterfaces; return how many were renamed."""
    count = 0
    for child, target in _child_target_names(children, parent_before, parent.name, module):
        if target is None:
            logger.warning(
                "Cannot derive a name for channel interface %r from parent %r; leaving it unchanged.",
                child.name,
                parent.name,
            )
            continue
        count += _rename_for_family(child, target, module.device, conflicts).count
    return count


def _apply_simple_rule_to_family(rule, parent, children, variables, module, conflicts):  # pragma: no cover
    """Rename a channelized family in lockstep with its parent; return the count renamed.

    The children act on the parent's *outcome*, never on its computed target: a parent that
    collided or failed to save leaves the whole family untouched, while a parent that already
    carries the right name still lets a stale child be repaired.
    """
    parent_before = parent.name
    new_name = evaluate_name_template(rule.name_template, {**variables, "base": parent.name})
    result = _rename_for_family(parent, new_name, module.device, conflicts)
    if result.outcome in (_COLLISION, _ERROR):
        logger.debug("Family of %r left unchanged: the parent could not be renamed to %r.", parent.name, new_name)
        return result.count
    return result.count + _rename_channel_children(parent, parent_before, children, module, conflicts)


def _rename_family_parent(rule, parent, variables, module, conflicts):  # pragma: no cover - see above
    """Rename an existing family's parent per the rule's parent template; return its rename outcome.

    Only a channelized rule that names its parent touches it — a flat rule, or a blank parent
    template, leaves the parent the name it already has.
    """
    if not (_is_channelized_rule(rule) and rule.parent_name_template):
        return _RenameResult(parent.name, _UNCHANGED, 0)
    target = evaluate_name_template(rule.parent_name_template, {**variables, "base": parent.name})
    return _rename_for_family(parent, target, module.device, conflicts)


def _apply_breakout_rule_to_family(rule, parent, children, variables, module, conflicts):  # pragma: no cover
    """Rename an already-channelized family; return the count renamed, or None when skipped.

    Nothing is ever created here: the channels the rule describes are rows NetBox already models,
    so a breakout rule renames them in place.  The parent is renamed only when the rule builds
    channelized families and names their parent; the channels' ``{base}`` stays the parent's name
    as it was before that rename.  A rule whose channel count disagrees with the hardware is a
    modelling mismatch — the family is skipped whole rather than renamed into a shape it does not
    have, and a parent that could not take its name stops the family the same way a simple rule's
    does.

    Every child's name is computed before the first save, so a template that only fails on a later
    channel (channel-dependent arithmetic) aborts the family untouched instead of half renaming it.
    """
    if getattr(parent, "channels", None) != rule.channel_count:
        logger.warning(
            "Interface %r provides %s channels but rule '%s' defines %s; skipping the family.",
            parent.name,
            getattr(parent, "channels", None),
            rule,
            rule.channel_count,
        )
        return None
    base_name = parent.name
    targets = [
        (
            child,
            evaluate_name_template(
                rule.name_template,
                {**variables, "base": base_name, "channel": str(rule.channel_start + child.channel_id - 1)},
            ),
        )
        for child in children
    ]
    result = _rename_family_parent(rule, parent, variables, module, conflicts)
    if result.outcome in (_COLLISION, _ERROR):
        logger.debug(
            "Family of %r left unchanged: the parent could not be renamed to %r.", base_name, result.target_name
        )
        return result.count
    count = result.count
    final_names = []
    for child, new_name in targets:
        previous_name = child.name
        child_result = _rename_for_family(child, new_name, module.device, conflicts)
        count += child_result.count
        final_name = new_name if child_result.outcome in (_RENAMED, _UNCHANGED) else previous_name
        final_names.append((child, final_name))
    _preserve_names_across_parent_cascade(parent, base_name, final_names)
    return count


def _is_channelized_rule(rule):
    """Return True when *rule* asks for the channelized topology instead of flat sibling interfaces."""
    return rule.breakout_mode == BreakoutModeChoices.CHANNELIZED


def _channelized_family_names(rule, base_name, variables):  # pragma: no cover - requires channelization support
    """Return ``(parent_name, [(channel_id, name), ...])`` for the family *rule* builds on *base_name*.

    ``{base}`` is the base interface's current name for the parent and every channel; ``{channel}``
    is ``channel_start + channel_id - 1``.  A blank parent template leaves the base's name alone.
    Takes the name rather than the interface so prediction can reuse it without a row to point at.
    """
    family_vars = {**variables, "base": base_name}
    parent_name = base_name
    if rule.parent_name_template:
        parent_name = evaluate_name_template(rule.parent_name_template, family_vars)
    channels = [
        (
            channel_id,
            evaluate_name_template(
                rule.name_template, {**family_vars, "channel": str(rule.channel_start + channel_id - 1)}
            ),
        )
        for channel_id in range(1, rule.channel_count + 1)
    ]
    return parent_name, channels


def _has_flat_expansion(module):  # pragma: no cover - requires channelization support
    """Return True when *module* carries more interfaces than its module type's templates describe.

    A flat breakout leaves N-1 rows beyond the templates, so the surplus is the structural mark of a
    family an earlier apply installed.  Counting templates rather than their resolved names keeps
    two templates that resolve to the same string from reading as one.
    """
    from dcim.models import Interface, InterfaceTemplate

    templates = InterfaceTemplate.objects.filter(module_type_id=module.module_type_id).count()
    return Interface.objects.filter(module=module).count() > templates


def _first_taken_name(device, names, exclude_pk):  # pragma: no cover - requires channelization support
    """Return the first of *names* already used by another interface on *device*, or None."""
    for name in names:
        if _name_exists_on_device(device, name, exclude_pk=exclude_pk):
            return name
    return None


def _build_channelized_family(rule, base, variables, module, conflicts):  # pragma: no cover - see above
    """Turn a plain base interface into a channelized family; return how many rows it changed.

    The whole family is preflighted before anything is written: a module that already carries a flat
    family, or a single occupied name — the parent's or any channel's — leaves the base exactly as
    it was instead of half converting it.
    """
    from dcim.choices import InterfaceTypeChoices
    from dcim.models import Interface

    device = module.device
    base_name = base.name
    parent_name, channels = _channelized_family_names(rule, base_name, variables)
    if _has_flat_expansion(module):
        # Converting one sibling into a parent would strand the others beside the new family.
        logger.warning(
            "Module %s already carries a flat breakout family; rule '%s' will not convert interface "
            "%r into the channelized parent %r — converting an installed family is a separate, "
            "explicit operation. Skipping.",
            module,
            rule,
            base.name,
            parent_name,
        )
        _record_skip(conflicts, device, base.name, parent_name, base.pk)
        return 0
    blocker = _first_taken_name(device, [parent_name, *(name for _, name in channels)], base.pk)
    if blocker is not None:
        logger.warning(
            "Cannot build the channelized family for interface %r on device %s: %r is already taken; skipping.",
            base.name,
            device,
            blocker,
        )
        _record_skip(conflicts, device, base.name, blocker, base.pk)
        return 0

    count = 0
    with transaction.atomic():
        created_channels = []
        base.channels = rule.channel_count
        if parent_name != base.name:
            base.name = parent_name
            count += 1
        base.full_clean()
        base.save()
        for channel_id, name in channels:
            channel = Interface(
                device=device,
                module=module,
                name=name,
                type=InterfaceTypeChoices.TYPE_CHANNEL,
                parent=base,
                channel_id=channel_id,
                enabled=base.enabled,
            )
            channel.full_clean()
            channel.save()
            created_channels.append((channel, name))
            count += 1
        _preserve_names_across_parent_cascade(base, base_name, created_channels)
    return count


def _apply_channelized_rule(rule, base, variables, module, conflicts):
    """Build the channelized family *rule* describes on a plain base interface.

    Returns None where NetBox cannot model channels: the rule describes a topology this release has
    no rows for, and building a flat family instead would silently give the operator another one.
    """
    if not supports_channelization():
        logger.warning(
            "Rule '%s' builds a channelized family, which this NetBox release cannot model; "
            "leaving interface %r unchanged.",
            rule,
            base.name,
        )
        return None
    return _build_channelized_family(rule, base, variables, module, conflicts)  # pragma: no cover - see above


def _apply_rule_with_family(rule, iface, children, variables, module, conflicts):
    """Apply *rule* to *iface*, carrying its channel subinterfaces along.

    Returns the number of interfaces renamed/created, or None when a channelized family was
    skipped for a structural reason.  Interfaces that own no family take the plain path unchanged.
    """
    if rule.channel_count > 0 and (_is_channelized_parent(iface) or children):  # pragma: no cover
        return _apply_breakout_rule_to_family(rule, iface, children, variables, module, conflicts)
    if children:  # pragma: no cover - requires channelization support
        return _apply_simple_rule_to_family(rule, iface, children, variables, module, conflicts)
    if rule.channel_count > 0 and _is_channelized_rule(rule):
        return _apply_channelized_rule(rule, iface, variables, module, conflicts)
    return _apply_rule_to_interface(rule, iface, {**variables, "base": iface.name}, module, conflicts=conflicts)


def _create_channel(iface, module, new_name, device, conflicts):
    """Create a breakout channel interface *new_name*; return 1 if created, else 0.

    Silently skips when this module already has the channel (idempotent
    re-apply); records a conflict when *new_name* is taken by a different
    interface on the device.
    """
    from dcim.models import Interface

    if Interface.objects.filter(module=module, name=new_name).exists():
        return 0  # idempotent: channel already created on this module
    if _name_exists_on_device(device, new_name):
        _record_conflict(conflicts, device, iface.name, new_name, iface.pk)
        return 0
    breakout_iface = Interface(
        device=device,
        module=module,
        name=new_name,
        type=iface.type,
        enabled=iface.enabled,
    )
    breakout_iface.full_clean()
    breakout_iface.save()
    return 1


def _apply_rule_to_interface(rule, iface, variables, module, conflicts=None):
    """Apply a single rule to an interface, handling breakout channels.

    All saves are wrapped in a transaction so a failure mid-breakout rolls
    back any partially created interfaces.  A computed name that already exists
    on the device is skipped (logged, and recorded in *conflicts* when a list
    is passed) instead of raising — so automatic renaming (module install,
    module-type change, VC change) never aborts the rest of the batch on a
    name collision.

    Returns the number of interfaces renamed/created.
    """
    count = 0
    device = module.device

    with transaction.atomic():
        if rule.channel_count > 0:
            # Breakout: rename base interface and create additional channel interfaces
            for ch in range(rule.channel_count):
                variables["channel"] = str(rule.channel_start + ch)
                new_name = evaluate_name_template(rule.name_template, variables)
                if ch == 0:
                    count += _rename_in_place(iface, new_name, device, conflicts)
                else:
                    count += _create_channel(iface, module, new_name, device, conflicts)
        else:
            # Simple rename (converter offset, platform naming, etc.)
            new_name = evaluate_name_template(rule.name_template, variables)
            count += _rename_in_place(iface, new_name, device, conflicts)

    return count


def _find_channel_base(rule, ifaces, variables):
    """Find the best 'base' interface for a channel rule on a single module.

    Prefers an interface whose current name already equals the expected ch=0 name
    (i.e. it has already been renamed to channel 0 and is safe to re-process).
    Falls back to the first interface (alphabetically) so that on first apply,
    the template-created base interface becomes channel 0.

    This ensures apply_rule_to_existing / find_interfaces_for_rule call
    _apply_rule_to_interface exactly ONCE per module for channel rules, preventing
    duplicate-name IntegrityErrors when channels already exist.
    """
    if not ifaces:
        return None
    for iface in ifaces:
        vars_copy = dict(variables)
        vars_copy["base"] = iface.name
        vars_copy["channel"] = str(rule.channel_start)  # ch=0
        try:
            ch0_name = evaluate_name_template(rule.name_template, vars_copy)
            if iface.name == ch0_name:
                return iface
        except ValueError:
            pass
    return ifaces[0]


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


def _family_entry(module, parent, details, children) -> dict | None:
    """Build a family preview entry from per-name *details*, or None when nothing would change.

    The entry stays keyed on the parent — the PK the Apply view submits — and lists the family's
    names in ``new_names``, so the existing template loop keeps working unchanged.
    """
    if [detail["name"] for detail in details] == [parent.name, *(child.name for child in children)]:
        return None
    return {
        "module": module,
        "interface": parent,
        "current_name": parent.name,
        "new_names": [detail["name"] for detail in details],
        "name_details": details,
    }


def _evaluate_plain_interface(rule, module, iface, variables, children=()) -> dict | None:
    """Return a result dict if *iface* or one of its channels would be renamed by *rule*, else None.

    A channelized parent is previewed together with its channels, so the Apply page shows the whole
    family behind the one PK it submits.
    """
    vars_copy = {**variables, "base": iface.name}
    try:
        new_name = evaluate_name_template(rule.name_template, vars_copy)
    except ValueError as exc:
        new_name = f"<error: {exc}>"
    if children:  # pragma: no cover - requires channelization support
        return _family_entry(module, iface, _lockstep_details(new_name, iface, children, module), children)
    return _family_entry(module, iface, [_name_detail(new_name, "interface")], ())


def _lockstep_details(new_name, parent, children, module) -> list:  # pragma: no cover - see above
    """Per-name preview details for a family renamed in lockstep with its parent.

    A channel whose suffix cannot be derived previews as unchanged — the same thing the apply path
    does with it.
    """
    details = [_name_detail(new_name, "parent")]
    for child, target in _child_target_names(children, parent.name, new_name, module):
        details.append(_name_detail(child.name if target is None else target, "channel", child.channel_id))
    return details


def _channelized_family_entry(rule, module, parent, children, variables) -> dict | None:  # pragma: no cover
    """Preview a breakout rule against an already-channelized family.

    Nothing is created — only the existing channels are renamed, plus the parent when the rule
    names one — so a family whose channel count disagrees with the rule previews as no change at
    all.
    """
    if getattr(parent, "channels", None) != rule.channel_count:
        return None
    parent_name = parent.name
    if _is_channelized_rule(rule) and rule.parent_name_template:
        try:
            parent_name = evaluate_name_template(rule.parent_name_template, {**variables, "base": parent.name})
        except ValueError as exc:
            parent_name = f"<error: {exc}>"
    details = [_name_detail(parent_name, "parent")]
    for child in children:
        channel = str(rule.channel_start + child.channel_id - 1)
        try:
            new_name = evaluate_name_template(
                rule.name_template, {**variables, "base": parent.name, "channel": channel}
            )
        except ValueError as exc:
            new_name = f"<error: {exc}>"
        details.append(_name_detail(new_name, "channel", child.channel_id))
    return _family_entry(module, parent, details, children)


def _channelized_family_preview(rule, module, base, variables) -> dict | None:  # pragma: no cover - see below
    """Describe the channelized family a rule would build on a plain base interface."""
    try:
        parent_name, channels = _channelized_family_names(rule, base.name, variables)
    except ValueError as exc:
        return _family_entry(module, base, [_name_detail(f"<error: {exc}>", "parent")], ())
    details = [_name_detail(parent_name, "parent")]
    details.extend(_name_detail(name, "channel", channel_id) for channel_id, name in channels)
    return _family_entry(module, base, details, ())


def _channelized_creation_entry(rule, module, bases, variables) -> dict | None:
    """Return the preview entry for the family a channelized rule would build, or None for none.

    A release that cannot model channels previews nothing, because the apply path builds nothing
    there either; neither does a module whose flat family the apply path refuses to convert.
    """
    if not supports_channelization():
        return None
    if _has_flat_expansion(module):  # pragma: no cover - requires channelization support
        return None
    return _channelized_family_preview(  # pragma: no cover - requires channelization support
        rule, module, _find_channel_base(rule, bases, variables), variables
    )


def _channel_rule_entries(rule, module, bases, children_by_parent, variables) -> list:
    """Return the preview entries a channel rule produces for one module.

    A module whose base is already channelized previews per family (renames only); a channelized
    rule on a plain base previews the family it would build; anything else keeps the flat breakout
    preview of one entry per module.
    """
    families = [base for base in bases if _is_channelized_parent(base)]
    if families:  # pragma: no cover - requires a NetBox that models channelization
        entries = [
            _channelized_family_entry(rule, module, parent, children_by_parent.get(parent.pk, ()), variables)
            for parent in families
        ]
        return [entry for entry in entries if entry]
    if _is_channelized_rule(rule):
        entry = _channelized_creation_entry(rule, module, bases, variables)
        return [entry] if entry else []
    entry = _channel_rule_entry(rule, module, bases, variables)
    return [entry] if entry else []


def _channel_rule_entry(rule, module, ifaces, variables) -> dict | None:
    """Return a result dict if the channel rule would change any name for this module, else None."""
    base_iface = _find_channel_base(rule, ifaces, variables)
    if base_iface is None:
        return None
    vars_copy = {**variables, "base": base_iface.name}
    expected_names = []
    try:
        for ch in range(rule.channel_count):
            expected_names.append(
                evaluate_name_template(rule.name_template, {**vars_copy, "channel": str(rule.channel_start + ch)})
            )
    except ValueError as exc:
        expected_names = [f"<error: {exc}>"]
    existing_names = {i.name for i in ifaces}
    # Report if any channel name is missing or the base itself needs renaming
    if any(n not in existing_names for n in expected_names) or (
        expected_names and expected_names[0] != base_iface.name
    ):
        return {
            "module": module,
            "interface": base_iface,
            "current_name": base_iface.name,
            "new_names": expected_names,
            "name_details": [_name_detail(name, "channel") for name in expected_names],
        }
    return None


def _count_remaining_interfaces(module_qs, processed_pks) -> int:
    """Count the rule candidates in modules not yet visited during a find_interfaces_for_rule scan."""
    from dcim.models import Interface

    qs = Interface.objects.filter(module__in=module_qs.exclude(pk__in=processed_pks))
    if supports_channelization():  # pragma: no cover - the column exists only on NetBox 4.7+
        qs = qs.filter(channel_id__isnull=True)  # a family counts once, through its parent
    return qs.count()


def _process_channel_module(rule, module, ifaces, variables, limit, results, module_qs, processed_pks):
    """Process one module for a channel rule.  Returns (checked_count, should_stop)."""
    bases, children_by_parent = _partition_families(ifaces)
    checked = len(bases)
    if not bases:
        return checked, False
    for entry in _channel_rule_entries(rule, module, bases, children_by_parent, variables):
        results.append(entry)
        if limit is not None and len(results) >= limit:
            return checked + _count_remaining_interfaces(module_qs, processed_pks), True
    return checked, False


def _process_plain_module(rule, module, ifaces, variables, limit, results, module_qs, processed_pks):
    """Process one module for a plain (non-channel) rule.  Returns (checked_count, should_stop)."""
    bases, children_by_parent = _partition_families(ifaces)
    checked = 0
    for iface_idx, iface in enumerate(bases):
        checked += 1
        entry = _evaluate_plain_interface(rule, module, iface, variables, children_by_parent.get(iface.pk, ()))
        if entry:
            results.append(entry)
            if limit is not None and len(results) >= limit:
                checked += len(bases) - (iface_idx + 1)
                checked += _count_remaining_interfaces(module_qs, processed_pks)
                return checked, True
    return checked, False


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
    process_fn = _process_channel_module if rule.channel_count > 0 else _process_plain_module

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
        checked, stop = process_fn(rule, module, ifaces, variables, limit, results, module_qs, processed_pks)
        total_checked += checked
        if stop:
            return results, total_checked

    return results, total_checked


def _apply_channel_rule_to_module(rule, module, ifaces, variables, id_set, conflicts):
    """Apply a channel rule to one module via its base interface; return the rename count.

    On a module whose base is already channelized the rule renames the existing channels, once per
    family.  Otherwise the rule is processed ONCE per module (not per interface) so existing
    channel names are not re-created.  An unexpected failure (e.g. a save race) is logged and
    skipped so it never aborts the surrounding batch.
    """
    bases, children_by_parent = _partition_families(ifaces)
    if not bases:
        return 0
    families = [base for base in bases if _is_channelized_parent(base)]
    if families:  # pragma: no cover - requires a NetBox that models channelization
        count = 0
        for parent in families:
            if id_set is not None and parent.pk not in id_set:
                continue
            count += _apply_family(rule, parent, children_by_parent.get(parent.pk, ()), variables, module, conflicts)
        return count
    base_iface = _find_channel_base(rule, bases, variables)
    if id_set is not None and base_iface.pk not in id_set:
        return 0
    vars_copy = dict(variables)
    vars_copy["base"] = base_iface.name
    try:
        if _is_channelized_rule(rule):
            return _apply_channelized_rule(rule, base_iface, variables, module, conflicts) or 0
        return _apply_rule_to_interface(rule, base_iface, vars_copy, module, conflicts=conflicts)
    except (ValueError, ValidationError, IntegrityError):
        logger.exception(
            "Failed to apply channel rule '%s' to module '%s' (id=%s); skipping.",
            rule,
            module,
            module.pk,
        )
        return 0


def _apply_family(rule, iface, children, variables, module, conflicts):
    """Apply *rule* to one base interface and its channels, logging (never raising) on failure."""
    try:
        return _apply_rule_with_family(rule, iface, children, variables, module, conflicts) or 0
    except (ValueError, ValidationError, IntegrityError):
        logger.exception(
            "Failed to apply rule '%s' to interface '%s' (id=%s); skipping.",
            rule,
            iface.name,
            iface.pk,
        )
        return 0


def _apply_plain_rule_to_module(rule, module, ifaces, variables, id_set, conflicts):
    """Apply a non-channel rule to each selected interface on one module; return the rename count.

    Each base interface is independent: an unexpected failure on one is logged and skipped so the
    rest of the module (and batch) still process.  Channel subinterfaces are not selectable on
    their own — they are renamed only as part of the family whose parent was selected.
    """
    bases, children_by_parent = _partition_families(ifaces)
    count = 0
    for iface in bases:
        if id_set is not None and iface.pk not in id_set:
            continue
        count += _apply_family(rule, iface, children_by_parent.get(iface.pk, ()), variables, module, conflicts)
    return count


def apply_rule_to_existing(rule, limit=None, interface_ids=None, conflicts=None):
    """Apply a rule retroactively to all matching installed modules.

    Unlike apply_interface_name_rules(), this does not skip already-renamed
    interfaces — it re-evaluates every interface on each matching module.

    For channel rules (channel_count > 0), each module is processed as a single
    unit using _find_channel_base() to pick the base interface.  Calling
    _apply_rule_to_interface for every interface in the module would produce
    duplicate-name IntegrityErrors when channel interfaces already exist.

    If *interface_ids* is provided (list/set of Interface PKs), only those
    interfaces are processed; all others are skipped.  For channel rules the
    base interface PK is used as the selector.  An empty *interface_ids*
    collection returns 0 immediately without touching the database.  Selecting a
    channelized parent brings its channel subinterfaces along; selecting a channel
    subinterface on its own does nothing, because it is not an independent candidate.

    If *conflicts* is a list, each interface skipped because its target name is
    already taken on the device is appended to it (and logged) — letting the
    caller report how many renames were dropped.  Collisions never raise.

    Returns the number of interfaces renamed/created.
    """
    from dcim.models import Interface

    id_set = frozenset(interface_ids) if interface_ids is not None else None
    if id_set is not None and not id_set:
        return 0

    if not rule.enabled:
        return 0

    module_qs = _build_module_qs(rule)

    # Batch-load interfaces to avoid N+1 queries in the module loop.
    ifaces_by_module = defaultdict(list)
    for iface in Interface.objects.filter(module__in=module_qs).order_by("module_id", "name"):
        ifaces_by_module[iface.module_id].append(iface)

    count = 0
    for module in module_qs.select_related("module_bay", "module_type", "device", "device__virtual_chassis"):
        variables = build_variables(module.module_bay, device=module.device)
        ifaces = ifaces_by_module.get(module.pk, [])

        if rule.channel_count > 0:
            count += _apply_channel_rule_to_module(rule, module, ifaces, variables, id_set, conflicts)
        else:
            count += _apply_plain_rule_to_module(rule, module, ifaces, variables, id_set, conflicts)

        if limit is not None and count >= limit:
            return count

    return count


# ---------------------------------------------------------------------------
# Assisted flat → channelized conversion
# ---------------------------------------------------------------------------
# An earlier flat apply leaves N sibling interfaces where NetBox 4.7+ models a channelized parent
# with N channel subinterfaces.  Converting one rewrites rows an operator owns — cables, addresses,
# tags — so it is never a side effect of applying a rule: the operator confirms it per family.

# The ch-0 row, the names its family carries now, and the names it would carry once converted.
_ConversionFamily = namedtuple("_ConversionFamily", ("module", "base", "current_names", "parent_name", "channel_names"))


def _conversion_offered(rule):
    """Return True when *rule* describes a topology an installed flat family could be converted into.

    A disabled rule renames nothing on any apply path, so it converts nothing either.  A flat family
    has no parent row — its ch-0 interface *is* the base — so without a parent name there is nowhere
    for that base to go, and the conversion is not offered at all.
    """
    return rule.enabled and _is_channelized_rule(rule) and rule.channel_count > 0 and bool(rule.parent_name_template)


def _base_marked_ch0_name(rule, variables):  # pragma: no cover - requires channelization support
    """Return *rule*'s escaped ch-0 output with ``{base}`` left as a sentinel, or None when it cannot be.

    Evaluated once per rule: the sentinel stands in for ``{base}`` so a raw matcher can be spliced
    over it afterwards.
    """
    try:
        evaluated = evaluate_name_template(
            rule.name_template, {**variables, "base": _BASE_SENTINEL, "channel": str(rule.channel_start)}
        )
    except (ValueError, TypeError):
        # A {base} inside an arithmetic expression cannot take a non-numeric stand-in — see the docs.
        logger.debug(
            "Rule '%s' evaluates {base} arithmetically, so a base predating a virtual-chassis "
            "position change cannot be recovered from its output names; not offering its families.",
            rule,
        )
        return None
    if _BASE_SENTINEL not in evaluated:
        return None  # the rule's output does not carry the base, so no drift reached it
    return re.escape(evaluated)


def _recovered_bases(rule, interfaces, variables, matchers):  # pragma: no cover - channelization only
    """Return the historical ``{base}`` values *rule*'s installed families still spell on this module.

    A flat family carries rule-*output* names, so a raw matcher cannot be run against them directly:
    it is spliced into the rule's own ch-0 output as a capture instead — a repeated ``{base}`` becomes
    a backreference rather than a second group — and the capture yields the base the family was named
    with.  Conversion rewrites rows an operator owns, so anything ambiguous (one matcher over two
    bases, or two templates recovering the same one) yields nothing at all.
    """
    marked = _base_marked_ch0_name(rule, variables)
    if marked is None:
        return []
    head, _, tail = marked.partition(_BASE_SENTINEL)
    tail = tail.replace(_BASE_SENTINEL, "(?P=base)")
    recovered = defaultdict(int)
    for matcher in matchers:
        family_pattern = _compile_pattern(f"{head}(?P<base>{matcher.pattern.pattern}){tail}")
        if family_pattern is None:
            continue
        matches = (family_pattern.fullmatch(iface.name) for iface in interfaces)
        bases = {match.group("base") for match in matches if match}
        if len(bases) == 1:
            recovered[bases.pop()] += 1
    return [base for base, claims in recovered.items() if claims == 1]


def _family_on(rule, module, by_name, variables, base_name):  # pragma: no cover - channelization only
    """Return the family *rule* describes on *base_name*, or None when this module carries none."""
    family_vars = {**variables, "base": base_name}
    parent_name = evaluate_name_template(rule.parent_name_template, family_vars)
    channel_names = [
        evaluate_name_template(rule.name_template, {**family_vars, "channel": str(rule.channel_start + offset)})
        for offset in range(rule.channel_count)
    ]
    base = by_name.get(channel_names[0])
    if base is None or _is_channel_child(base) or _is_channelized_parent(base):
        return None
    return _ConversionFamily(
        module=module,
        base=base,
        current_names=[name for name in channel_names if name in by_name],
        parent_name=parent_name,
        channel_names=channel_names,
    )


def _conversion_family(rule, module, interfaces, variables, raw):  # pragma: no cover - channelization only
    """Return the flat family *rule* would convert on *module*, or None when it carries none.

    Identification is by name: ``name_template`` is evaluated over the rule's channel range against
    each raw template name, and the ch-0 name has to still be a plain interface — a family that was
    already converted (its ch-0 name now belongs to a channel row) is therefore never offered twice.
    A family named before this device's virtual-chassis position changed spells a base no template
    resolves to any more, so those bases are recovered from the family's own names and then
    identified exactly the same way.
    """
    by_name = {iface.name: iface for iface in interfaces}
    for base_name in sorted(raw.names):
        family = _family_on(rule, module, by_name, variables, base_name)
        if family is not None:
            return family
    for base_name in _recovered_bases(rule, interfaces, variables, raw.matchers):
        family = _family_on(rule, module, by_name, variables, base_name)
        if family is not None:
            return family
    return None


def _conversion_families(rule):  # pragma: no cover - requires channelization support
    """Yield the flat family each module in *rule*'s scope still carries.

    A flat breakout is applied once per module (see ``_apply_channel_rule_to_module``), so a module
    carries at most one such family and the conversion mirrors that.
    """
    from dcim.models import Interface

    modules = list(_build_module_qs(rule).select_related("module_type", "device", *_BAY_CHAIN_RELATIONS))
    raw_by_module = _raw_names_by_module(modules)
    ifaces_by_module = defaultdict(list)
    for iface in Interface.objects.filter(module__in=[module.pk for module in modules]).order_by("module_id", "name"):
        ifaces_by_module[iface.module_id].append(iface)
    for module in modules:
        variables = build_variables(module.module_bay, device=module.device)
        ifaces = ifaces_by_module.get(module.pk, [])
        family = _conversion_family(rule, module, ifaces, variables, raw_by_module[module.pk])
        if family is not None:
            yield family


def _validate_or_block(iface, role):  # pragma: no cover - requires channelization support
    """Run NetBox's own validation on *iface*, restating a rejection as this family's blocking reason."""
    try:
        iface.full_clean()
    except ValidationError as exc:
        raise ValidationError(f"{role} {iface.name!r}: {' '.join(exc.messages)}") from exc


def _split_ch0_row(rule, family, base):  # pragma: no cover - requires channelization support
    """Make *base* the family's parent and move its logical identity onto a new channel-1 child.

    Everything an operator configured on the ch-0 row described a channel, not the cage carrying it,
    so addresses, VLANs, MTU, description and tags move; custom fields can mean either thing and are
    copied.  The physical row keeps its pk, cable, type, module link and mark_connected.
    """
    from dcim.choices import InterfaceTypeChoices
    from dcim.models import Interface

    carried = {
        "description": base.description,
        "mtu": base.mtu,
        "mode": base.mode,
        "untagged_vlan_id": base.untagged_vlan_id,
    }
    tagged_vlans = list(base.tagged_vlans.all())
    tags = list(base.tags.all())

    base.name = family.parent_name
    base.channels = rule.channel_count
    base.description = ""
    base.mtu = None
    base.mode = ""
    base.untagged_vlan = None
    _validate_or_block(base, "parent")
    base.save()  # BaseInterface.save() drops the tagged VLANs of an interface that no longer tags
    base.tags.clear()

    channel = Interface(
        device=family.module.device,
        module=family.module,
        name=family.channel_names[0],
        type=InterfaceTypeChoices.TYPE_CHANNEL,
        parent=base,
        channel_id=1,
        enabled=base.enabled,
        custom_field_data=dict(base.custom_field_data or {}),
        **carried,
    )
    _validate_or_block(channel, "channel")
    channel.save()
    channel.tagged_vlans.set(tagged_vlans)
    channel.tags.set(tags)
    base.ip_addresses.all().update(assigned_object_id=channel.pk)
    base.fhrp_group_assignments.all().update(interface_id=channel.pk)


def _rewrite_family(rule, family):  # pragma: no cover - requires channelization support
    """Convert *family* in place, raising ValidationError with the reason when it cannot be converted.

    Only what upstream cannot decide for us is checked here: the parent's name has to be free, every
    sibling has to be present, a sibling already bound to another parent's channel is not ours to
    take, and a cabled sibling cannot become a channel — TYPE_CHANNEL is nonconnectable but not
    virtual, so ``Interface.clean()`` accepts a cable on one.  Everything else is left to
    ``full_clean()`` on each prospective row, which inherits upstream's rules as they grow.
    """
    from dcim.choices import InterfaceTypeChoices
    from dcim.models import Interface

    device = family.module.device
    # Locked for the transaction: the checks below act on this snapshot, and the saves write it back.
    by_name = {iface.name: iface for iface in Interface.objects.select_for_update().filter(module=family.module)}
    base = by_name.get(family.channel_names[0])
    if base is None or base.pk != family.base.pk:
        raise ValidationError(
            f"{family.channel_names[0]!r} is gone or replaced: the family changed since it was scanned"
        )

    if _name_exists_on_device(device, family.parent_name, exclude_pk=base.pk):
        raise ValidationError(f"the parent name {family.parent_name!r} is already taken on {device}")

    siblings = []
    for channel_id, name in enumerate(family.channel_names[1:], start=2):
        sibling = by_name.get(name)
        if sibling is None or sibling.pk == base.pk:
            raise ValidationError(f"{name!r} is missing: this module carries no complete flat family")
        # Rebinding it validates cleanly, so only this check keeps the other family whole.
        if _is_channel_child(sibling):
            owner = sibling.parent.name if sibling.parent_id else "another parent"
            raise ValidationError(
                f"{name!r} is already channel {sibling.channel_id} of {owner}; "
                f"converting would take it out of that family"
            )
        if sibling.cable_id:
            raise ValidationError(f"{name!r} has a cable attached; a channel takes its cable from the parent")
        siblings.append((channel_id, sibling))

    _split_ch0_row(rule, family, base)
    for channel_id, sibling in siblings:
        sibling.type = InterfaceTypeChoices.TYPE_CHANNEL
        sibling.parent = base
        sibling.channel_id = channel_id
        _validate_or_block(sibling, "channel")
        sibling.save()


def _convert_family(rule, family, commit):  # pragma: no cover - requires channelization support
    """Convert *family*; return an empty string on success, or the reason it was refused.

    The whole conversion runs inside one savepoint, so a dry run (*commit* False) and a family that
    turns out to be unconvertible both leave every row exactly as it was — the rows are re-read here
    too, so a rolled-back dry run cannot hand mutated objects back to the caller.
    """
    try:
        with transaction.atomic():
            _rewrite_family(rule, family)
            if not commit:
                transaction.set_rollback(True)
    except (ValidationError, IntegrityError, ValueError) as exc:
        return "; ".join(getattr(exc, "messages", [str(exc)]))
    return ""


def _conversion_metadata_note(family):  # pragma: no cover - requires channelization support
    """Return the sentence the Apply page shows about where the ch-0 row's configuration ends up."""
    return (
        f"The addresses, VLANs, MTU, description and tags on {family.base.name} move to the new "
        f"channel 1 interface that takes over that name; custom field values are copied. The physical "
        f"row keeps its ID and becomes the parent {family.parent_name}, so automation keyed on that "
        f"interface ID will address the parent afterwards."
    )


def _conversion_verdict(family, reason):  # pragma: no cover - requires channelization support
    """Describe what converting *family* would do, and why it cannot be done when it cannot."""
    details = [_name_detail(family.parent_name, "parent")]
    details.extend(
        _name_detail(name, "channel", channel_id) for channel_id, name in enumerate(family.channel_names, start=1)
    )
    return {
        "module": family.module,
        "interface": family.base,
        "current_name": family.base.name,
        "current_names": family.current_names,
        "new_names": [family.parent_name, *family.channel_names],
        "name_details": details,
        "convertible": not reason,
        "reason": reason,
        "metadata_note": _conversion_metadata_note(family),
    }


def find_convertible_families(rule, limit=None) -> tuple:
    """Return ``(verdicts, has_more)`` for the flat families *rule* could convert, convertible or not.

    Nothing is written: every family is converted inside a savepoint that is rolled back again, so
    each verdict carries the reason NetBox itself would refuse the family rather than a guess at its
    rules.  Each verdict names the ch-0 row the confirm form submits, the family's current names,
    the names it would carry, and where the ch-0 row's configuration lands.

    That dry run is what the scan costs, and a blocked family costs it too, so *limit* caps the
    families examined — one verdict each — rather than the convertible ones among them.  A family
    beyond the limit is never dry-run; *has_more* reports that one was left unexamined.
    """
    if not (_conversion_offered(rule) and supports_channelization()):
        return [], False
    return _find_convertible_families(rule, limit)  # pragma: no cover - requires channelization support


def _find_convertible_families(rule, limit):  # pragma: no cover - requires channelization support
    """Dry-run at most *limit* of *rule*'s flat families; see ``find_convertible_families``."""
    verdicts = []
    for family in _conversion_families(rule):
        if limit is not None and len(verdicts) >= limit:
            return verdicts, True
        verdicts.append(_conversion_verdict(family, _convert_family(rule, family, commit=False)))
    return verdicts, False


def convert_flat_families(rule, base_pks=None, conflicts=None) -> int:
    """Convert *rule*'s installed flat families to the channelized topology; return how many.

    *base_pks* is the set of ch-0 interface pks the operator confirmed: ``None`` converts every
    convertible family (the batch the background job runs), an empty collection converts none.  A
    family that cannot be converted is logged, appended to *conflicts* in the usual skipped-rename
    shape and passed over — it is never half converted, and never costs the rest of the batch.
    """
    if not supports_channelization():
        logger.warning(
            "Rule '%s' converts flat families into the channelized topology, which this NetBox release "
            "cannot model; nothing was converted.",
            rule,
        )
        return 0
    return _convert_flat_families(rule, base_pks, conflicts)  # pragma: no cover - see above


def _convert_flat_families(rule, base_pks, conflicts):  # pragma: no cover - requires channelization support
    """Convert the confirmed flat families of *rule*; see ``convert_flat_families``."""
    if not _conversion_offered(rule):
        return 0
    selected = None if base_pks is None else frozenset(base_pks)
    if selected is not None and not selected:
        return 0

    converted = 0
    for family in _conversion_families(rule):
        if selected is not None and family.base.pk not in selected:
            continue
        current_name = family.base.name
        reason = _convert_family(rule, family, commit=True)
        if reason:
            logger.warning(
                "Cannot convert the flat family of interface %r on %s into the channelized parent %r: %s. Skipping.",
                current_name,
                family.module,
                family.parent_name,
                reason,
            )
            _record_skip(conflicts, family.module.device, current_name, family.parent_name, family.base.pk)
            continue
        converted += 1
    return converted
