# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Core renaming engine — rule lookup and interface rename logic.

This module is imported lazily by signals.py so that model imports happen
after Django is fully initialised.
"""

import ast
import contextlib
import logging
import re
import threading

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Sum

logger = logging.getLogger(__name__)

# In-process cache of the enabled InterfaceNameRule set, keyed by a cheap fingerprint
# of that set. find_matching_rule() is called once per module row when a device's
# module-sync table is rendered, and each call used to run a DB query per scope
# candidate per tier; loading the (small) rule set once and matching in memory removes
# that per-row query storm. The fingerprint (see _enabled_rules_version) is re-read once
# per call and the set reloaded only when it changes, so it self-invalidates on any
# create/delete/edit — including edits that bypass auto_now (SET_NULL cascades, bulk
# .update() of a scope field) and across test transactions — with no signal wiring.
_RULE_CACHE = {"version": None, "exact": (), "regex": (), "memo": {}}

# Per-version cap on the find_matching_rule memo. A long-lived worker that sees many distinct
# (module_type, scope) contexts under a stable rule set would otherwise grow it without bound;
# on overflow the memo is cleared wholesale and rebuilt lazily. The cap is far above the number
# of contexts in any single module-sync render, so it never churns mid-render.
_MEMO_MAX = 4096

# Per-thread pin depth (see pinned_rule_cache). While > 0 on the current thread, _get_enabled_rules
# trusts the loaded set without re-reading the fingerprint, so an internal batch that wraps its loop
# turns N per-call fingerprint queries into one. Thread-local so concurrent requests don't affect
# each other; the depth defaults to 0, so any path that does not pin (the per-row signal handler,
# single applies) re-reads the fingerprint on every call exactly as before.
_pin = threading.local()


@contextlib.contextmanager
def pinned_rule_cache():
    """Pin the enabled-rule set for the duration of the block, skipping the per-call fingerprint query.

    find_matching_rule() normally re-reads a cheap fingerprint on every call to detect rule-set
    changes. An internal batch that makes many lookups against an unchanging rule set — e.g.
    _apply_rules_for_device_deferred(), which re-applies rules to every module on a device after a
    virtual-chassis change — can wrap its loop in this context manager so the set is loaded and
    fingerprinted once and reused for every call inside the block, turning N fingerprint queries
    into one::

        with pinned_rule_cache():
            for module in modules:
                apply_interface_name_rules(module, module.module_bay, force_reapply=True)

    This is an INR-internal helper: only code that owns the batch loop pins, so it never becomes a
    cross-plugin dependency. Other plugins integrate through the ``predict_module_interface_names``
    signal, which is dispatched one row at a time — so an external caller neither can nor needs to
    pin, and INR exposes no batch API across the plugin boundary.

    Scope is explicit and per-thread: outside the block (and in any other thread/request) the normal
    self-invalidating behaviour is unchanged, so there is no global staleness window. Nesting is
    safe — only the outermost block manages the pin. Priming is lazy: the first find_matching_rule
    inside the block does the one real fingerprint read + reload, and the rest reuse it — so a block
    that makes no lookups does no query at all. The loop may rename interfaces, but it must not edit
    rules: edits to InterfaceNameRule made *inside* the block are not observed until it exits.
    """
    depth = getattr(_pin, "depth", 0)
    _pin.depth = depth + 1
    if depth == 0:
        _pin.primed = False  # the first lookup inside primes the cache (lazily — an empty block stays query-free)
    try:
        yield
    finally:
        _pin.depth -= 1


def _compile_pattern(pattern):
    """Compile a regex *pattern* once, returning the compiled object or None for an invalid pattern.

    A None result is skipped at match time, mirroring the previous per-match ``try/except re.error``
    without recompiling the pattern on every lookup.
    """
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _enabled_rules_version():
    """Return a cheap fingerprint of the enabled-rule set.

    ``(count, latest last_updated)`` catches creates, deletes and any ``.save()`` edit. The
    FK-id and channel sums additionally catch changes that bypass ``auto_now`` while keeping the
    count constant — a ``SET_NULL`` cascade nulling a scope FK, or a bulk ``.update()`` of a
    scope/channel field — so the cache self-invalidates on those too. The one edit it cannot
    see is a raw bulk ``.update()`` of a *text* field (name_template / module_type_pattern),
    which bumps neither the count, the sums, nor last_updated; use ``.save()`` for such edits.
    """
    from .models import InterfaceNameRule

    agg = InterfaceNameRule.objects.filter(enabled=True).aggregate(
        n=Count("pk"),
        latest=Max("last_updated"),
        mt=Sum("module_type"),
        pmt=Sum("parent_module_type"),
        dt=Sum("device_type"),
        pl=Sum("platform"),
        chan=Sum("channel_count"),
        chs=Sum("channel_start"),
    )
    latest = agg["latest"]
    return (
        agg["n"] or 0,
        latest.isoformat() if latest else "",
        agg["mt"] or 0,
        agg["pmt"] or 0,
        agg["dt"] or 0,
        agg["pl"] or 0,
        agg["chan"] or 0,
        agg["chs"] or 0,
    )


def _get_enabled_rules():
    """Return ``(exact_rules, regex_rules, memo)``, reloading only when the rule set changes.

    ``exact_rules`` are ordered by ``(module_type__model, pk)`` to mirror the model's default
    ordering (so an in-memory ``first match`` equals the previous ``.first()``). ``regex_rules``
    is a tuple of ``(compiled_pattern, rule)`` pairs, pre-sorted once by ``(-pattern length, pk)``
    and with each pattern compiled once, so the regex tier neither re-sorts nor recompiles per
    call. ``memo`` caches find_matching_rule results for the current rule-set version.

    Inside a ``pinned_rule_cache()`` block on this thread, once the set has been primed (by the first
    lookup in the block) the fingerprint query is skipped and the already-loaded set is returned.
    """
    pinned = getattr(_pin, "depth", 0) > 0
    if pinned and getattr(_pin, "primed", False):
        return _RULE_CACHE["exact"], _RULE_CACHE["regex"], _RULE_CACHE["memo"]

    from .models import InterfaceNameRule

    version = _enabled_rules_version()
    if _RULE_CACHE["version"] != version:
        rules = list(InterfaceNameRule.objects.filter(enabled=True).order_by("module_type__model", "pk"))
        _RULE_CACHE["exact"] = tuple(r for r in rules if not r.module_type_is_regex)
        regex_rules = sorted(
            (r for r in rules if r.module_type_is_regex),
            key=lambda r: (-len(r.module_type_pattern or ""), r.pk),
        )
        _RULE_CACHE["regex"] = tuple((_compile_pattern(r.module_type_pattern), r) for r in regex_rules)
        _RULE_CACHE["memo"] = {}
        _RULE_CACHE["version"] = version
    if pinned:
        _pin.primed = True  # subsequent lookups in this block skip the fingerprint
    return _RULE_CACHE["exact"], _RULE_CACHE["regex"], _RULE_CACHE["memo"]


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


def _collect_unrenamed(interfaces, rule, raw_names, force_reapply):
    """Return the subset of *interfaces* that should be processed by the rule.

    Normal (non-force) mode: only interfaces whose current name is still in the
    raw template names (idempotency guard).

    force_reapply, non-channel: all interfaces (e.g. vc_position changed).

    force_reapply, channel rule: one interface per base name, matching when the
    full base OR its last path segment is in *raw_names*; prefers the ":0"
    channel to avoid duplicate-name errors on re-apply.
    """
    if not force_reapply:
        return [i for i in interfaces if i.name in raw_names]
    if rule.channel_count == 0:
        return interfaces
    # Breakout + force_reapply: deduplicate by base, prefer ":0"
    seen_bases: dict = {}
    for i in interfaces:
        base = i.name.rsplit(":", 1)[0]
        # Also match when the last path segment equals a raw name (already-renamed bases).
        if base in raw_names or base.rsplit("/", 1)[-1] in raw_names:
            if base not in seen_bases or i.name.endswith(":0"):
                seen_bases[base] = i
    return list(seen_bases.values())


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

    device_type = module.device.device_type if module.device else None
    platform = module.device.platform if module.device else None
    rule = find_matching_rule(module.module_type, _get_parent_module_type(module_bay), device_type, platform)

    if not rule:
        return 0

    variables = build_variables(module_bay, device=module.device)
    interfaces = list(Interface.objects.filter(module=module))

    if not interfaces:
        return 0

    # Determine raw names NetBox assigned from templates; fall back to bay_position.
    raw_names = _get_raw_interface_names(module) or {variables["bay_position"]}
    unrenamed = _collect_unrenamed(interfaces, rule, raw_names, force_reapply)

    if not unrenamed:
        return 0  # Already renamed (idempotent guard)

    renamed = 0
    conflicts: list = []
    for iface in unrenamed:
        vars_copy = dict(variables)
        vars_copy["base"] = iface.name
        try:
            renamed += _apply_rule_to_interface(rule, iface, vars_copy, module, conflicts=conflicts)
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

    if unrenamed and renamed == 0 and not conflicts:
        # All interfaces already have the names the rule would produce — flag as
        # potentially obsolete (e.g., newer NetBox generates correct names natively).
        # Skipped when the 0-count was caused by name collisions (a different reason
        # than a no-op rule), so a collision never mislabels the rule as deprecated.
        _flag_rule_potentially_deprecated(rule)

    return renamed


def predict_rule_output(module, module_bay, raw_names):
    """Predict the names apply_interface_name_rules would produce for raw_names.

    Pure function — does not save, mutate, or query the Interface table. Used
    by external integrations (e.g., netbox-librenms-plugin) that need to know
    the post-rename names without actually applying any rule.

    For breakout rules (channel_count > 0), each raw name expands to
    channel_count predicted names. For simple renames, one name in → one name
    out. Returns raw_names unchanged when no rule matches or evaluation fails.
    """
    device_type = module.device.device_type if module.device else None
    platform = module.device.platform if module.device else None
    rule = find_matching_rule(module.module_type, _get_parent_module_type(module_bay), device_type, platform)
    if not rule:
        return list(raw_names)

    variables = build_variables(module_bay, device=module.device)

    output = []
    for raw_name in raw_names:
        vars_copy = dict(variables)
        vars_copy["base"] = raw_name
        try:
            if rule.channel_count > 0:
                temp = []
                for ch in range(rule.channel_count):
                    vars_copy["channel"] = str(rule.channel_start + ch)
                    temp.append(evaluate_name_template(rule.name_template, vars_copy))
                output.extend(temp)
            else:
                output.append(evaluate_name_template(rule.name_template, vars_copy))
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

    total = 0
    renamed_pks: set[int] = set()
    for rule in rules:
        for iface in interfaces:
            if _try_rename_device_interface(rule, iface, vc_position, device, renamed_pks):
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


def _scope_ids(parent_module_type, device_type, platform):
    """Map the (parent_module_type, device_type, platform) scope objects to their FK ids.

    ``None`` (no constraint) maps to ``None`` so it compares equal to a rule's unset scope FK.
    Centralises the ``x.pk if x is not None else None`` coalescing used by both match tiers and
    the memo key.
    """
    return (
        parent_module_type.pk if parent_module_type is not None else None,
        device_type.pk if device_type is not None else None,
        platform.pk if platform is not None else None,
    )


def _rule_scope_matches(rule, scope_ids):
    """Return True when *rule*'s (parent_module_type, device_type, platform) FKs equal *scope_ids*."""
    pmt_id, dt_id, pl_id = scope_ids
    return rule.parent_module_type_id == pmt_id and rule.device_type_id == dt_id and rule.platform_id == pl_id


