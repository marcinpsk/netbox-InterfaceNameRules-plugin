# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Select an enabled interface-name rule for a module context."""

import contextlib
import threading

from django.core.exceptions import ValidationError
from django.db.models import Aggregate, F, TextField, Value
from django.db.models.functions import Cast, Coalesce, Concat, Length

from .regex_safety import compile_module_type_pattern

# Publish each loaded rule set as one new dictionary. Concurrent readers then see
# one complete version rather than a mixture of cache entries from two versions.
_RULE_CACHE = {"version": None, "exact": (), "regex": (), "memo": {}}

# Bound the number of module and scope contexts retained for one rule-set version.
_MEMO_MAX = 4096
_MEMO_MISS = object()

# A pinned batch owns a thread-local rule-set snapshot and a private memo.
_pin = threading.local()


@contextlib.contextmanager
def pinned_rule_cache():
    """Pin one enabled-rule snapshot for all selections inside the block.

    The first selection loads and fingerprints the rule set. Later selections in
    the same thread skip the fingerprint query. Nested blocks share the snapshot.
    The pin is thread-local, and an empty block does not load rules.
    """
    depth = getattr(_pin, "depth", 0)
    _pin.depth = depth + 1
    if depth == 0:
        _pin.primed = False
    try:
        yield
    finally:
        _pin.depth -= 1
        if _pin.depth == 0:
            _pin.primed = False
            for attr in ("exact", "regex", "memo"):
                _pin.__dict__.pop(attr, None)


def _compile_pattern(pattern):
    """Compile a stored pattern once, or return None when RE2 rejects it."""
    try:
        return compile_module_type_pattern(pattern)
    except ValidationError:
        return None


# These fields can change either matching or the selected rule's output. The row
# identity prevents compensating edits across two rules from preserving the hash.
_VERSION_COLUMNS = (
    "id",
    "module_type_id",
    "module_type_is_regex",
    "module_type_pattern",
    "parent_module_type_id",
    "device_type_id",
    "platform_id",
    "name_template",
    "parent_name_template",
    "breakout_mode",
    "channel_count",
    "channel_start",
    "applies_to_device_interfaces",
)


class _Md5OrderedStringAgg(Aggregate):
    """Build ``md5(string_agg(<row>, <delimiter> ORDER BY id))``."""

    function = "STRING_AGG"
    template = "MD5(%(function)s(%(expressions)s ORDER BY id))"
    output_field = TextField()


def _version_row_signature():
    """Build an unambiguous, length-prefixed signature for one rule row."""
    empty = Value("", output_field=TextField())
    colon = Value(":", output_field=TextField())
    parts = []
    for column in _VERSION_COLUMNS:
        cast = Cast(F(column), output_field=TextField())
        value = Coalesce(cast, empty, output_field=TextField()) if column.endswith("_id") else cast
        parts.append(Cast(Length(value), output_field=TextField()))
        parts.append(colon)
        parts.append(value)
    return Concat(*parts, output_field=TextField())


_ROW_SIGNATURE = _version_row_signature()


def _enabled_rules_version():
    """Return a deterministic content fingerprint of all enabled rules.

    PostgreSQL hashes the matching and output columns in primary-key order. Each
    value is length-prefixed, so arbitrary text cannot create field or row boundary
    collisions. The empty rule set has a stable empty fingerprint.
    """
    from .models import InterfaceNameRule

    return InterfaceNameRule.objects.filter(enabled=True).aggregate(
        fingerprint=Coalesce(
            _Md5OrderedStringAgg(_ROW_SIGNATURE, Value("", output_field=TextField())),
            Value("", output_field=TextField()),
        )
    )["fingerprint"]


