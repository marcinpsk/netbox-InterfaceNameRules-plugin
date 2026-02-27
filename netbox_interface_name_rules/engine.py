# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Core renaming engine — rule lookup and interface rename logic.

This module is imported lazily by signals.py so that model imports happen
after Django is fully initialised.
"""

import ast
import logging
import re

from django.db import transaction
from django.db.models.functions import Length

logger = logging.getLogger(__name__)


def apply_interface_name_rules(module, module_bay, force_reapply=False):
    """Apply InterfaceNameRule rename after module installation.

    Looks up a matching rule for (module_type, parent_module_type, device_type, platform)
    and renames interfaces created by NetBox's template instantiation.

    Only processes interfaces whose name still matches the raw bay position
    (i.e., haven't been renamed yet), ensuring idempotency.  Pass
    ``force_reapply=True`` to skip this check and re-apply rules to ALL
    module interfaces (used when vc_position or other variables change).

    Returns:
        Number of interfaces renamed/created, or 0 if no rule matched.

    """
    from dcim.models import Interface

    module_type = module.module_type

    # Determine parent module type (if installed inside another module)
    parent_module_type = None
    if module_bay.parent:
        parent_bay = module_bay.parent
        if hasattr(parent_bay, "installed_module") and parent_bay.installed_module:
            parent_module_type = parent_bay.installed_module.module_type

    # Look up rule: most specific first, then broader matches
    device_type = module.device.device_type if module.device else None
    platform = module.device.platform if module.device else None
    rule = find_matching_rule(module_type, parent_module_type, device_type, platform)

    if not rule:
        return 0

    variables = build_variables(module_bay, device=module.device)
    interfaces = list(Interface.objects.filter(module=module))

    if not interfaces:
        return 0

    # Determine the raw interface names NetBox assigned from templates.
    # Use NetBox's own resolve_name() to handle nested modules correctly
    # (e.g., {module} walks the module tree for converters).
    raw_names = _get_raw_interface_names(module)
    if not raw_names:
        # Fallback when module type has no InterfaceTemplates
        raw_names = {variables["bay_position"]}

    if force_reapply and rule.channel_count == 0:
        # Re-apply to all interfaces regardless of current name (e.g. vc_position changed).
        unrenamed = interfaces
    elif force_reapply and rule.channel_count > 0:
        # For breakout rules, re-apply only to channel sub-interfaces whose base name
        # (before ":") matches a raw template name so vc_position changes propagate
        # without re-creating already-correct channel entries.
        unrenamed = [i for i in interfaces if i.name.rsplit(":", 1)[0] in raw_names]
    else:
        unrenamed = [i for i in interfaces if i.name in raw_names]
    if not unrenamed:
        return 0  # Already renamed (idempotent guard)

    renamed = 0
    for iface in unrenamed:
        vars_copy = dict(variables)
        vars_copy["base"] = iface.name
        renamed += _apply_rule_to_interface(rule, iface, vars_copy, module)

    if unrenamed and renamed == 0:
        # All interfaces already have the names the rule would produce.
        # This can happen when a newer NetBox version generates correct names
        # natively (e.g. via improved template resolution), making this rule
        # potentially obsolete — at least for this module type/context.
        _flag_rule_potentially_deprecated(rule)

    return renamed


def apply_device_interface_rules(device):
    """Rename device-level interfaces (module=None) when a device joins/changes position in a VC.

    Finds all enabled rules with ``applies_to_device_interfaces=True`` that match the device's
    type and platform, then renames any matching interfaces using the name_template.

    Template variables available: ``{vc_position}``, ``{base}`` (full current name),
    ``{port}`` (segment after the last ``/``, or the full name if no ``/`` present).

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

    rules = (
        InterfaceNameRule.objects.filter(
            applies_to_device_interfaces=True,
            enabled=True,
        )
        .filter(Q(device_type=device_type) | Q(device_type__isnull=True))
        .filter(Q(platform=platform) | Q(platform__isnull=True))
        .order_by("-priority", "pk")
    )

    if not rules.exists():
        return 0

    interfaces = list(Interface.objects.filter(device=device, module=None))
    if not interfaces:
        return 0

    total = 0
    renamed_pks: set[int] = set()
    for rule in rules:
        for iface in interfaces:
            if iface.pk in renamed_pks:
                continue  # Already renamed by a higher-priority rule
            # Filter by interface name pattern if specified
            if rule.module_type_pattern:
                try:
                    if not re.fullmatch(rule.module_type_pattern, iface.name):
                        continue
                except re.error:
                    continue

            port = iface.name.rsplit("/", 1)[-1] if "/" in iface.name else iface.name
            variables = {"vc_position": vc_position, "base": iface.name, "port": port}

            try:
                new_name = evaluate_name_template(rule.name_template, variables)
            except Exception:
                logger.exception(
                    "Failed to evaluate template %r for interface %s (rule %s)",
                    rule.name_template,
                    iface.name,
                    rule.pk,
                )
                continue

            if new_name == iface.name:
                continue

            old_name = iface.name
            iface.name = new_name
            try:
                iface.full_clean()
            except Exception:
                logger.exception(
                    "Validation failed for device interface %s → %s (rule %s, device %s)",
                    old_name,
                    new_name,
                    rule.pk,
                    device.pk,
                )
                iface.name = old_name  # revert
                continue
            try:
                iface.save()
            except Exception:
                logger.exception(
                    "DB save failed for device interface %s → %s (rule %s, device %s)",
                    old_name,
                    new_name,
                    rule.pk,
                    device.pk,
                )
                iface.name = old_name  # revert
                continue
            renamed_pks.add(iface.pk)
            logger.debug(
                "Renamed device interface %s → %s (rule %s, device %s)", old_name, new_name, rule.pk, device.pk
            )
            total += 1

    return total


def _get_raw_interface_names(module):
    """Return the original interface names NetBox assigned from templates.

    Prefetches module relationships to avoid per-template queries when
    InterfaceTemplate.resolve_name() dereferences the module bay chain.
    """
    from dcim.models import InterfaceTemplate, Module

    module_fresh = Module.objects.select_related(
        "module_bay",
        "module_bay__parent",
        "module_bay__module",
        "module_bay__module__module_bay",
        "module_bay__module__module_bay__parent",
        "module_bay__module__module_bay__module",
    ).get(pk=module.pk)
    templates = InterfaceTemplate.objects.filter(module_type=module_fresh.module_type)
    return {tmpl.resolve_name(module_fresh) for tmpl in templates}


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


def find_matching_rule(module_type, parent_module_type, device_type, platform=None):
    """Find the most specific InterfaceNameRule matching the context.

    Uses a two-tier strategy:
      Tier 1 — Exact FK match (priority order, most specific first):
        Iterates all combinations of (parent_module_type, device_type, platform)
        from fully-constrained to fully-unconstrained (None = any).
      Tier 2 — Regex pattern match (same priority order, longer patterns first):
        Same specificity cascade, but module_type_pattern is matched via
        re.fullmatch() against module_type.model. Patterns are iterated
        from longest to shortest to prefer more specific patterns.

    Returns the first matching rule, or None if no rule matches.
    """
    from .models import InterfaceNameRule

    # Build candidate (parent_module_type, device_type, platform) tuples ordered
    # most-specific to least-specific, deduplicating when inputs are None.
    seen: set = set()
    candidates = []
    for pmt in [parent_module_type, None] if parent_module_type else [None]:
        for dt in [device_type, None] if device_type else [None]:
            for pl in [platform, None] if platform else [None]:
                key = (pmt, dt, pl)
                if key not in seen:
                    seen.add(key)
                    candidates.append(key)

    # Tier 1: exact FK match
    for pmt, dt, pl in candidates:
        filters: dict = {"module_type": module_type, "module_type_is_regex": False, "enabled": True}
        if pmt is None:
            filters["parent_module_type__isnull"] = True
        else:
            filters["parent_module_type"] = pmt
        if dt is None:
            filters["device_type__isnull"] = True
        else:
            filters["device_type"] = dt
        if pl is None:
            filters["platform__isnull"] = True
        else:
            filters["platform"] = pl
        rule = InterfaceNameRule.objects.filter(**filters).first()
        if rule:
            return rule

    # Tier 2: regex pattern match — longer patterns are tried first (more specific)
    model_name = module_type.model
    for pmt, dt, pl in candidates:
        filters = {"module_type_is_regex": True, "enabled": True}
        if pmt is None:
            filters["parent_module_type__isnull"] = True
        else:
            filters["parent_module_type"] = pmt
        if dt is None:
            filters["device_type__isnull"] = True
        else:
            filters["device_type"] = dt
        if pl is None:
            filters["platform__isnull"] = True
        else:
            filters["platform"] = pl
        qs = (
            InterfaceNameRule.objects.filter(**filters)
            .annotate(pattern_length=Length("module_type_pattern"))
            .order_by("-pattern_length", "pk")
        )
        for rule in qs:
            try:
                if re.fullmatch(rule.module_type_pattern, model_name):
                    return rule
            except re.error:
                continue

    return None


def build_variables(module_bay, device=None):
    """Build template variable dict from a module bay's position context.

    Extracts numeric and raw position values from the bay and its parent chain,
    producing the variables available for name_template substitution.

    Returns a dict with keys: slot, bay_position, bay_position_num,
    parent_bay_position, sfp_slot, and optionally vc_position.

    ``vc_position`` is only injected when *device* is a Virtual Chassis member
    (device.virtual_chassis_id is set).  Templates using ``{vc_position}`` on a
    non-VC device will raise ValueError during evaluation — this is intentional.
    Note: Juniper VC positions start at 0, so 0 is a valid real-world value and
    cannot be used as a "not in VC" sentinel.
    """
    bay_position = module_bay.position or "0"
    # If position is a template expression (e.g., {module}), extract from bay name
    if bay_position.startswith("{"):
        match = re.search(r"(\d+)$", module_bay.name)
        bay_position = match.group(1) if match else "0"
    # Extract numeric-only version for arithmetic (e.g., "swp1" → "1")
    bay_position_num_match = re.search(r"(\d+)$", bay_position)
    bay_position_num = bay_position_num_match.group(1) if bay_position_num_match else bay_position

    parent_bay_position = "0"
    sfp_slot = bay_position_num
    slot = bay_position_num

    if module_bay.parent:
        parent_bay = module_bay.parent
        parent_bay_position = parent_bay.position or "0"
        # slot is typically the top-level module position
        if parent_bay.parent and hasattr(parent_bay.parent, "installed_module"):
            grandparent = parent_bay.parent
            slot = grandparent.position or parent_bay_position
        else:
            slot = parent_bay_position
    elif hasattr(module_bay, "module") and module_bay.module:
        # Bay belongs to an installed module (not nested, but module-scoped)
        owner_module = module_bay.module
        if hasattr(owner_module, "module_bay") and owner_module.module_bay:
            slot = owner_module.module_bay.position or bay_position

    result = {
        "slot": slot,
        "bay_position": bay_position,
        "bay_position_num": bay_position_num,
        "parent_bay_position": parent_bay_position,
        "sfp_slot": sfp_slot,
    }
    if (
        device is not None
        and getattr(device, "virtual_chassis_id", None) is not None
        and device.vc_position is not None
    ):
        result["vc_position"] = str(device.vc_position)
    return result


def _apply_rule_to_interface(rule, iface, variables, module):
    """Apply a single rule to an interface, handling breakout channels.

    All saves are wrapped in a transaction so a failure mid-breakout rolls
    back any partially created interfaces.

    Returns the number of interfaces renamed/created.
    """
    from dcim.models import Interface

    count = 0

    with transaction.atomic():
        if rule.channel_count > 0:
            # Breakout: rename base interface and create additional channel interfaces
            for ch in range(rule.channel_count):
                variables["channel"] = str(rule.channel_start + ch)
                new_name = evaluate_name_template(rule.name_template, variables)
                if ch == 0:
                    if new_name != iface.name:
                        iface.name = new_name
                        iface.full_clean()
                        iface.save()
                        count += 1
                else:
                    if not Interface.objects.filter(module=module, name=new_name).exists():
                        breakout_iface = Interface(
                            device=module.device,
                            module=module,
                            name=new_name,
                            type=iface.type,
                            enabled=iface.enabled,
                        )
                        breakout_iface.full_clean()
                        breakout_iface.save()
                        count += 1
        else:
            # Simple rename (converter offset, platform naming, etc.)
            new_name = evaluate_name_template(rule.name_template, variables)
            if new_name != iface.name:
                iface.name = new_name
                iface.full_clean()
                iface.save()
                count += 1

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
      - all matching interfaces are already correctly named (e.g. NetBox resolved
        {module_path} at install time, making the rule a no-op for existing interfaces).

    This is more expensive than a plain EXISTS query but ensures the Applicable
    column in the Apply Rules list accurately reflects "would something change?"
    rather than the misleading "do interfaces exist?".
    """
    try:
        results, _ = find_interfaces_for_rule(rule, limit=1)
        return len(results) > 0
    except Exception:
        return False


def find_interfaces_for_rule(rule, limit=None):
    """Find interfaces that would be renamed by applying the given rule retroactively.

    Searches for all Module instances matching the rule's criteria and computes
    what their interfaces would be renamed to.

    Returns a tuple ``(results, total_checked)`` where *results* is a list of dicts::

        {
            "module":       Module instance,
            "interface":    Interface instance,
            "current_name": str,
            "new_names":    list[str],   # one entry per channel, or single-element
        }

    Only includes entries where at least one new_name differs from current_name.
    If *limit* is set the list is truncated after that many changed entries, but
    *total_checked* always reflects the full count of interfaces examined.
    """
    from dcim.models import Interface, Module

    if rule.module_type_is_regex:
        module_qs = Module.objects.filter(module_type__in=_matching_moduletype_pks(rule.module_type_pattern))
    else:
        module_qs = Module.objects.filter(module_type=rule.module_type)

    if rule.parent_module_type:
        module_qs = module_qs.filter(module_bay__parent__installed_module__module_type=rule.parent_module_type)
    if rule.device_type:
        module_qs = module_qs.filter(device__device_type=rule.device_type)
    if rule.platform:
        module_qs = module_qs.filter(device__platform=rule.platform)

    module_qs = module_qs.select_related(
        "module_type",
        "device",
        "device__device_type",
        "device__platform",
        "device__virtual_chassis",
        "module_bay",
        "module_bay__parent",
    )

    processed_pks = []
    results = []
    total_checked = 0
    for module in module_qs:
        processed_pks.append(module.pk)
        variables = build_variables(module.module_bay, device=module.device)
        ifaces_in_module = list(Interface.objects.filter(module=module).order_by("name"))

        if rule.channel_count > 0:
            # Channel rules: count interfaces as a unit (processed all-at-once below).
            total_checked += len(ifaces_in_module)
            if not ifaces_in_module:
                continue
            base_iface = _find_channel_base(rule, ifaces_in_module, variables)
            vars_copy = dict(variables)
            vars_copy["base"] = base_iface.name
            expected_names = []
            try:
                for ch in range(rule.channel_count):
                    vars_ch = dict(vars_copy)
                    vars_ch["channel"] = str(rule.channel_start + ch)
                    expected_names.append(evaluate_name_template(rule.name_template, vars_ch))
            except ValueError as exc:
                expected_names = [f"<error: {exc}>"]

            existing_names = {i.name for i in ifaces_in_module}
            # Report if any expected channel is missing, or the base needs renaming
            if any(n not in existing_names for n in expected_names) or (
                expected_names and expected_names[0] != base_iface.name
            ):
                results.append(
                    {
                        "module": module,
                        "interface": base_iface,
                        "current_name": base_iface.name,
                        "new_names": expected_names,
                    }
                )
                if limit is not None and len(results) >= limit:
                    total_checked += Interface.objects.filter(
                        module__in=module_qs.exclude(pk__in=processed_pks)
                    ).count()
                    return results, total_checked
        else:
            for iface_idx, iface in enumerate(ifaces_in_module):
                total_checked += 1  # count each interface as we iterate it
                vars_copy = dict(variables)
                vars_copy["base"] = iface.name
                try:
                    new_names = [evaluate_name_template(rule.name_template, vars_copy)]
                except ValueError as exc:
                    new_names = [f"<error: {exc}>"]

                if new_names[0] != iface.name:
                    results.append(
                        {
                            "module": module,
                            "interface": iface,
                            "current_name": iface.name,
                            "new_names": new_names,
                        }
                    )
                    if limit is not None and len(results) >= limit:
                        # Count remaining interfaces in the current module (not yet iterated)
                        total_checked += len(ifaces_in_module) - (iface_idx + 1)
                        # Count interfaces in modules not yet started
                        total_checked += Interface.objects.filter(
                            module__in=module_qs.exclude(pk__in=processed_pks)
                        ).count()
                        return results, total_checked

    return results, total_checked


def apply_rule_to_existing(rule, limit=None, interface_ids=None):
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
    collection returns 0 immediately without touching the database.

    Returns the number of interfaces renamed/created.
    """
    from dcim.models import Interface, Module

    id_set = frozenset(interface_ids) if interface_ids is not None else None
    if id_set is not None and not id_set:
        return 0

    if rule.module_type_is_regex:
        module_qs = Module.objects.filter(module_type__in=_matching_moduletype_pks(rule.module_type_pattern))
    else:
        module_qs = Module.objects.filter(module_type=rule.module_type)

    if rule.parent_module_type:
        module_qs = module_qs.filter(module_bay__parent__installed_module__module_type=rule.parent_module_type)
    if rule.device_type:
        module_qs = module_qs.filter(device__device_type=rule.device_type)
    if rule.platform:
        module_qs = module_qs.filter(device__platform=rule.platform)

    count = 0
    for module in module_qs.select_related("module_bay", "module_type", "device", "device__virtual_chassis"):
        variables = build_variables(module.module_bay, device=module.device)

        if rule.channel_count > 0:
            # Channel rule: process module ONCE using the best base interface.
            # Calling _apply_rule_to_interface for each existing interface would
            # attempt to create the same channel names multiple times.
            ifaces = list(Interface.objects.filter(module=module).order_by("name"))
            if not ifaces:
                continue
            base_iface = _find_channel_base(rule, ifaces, variables)
            if id_set is not None and base_iface.pk not in id_set:
                continue
            vars_copy = dict(variables)
            vars_copy["base"] = base_iface.name
            try:
                count += _apply_rule_to_interface(rule, base_iface, vars_copy, module)
            except Exception:
                logger.exception(
                    "Failed to apply channel rule '%s' to module '%s' (id=%s); skipping.",
                    rule,
                    module,
                    module.pk,
                )
        else:
            for iface in list(Interface.objects.filter(module=module).order_by("name")):
                if id_set is not None and iface.pk not in id_set:
                    continue
                vars_copy = dict(variables)
                vars_copy["base"] = iface.name
                try:
                    count += _apply_rule_to_interface(rule, iface, vars_copy, module)
                except Exception:
                    logger.exception(
                        "Failed to apply rule '%s' to interface '%s' (id=%s); skipping.",
                        rule,
                        iface.name,
                        iface.pk,
                    )
                    continue

        if limit is not None and count >= limit:
            return count

    return count


def evaluate_name_template(template: str, variables: dict) -> str:
    """Evaluate a name template with variable substitution and safe arithmetic.

    Supports templates like:
        "GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}"

    Variables are substituted first, then any brace-enclosed expression
    containing arithmetic operators is safely evaluated via AST. True division
    (/) is not allowed — use floor division (//) instead. Results are cast to
    int to ensure interface names are always whole numbers.
    """
    # First pass: substitute all simple variables
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))

    # Second pass: evaluate any remaining brace-enclosed arithmetic expressions
    def _eval_expr(match):
        expr = match.group(1).strip()
        # Allow digits, arithmetic operators (excluding lone /), parens, whitespace.
        # Negative lookahead disallows a single / that is not part of //.
        if not re.match(r"^(?!.*(?<!/)/(?!/))[\d\s\+\-\*\(\/\)]+$", expr):
            raise ValueError(f"Unsafe expression in name template: {expr}")
        try:
            node = ast.parse(expr, mode="eval")
            for child in ast.walk(node):
                if not isinstance(
                    child,
                    (
                        ast.Expression,
                        ast.BinOp,
                        ast.UnaryOp,
                        ast.Constant,
                        ast.Add,
                        ast.Sub,
                        ast.Mult,
                        ast.FloorDiv,
                        ast.USub,
                        ast.UAdd,
                    ),
                ):
                    raise ValueError(f"Unsafe AST node in expression: {type(child).__name__}")
            return str(int(eval(compile(node, "<template>", "eval"))))  # noqa: S307
        except (SyntaxError, TypeError) as e:
            raise ValueError(f"Invalid arithmetic expression '{expr}': {e}") from e

    result = re.sub(r"\{([^}]+)\}", _eval_expr, result)
    return result