def _build_candidates(parent_module_type, device_type, platform) -> list:
    """Build ordered list of (pmt, dt, pl) tuples from most to least specific.

    Each argument expands to ``[value, None]`` when provided, or ``[None]``
    when already absent.  Deduplication ensures no key appears twice (which
    would happen when multiple inputs are None).
    """
    seen: set = set()
    candidates = []
    pmt_opts = [parent_module_type, None] if parent_module_type else [None]
    dt_opts = [device_type, None] if device_type else [None]
    pl_opts = [platform, None] if platform else [None]
    for pmt in pmt_opts:
        for dt in dt_opts:
            for pl in pl_opts:
                key = (pmt, dt, pl)
                if key not in seen:
                    seen.add(key)
                    candidates.append(key)
    return candidates


def _find_exact_match(module_type, candidates, exact_rules=None):
    """Tier 1: return the first enabled exact-FK rule in specificity order, or None.

    ``exact_rules`` is the preloaded, ``(module_type__model, pk)``-ordered enabled
    exact-rule set (the hot path passes it to avoid a DB query per call); when omitted
    it is loaded on demand so direct callers keep working.
    """
    if exact_rules is None:
        exact_rules, _, _ = _get_enabled_rules()

    # module_type fixed below → the (module_type__model, pk) ordering reduces to pk, so the
    # first matching rule equals the previous ``.filter(...).first()``.
    scoped = [r for r in exact_rules if r.module_type_id == module_type.pk]
    for candidate in candidates:
        scope_ids = _scope_ids(*candidate)
        for rule in scoped:
            if _rule_scope_matches(rule, scope_ids):
                return rule
    return None