def _get_enabled_rules():
    """Return the exact rules, regex rules, and memo for the current version.

    Exact rules retain model ordering, which reduces to primary-key order for one
    module type. Regex rules are compiled once and ordered by decreasing pattern
    length, then primary key. A reload publishes one new cache dictionary so a
    concurrent reader cannot combine values from two versions.
    """
    global _RULE_CACHE

    pinned = getattr(_pin, "depth", 0) > 0
    if pinned and getattr(_pin, "primed", False):
        # Return the thread's snapshot. Another thread can replace the shared cache.
        return _pin.exact, _pin.regex, _pin.memo

    from .models import InterfaceNameRule

    cache = _RULE_CACHE
    version = _enabled_rules_version()
    if cache["version"] != version:
        rules = list(InterfaceNameRule.objects.filter(enabled=True).order_by("module_type__model", "pk"))
        exact = tuple(rule for rule in rules if not rule.module_type_is_regex)
        regex_rules = sorted(
            (rule for rule in rules if rule.module_type_is_regex),
            key=lambda rule: (-len(rule.module_type_pattern or ""), rule.pk),
        )
        regex = tuple((_compile_pattern(rule.module_type_pattern), rule) for rule in regex_rules)
        cache = {"version": version, "exact": exact, "regex": regex, "memo": {}}
        _RULE_CACHE = cache

    if pinned:
        # Keep a private memo so another thread cannot clear this batch's entries.
        _pin.exact = cache["exact"]
        _pin.regex = cache["regex"]
        _pin.memo = dict(cache["memo"])
        _pin.primed = True
        return _pin.exact, _pin.regex, _pin.memo

    return cache["exact"], cache["regex"], cache["memo"]


def _scope_ids(parent_module_type, device_type, platform):
    """Map optional scope objects to their foreign-key values."""
    return (
        parent_module_type.pk if parent_module_type is not None else None,
        device_type.pk if device_type is not None else None,
        platform.pk if platform is not None else None,
    )


def _rule_scope_matches(rule, scope_ids):
    """Return whether a rule has exactly the requested scope values."""
    parent_module_type_id, device_type_id, platform_id = scope_ids
    return (
        rule.parent_module_type_id == parent_module_type_id
        and rule.device_type_id == device_type_id
        and rule.platform_id == platform_id
    )


def _build_candidates(parent_module_type, device_type, platform) -> list:
    """Build scope combinations from most specific to least specific."""
    seen: set = set()
    candidates = []
    parent_options = [parent_module_type, None] if parent_module_type else [None]
    device_options = [device_type, None] if device_type else [None]
    platform_options = [platform, None] if platform else [None]
    for parent in parent_options:
        for device in device_options:
            for candidate_platform in platform_options:
                candidate = (parent, device, candidate_platform)
                if candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
    return candidates


def _find_exact_match(module_type, candidates, exact_rules=None):
    """Return the first enabled exact rule in scope-precedence order."""
    if exact_rules is None:
        exact_rules, _, _ = _get_enabled_rules()

    scoped_rules = [rule for rule in exact_rules if rule.module_type_id == module_type.pk]
    for candidate in candidates:
        scope_ids = _scope_ids(*candidate)
        for rule in scoped_rules:
            if _rule_scope_matches(rule, scope_ids):
                return rule
    return None


def _find_regex_match(model_name: str, candidates, regex_rules=None):
    """Return the first enabled regex rule in scope-precedence order."""
    if regex_rules is None:
        _, regex_rules, _ = _get_enabled_rules()

    for candidate in candidates:
        scope_ids = _scope_ids(*candidate)
        for compiled, rule in regex_rules:
            if compiled is not None and _rule_scope_matches(rule, scope_ids) and compiled.fullmatch(model_name):
                return rule
    return None


def find_matching_rule(module_type, parent_module_type, device_type, platform=None):
    """Return the most specific enabled rule for a module and scope context.

    Exact module-type rules take priority over regular-expression rules. Within
    each tier, parent module type, device type, and platform scopes are tried from
    most specific to least specific.
    """
    if module_type is None:
        return None

    exact_rules, regex_rules, memo = _get_enabled_rules()
    signature = (
        module_type.pk,
        # Regex matching reads the live model name, so the memo key must include it.
        module_type.model,
        *_scope_ids(parent_module_type, device_type, platform),
    )
    # A single dictionary read cannot race with another thread's memo clear between
    # a membership check and a later subscript.
    cached = memo.get(signature, _MEMO_MISS)
    if cached is not _MEMO_MISS:
        return cached

    candidates = _build_candidates(parent_module_type, device_type, platform)
    result = _find_exact_match(module_type, candidates, exact_rules) or _find_regex_match(
        module_type.model,
        candidates,
        regex_rules,
    )
    if len(memo) >= _MEMO_MAX:
        memo.clear()
    memo[signature] = result
    return result