def _find_regex_match(model_name: str, candidates, regex_rules=None):
    """Tier 2: return the first enabled regex rule whose pattern fullmatches *model_name*, or None.

    Tries candidates in specificity order; within each level longer patterns are tried first
    (more specific).  ``regex_rules`` is the preloaded ``(compiled_pattern, rule)`` set — pre-sorted
    by ``(-pattern length, pk)`` with each pattern compiled once (a None compile is an invalid
    pattern, silently skipped); loaded on demand when omitted.
    """
    if regex_rules is None:
        _, regex_rules, _ = _get_enabled_rules()

    for candidate in candidates:
        scope_ids = _scope_ids(*candidate)
        for compiled, rule in regex_rules:
            if compiled is not None and _rule_scope_matches(rule, scope_ids) and compiled.fullmatch(model_name):
                return rule
    return None


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

    The enabled rule set is loaded once and matched in memory, and the per-context
    result is memoized for the current rule-set version, so repeated calls (e.g. one
    per module row in a module-sync render) don't re-query the database.

    Returns the first matching rule, or None if no rule matches.
    """
    if module_type is None:
        # Module rules are always keyed on a module type; both tiers dereference it
        # (module_type.pk / .model), so there is nothing to match without one.
        return None

    exact_rules, regex_rules, memo = _get_enabled_rules()
    # The regex tier matches against module_type.model (a live string), so the memo must key on
    # it too — otherwise a ModuleType.model rename (same pk) would return a stale regex result.
    sig = (module_type.pk, module_type.model, *_scope_ids(parent_module_type, device_type, platform))
    if sig in memo:
        return memo[sig]

    candidates = _build_candidates(parent_module_type, device_type, platform)
    result = _find_exact_match(module_type, candidates, exact_rules) or _find_regex_match(
        module_type.model, candidates, regex_rules
    )
    if len(memo) >= _MEMO_MAX:
        memo.clear()  # bound per-version memory; entries are rebuilt lazily on the next miss
    memo[sig] = result
    return result


def _extract_trailing_digits(s: str) -> str:
    r"""Return the trailing digit run of *s* without regex backtracking.

    Pure O(n) string scan — eliminates the polynomial backtracking risk that
    arises from using ``re.search(r"(\d+)$", ...)`` on strings ending in a
    non-digit character (e.g. ``"1" * n + "x"`` would cause O(n²) steps).

    Returns an empty string when *s* has no trailing digits.
    """
    i = len(s)
    while i > 0 and s[i - 1].isdigit():
        i -= 1
    return s[i:]


def _resolve_bay_position(module_bay):
    """Return (bay_position, bay_position_num) from a module bay's position field.

    Handles template expressions like ``{module}`` by extracting the trailing
    digit from the bay name.  Falls back to ``"0"`` if no digit is found.
    """
    bay_position = module_bay.position or "0"
    if bay_position.startswith("{"):
        digits = _extract_trailing_digits(module_bay.name)
        bay_position = digits if digits else "0"
    digits = _extract_trailing_digits(bay_position)
    bay_position_num = digits if digits else "0"
    return bay_position, bay_position_num


def _resolve_slot(module_bay, bay_position_num, parent_bay_position):
    """Return the ``slot`` variable from the module bay hierarchy.

    When the bay has a parent bay, slot comes from the parent (or grandparent
    when two levels of nesting exist).  When the bay belongs to an installed
    module with its own bay, slot comes from that module's bay position.
    Falls back to ``bay_position_num``.
    """
    if module_bay.parent:
        parent_bay = module_bay.parent
        if parent_bay.parent and hasattr(parent_bay.parent, "installed_module"):
            return parent_bay.parent.position or parent_bay_position
        return parent_bay_position
    if hasattr(module_bay, "module") and module_bay.module:
        owner_module = module_bay.module
        if hasattr(owner_module, "module_bay") and owner_module.module_bay:
            return owner_module.module_bay.position or bay_position_num
    return bay_position_num


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
    bay_position, bay_position_num = _resolve_bay_position(module_bay)

    parent_bay_position = "0"
    if module_bay.parent:
        parent_bay_position = module_bay.parent.position or "0"

    slot = _resolve_slot(module_bay, bay_position_num, parent_bay_position)

    result = {
        "slot": slot,
        "bay_position": bay_position,
        "bay_position_num": bay_position_num,
        "parent_bay_position": parent_bay_position,
        "sfp_slot": bay_position_num,
    }
    if (
        device is not None
        and getattr(device, "virtual_chassis_id", None) is not None
        and device.vc_position is not None
    ):
        result["vc_position"] = str(device.vc_position)
    return result


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


def _record_conflict(conflicts, device, current_name, attempted_name, interface_pk=None):
    """Log a name collision at WARNING and append it to *conflicts* when collecting.

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
    if conflicts is not None:
        conflicts.append(
            {
                "device": str(device),
                "current_name": current_name,
                "attempted_name": attempted_name,
                "interface_pk": interface_pk,
            }
        )


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


def _evaluate_plain_interface(rule, module, iface, variables) -> dict | None:
    """Return a result dict if *iface* would be renamed by *rule*, else None."""
    vars_copy = {**variables, "base": iface.name}
    try:
        new_name = evaluate_name_template(rule.name_template, vars_copy)
    except ValueError as exc:
        new_name = f"<error: {exc}>"
    if new_name == iface.name:
        return None
    return {"module": module, "interface": iface, "current_name": iface.name, "new_names": [new_name]}


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
        return {"module": module, "interface": base_iface, "current_name": base_iface.name, "new_names": expected_names}
    return None


def _count_remaining_interfaces(module_qs, processed_pks) -> int:
    """Count interfaces in modules not yet visited during a find_interfaces_for_rule scan."""
    from dcim.models import Interface

    return Interface.objects.filter(module__in=module_qs.exclude(pk__in=processed_pks)).count()


def _process_channel_module(rule, module, ifaces, variables, limit, results, module_qs, processed_pks):
    """Process one module for a channel rule.  Returns (checked_count, should_stop)."""
    checked = len(ifaces)
    if not ifaces:
        return checked, False
    entry = _channel_rule_entry(rule, module, ifaces, variables)
    if entry:
        results.append(entry)
        if limit is not None and len(results) >= limit:
            return checked + _count_remaining_interfaces(module_qs, processed_pks), True
    return checked, False


def _process_plain_module(rule, module, ifaces, variables, limit, results, module_qs, processed_pks):
    """Process one module for a plain (non-channel) rule.  Returns (checked_count, should_stop)."""
    checked = 0
    for iface_idx, iface in enumerate(ifaces):
        checked += 1
        entry = _evaluate_plain_interface(rule, module, iface, variables)
        if entry:
            results.append(entry)
            if limit is not None and len(results) >= limit:
                checked += len(ifaces) - (iface_idx + 1)
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
            "new_names":    list[str],   # one entry per channel, or single-element
        }

    Only includes entries where at least one new_name differs from current_name.
    If *limit* is set the list is truncated after that many changed entries, but
    *total_checked* always reflects the full count of interfaces examined.
    """
    from collections import defaultdict

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

    A channel rule is processed ONCE per module (not per interface) so existing
    channel names are not re-created.  An unexpected failure (e.g. a save race)
    is logged and skipped so it never aborts the surrounding batch.
    """
    if not ifaces:
        return 0
    base_iface = _find_channel_base(rule, ifaces, variables)
    if id_set is not None and base_iface.pk not in id_set:
        return 0
    vars_copy = dict(variables)
    vars_copy["base"] = base_iface.name
    try:
        return _apply_rule_to_interface(rule, base_iface, vars_copy, module, conflicts=conflicts)
    except (ValueError, ValidationError, IntegrityError):
        logger.exception(
            "Failed to apply channel rule '%s' to module '%s' (id=%s); skipping.",
            rule,
            module,
            module.pk,
        )
        return 0


def _apply_plain_rule_to_module(rule, module, ifaces, variables, id_set, conflicts):
    """Apply a non-channel rule to each selected interface on one module; return the rename count.

    Each interface is independent: an unexpected failure on one is logged and
    skipped so the rest of the module (and batch) still process.
    """
    count = 0
    for iface in ifaces:
        if id_set is not None and iface.pk not in id_set:
            continue
        vars_copy = dict(variables)
        vars_copy["base"] = iface.name
        try:
            count += _apply_rule_to_interface(rule, iface, vars_copy, module, conflicts=conflicts)
        except (ValueError, ValidationError, IntegrityError):
            logger.exception(
                "Failed to apply rule '%s' to interface '%s' (id=%s); skipping.",
                rule,
                iface.name,
                iface.pk,
            )
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
    collection returns 0 immediately without touching the database.

    If *conflicts* is a list, each interface skipped because its target name is
    already taken on the device is appended to it (and logged) — letting the
    caller report how many renames were dropped.  Collisions never raise.

    Returns the number of interfaces renamed/created.
    """
    from collections import defaultdict

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

    return re.sub(r"\{([^}]+)\}", _eval_expr, result)
