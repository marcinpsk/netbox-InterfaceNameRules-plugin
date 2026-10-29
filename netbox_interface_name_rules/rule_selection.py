# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Select an enabled interface-name rule for a module context."""

import contextlib
import threading
from dataclasses import dataclass

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db.models import Aggregate, F, Subquery, TextField, Value
from django.db.models.functions import Cast, Coalesce, Concat, Length

from .regex_safety import compile_module_type_pattern


@dataclass(frozen=True, slots=True)
class _RuleSnapshot:
    """One enabled-rule set and platform tree read from one pair of aliases; only the memo changes after publication."""

    aliases: tuple | None
    version: str | None
    exact: tuple
    regex: tuple
    platform_parents: dict
    memo: dict


def _empty_rule_cache():
    """Return a new snapshot that matches no alias, so the next read reloads."""
    return _RuleSnapshot(aliases=None, version=None, exact=(), regex=(), platform_parents={}, memo={})


# A reload rebinds this to a new snapshot, so a reader never mixes entries from two versions.
_RULE_CACHE = _empty_rule_cache()

# Bound the number of module and scope contexts retained for one rule-set version.
_MEMO_MAX = 4096
_MEMO_MISS = object()

# A pinned batch owns a thread-local rule-set snapshot and a private memo.
_pin = threading.local()


@contextlib.contextmanager
def pinned_rule_cache():
    """Pin one enabled-rule snapshot for all selections inside the block.

    The first selection loads and fingerprints the rule set. Later selections in
    the same thread and from the same read aliases skip the fingerprint query.
    Nested blocks share the snapshot. The pin is thread-local, and an empty block
    does not load rules.
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
            for attr in ("aliases", "exact", "regex", "platform_parents", "memo"):
                _pin.__dict__.pop(attr, None)


def module_types_matching_pattern(pattern):
    """Return the model names *pattern* matches among the module types that exist now.

    Compiles and full-matches exactly as selection does, so a pattern that returns nothing here
    also selects nothing when a module is installed.
    """
    from dcim.models import ModuleType

    compiled = compile_module_type_pattern(pattern)
    return tuple(model for model in ModuleType.objects.values_list("model", flat=True) if compiled.fullmatch(model))


def _enabled_module_rules():
    """Return the queryset of the enabled rules that module selection can pick.

    Device-interface rows reuse module_type_pattern as an interface-name filter, so they are never module rules.
    """
    from .models import InterfaceNameRule

    return InterfaceNameRule.objects.filter(enabled=True, applies_to_device_interfaces=False)


def parent_type_scopes_a_rule(module_type_ids) -> bool:
    """Return whether an enabled module rule has one of *module_type_ids* as its parent module type.

    One EXISTS query: the rule cache would load every rule during the save when it is not current.
    """
    return _enabled_module_rules().filter(parent_module_type_id__in=module_type_ids).exists()


def compile_stored_pattern(pattern):
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
        parts.extend((Cast(Length(value), output_field=TextField()), colon, value))
    return Concat(*parts, output_field=TextField())


_ROW_SIGNATURE = _version_row_signature()


def _platform_tree_hash():
    """Return a one-row queryset that hashes the parent of every platform in primary-key order."""
    from dcim.models import Platform

    text = TextField()
    row = Concat(
        Cast(F("id"), output_field=text),
        Value(":", output_field=text),
        Coalesce(Cast(F("parent_id"), output_field=text), Value("", output_field=text)),
        output_field=text,
    )
    # Grouping on a constant aggregates every platform into one row.
    return (
        Platform.objects.order_by()
        .annotate(tree=Value(1))
        .values("tree")
        .annotate(signature=_Md5OrderedStringAgg(row, Value(",", output_field=text)))
        .values_list("signature", flat=True)
    )


def platforms_nest() -> bool:
    """Return whether this NetBox nests platforms (4.4+); before that, every platform is a root.

    Probed from the Platform model rather than a version comparison, as ``supports_channelization`` does.
    """
    from dcim.models import Platform

    try:
        Platform._meta.get_field("parent")
    except FieldDoesNotExist:  # pragma: no cover - only reachable on NetBox 4.3
        return False
    return True


def _enabled_rules_version(alias, platform_alias):
    """Return a deterministic content fingerprint of the enabled rules on *alias* and of the platform tree.

    PostgreSQL hashes the matching and output columns in primary-key order. Each
    value is length-prefixed, so arbitrary text cannot create field or row boundary
    collisions. The empty rule set has a stable empty rules part. The platform
    tree decides which rules a device platform matches, so a tree edit changes the
    fingerprint too. One query reads both when they share an alias. With exempt
    rule models in a branch, the rules are on main and the tree is on the branch,
    so a second query reads the tree. Without nested platforms there is no tree to hash.
    """
    from .models import InterfaceNameRule

    text = TextField()
    empty = Value("", output_field=text)
    rules_hash = Coalesce(_Md5OrderedStringAgg(_ROW_SIGNATURE, empty), empty)
    rules = InterfaceNameRule.objects.using(alias).filter(enabled=True)
    if not platforms_nest():  # pragma: no cover - only reachable on NetBox 4.3
        return rules.aggregate(fingerprint=rules_hash)["fingerprint"]
    if platform_alias == alias:
        tree_hash = Coalesce(Subquery(_platform_tree_hash()), empty)
        return rules.aggregate(
            fingerprint=Concat(rules_hash, Value("/", output_field=text), tree_hash, output_field=text)
        )["fingerprint"]
    tree_hash = next(iter(_platform_tree_hash().using(platform_alias)), None) or ""
    return f"{rules.aggregate(fingerprint=rules_hash)['fingerprint']}/{tree_hash}"


def _get_enabled_rules():
    """Return the exact rules, regex rules, platform parents and memo for the current version on the read alias.

    Exact rules retain model ordering, which reduces to primary-key order for one
    module type. Regex rules are compiled once and ordered by decreasing pattern
    length, then primary key. The platform parents map each platform pk to its
    parent pk. A reload publishes one new cache snapshot so a concurrent reader
    cannot combine values from two versions.
    """
    from dcim.models import Platform

    # One module-level cache, replaced atomically.
    global _RULE_CACHE  # noqa: PLW0603

    # A branch can hold other rules or another platform tree than main, so a snapshot belongs to its read aliases.
    alias = _enabled_module_rules().db
    platform_alias = Platform.objects.db
    aliases = (alias, platform_alias)
    pinned = getattr(_pin, "depth", 0) > 0
    if pinned and getattr(_pin, "primed", False) and _pin.aliases == aliases:
        # Return the thread's snapshot. Another thread can replace the shared cache.
        return _pin.exact, _pin.regex, _pin.platform_parents, _pin.memo

    cache = _RULE_CACHE
    version = _enabled_rules_version(alias, platform_alias)
    if (cache.aliases, cache.version) != (aliases, version):
        rules = list(_enabled_module_rules().using(alias).order_by("module_type__model", "pk"))
        exact = tuple(rule for rule in rules if not rule.module_type_is_regex)
        regex_rules = sorted(
            (rule for rule in rules if rule.module_type_is_regex),
            key=lambda rule: (-len(rule.module_type_pattern or ""), rule.pk),
        )
        regex = tuple((compile_stored_pattern(rule.module_type_pattern), rule) for rule in regex_rules)
        platform_parents = (
            dict(Platform.objects.using(platform_alias).order_by().values_list("pk", "parent_id"))
            if platforms_nest()
            else {}
        )
        cache = _RuleSnapshot(
            aliases=aliases, version=version, exact=exact, regex=regex, platform_parents=platform_parents, memo={}
        )
        _RULE_CACHE = cache

    if pinned:
        # Keep a private memo so another thread cannot clear this batch's entries.
        _pin.aliases = aliases
        _pin.exact = cache.exact
        _pin.regex = cache.regex
        _pin.platform_parents = cache.platform_parents
        _pin.memo = dict(cache.memo)
        _pin.primed = True
        return _pin.exact, _pin.regex, _pin.platform_parents, _pin.memo

    return cache.exact, cache.regex, cache.platform_parents, cache.memo


def _scope_ids(parent_module_type, device_type, platform):
    """Map optional scope objects to their foreign-key values."""
    return (
        parent_module_type.pk if parent_module_type is not None else None,
        device_type.pk if device_type is not None else None,
        platform.pk if platform is not None else None,
    )


def _platform_lineage(platform_id, platform_parents) -> list:
    """Return *platform_id* and the pk of each of its ancestors, the closest first.

    A platform that the snapshot does not know, such as one created in a pinned batch, has no ancestors.
    """
    lineage = []
    while platform_id is not None:
        if platform_id in lineage:
            raise ValueError(f"Platform {platform_id} is its own ancestor.")
        lineage.append(platform_id)
        platform_id = platform_parents.get(platform_id)
    return lineage


def platform_lineage(platform) -> list:
    """Return the pk of *platform* and of each of its ancestors, the closest first, from the rule snapshot."""
    if platform is None:
        return []
    _, _, platform_parents, _ = _get_enabled_rules()
    return _platform_lineage(platform.pk, platform_parents)


def platform_subtree(platform_id) -> set:
    """Return *platform_id* and the pk of each of its descendants, from the rule snapshot."""
    _, _, platform_parents, _ = _get_enabled_rules()
    descendants = (pk for pk in platform_parents if platform_id in _platform_lineage(pk, platform_parents))
    return {platform_id, *descendants}


def _rule_scope_matches(rule, scope_ids):
    """Return whether a rule has exactly the requested scope values."""
    parent_module_type_id, device_type_id, platform_id = scope_ids
    return (
        rule.parent_module_type_id == parent_module_type_id
        and rule.device_type_id == device_type_id
        and rule.platform_id == platform_id
    )


def _build_candidates(parent_module_type_id, device_type_id, platform_lineage) -> list:
    """Build scope-id combinations from most specific to least specific; a closer platform comes first."""
    parent_options = [parent_module_type_id, None] if parent_module_type_id is not None else [None]
    device_options = [device_type_id, None] if device_type_id is not None else [None]
    platform_options = [*platform_lineage, None]
    return [
        (parent, device, platform)
        for parent in parent_options
        for device in device_options
        for platform in platform_options
    ]


def _find_exact_match(module_type, candidates, exact_rules=None):
    """Return the first enabled exact rule in scope-precedence order."""
    if exact_rules is None:
        exact_rules, _, _, _ = _get_enabled_rules()

    scoped_rules = [rule for rule in exact_rules if rule.module_type_id == module_type.pk]
    for candidate in candidates:
        for rule in scoped_rules:
            if _rule_scope_matches(rule, candidate):
                return rule
    return None


def _find_regex_match(model_name: str, candidates, regex_rules=None):
    """Return the first enabled regex rule in scope-precedence order."""
    if regex_rules is None:
        _, regex_rules, _, _ = _get_enabled_rules()

    for candidate in candidates:
        for compiled, rule in regex_rules:
            if compiled is not None and _rule_scope_matches(rule, candidate) and compiled.fullmatch(model_name):
                return rule
    return None


def find_matching_rule(module_type, parent_module_type, device_type, platform=None):
    """Return the most specific enabled rule for a module and scope context.

    Exact module-type rules take priority over regular-expression rules. Within
    each tier, parent module type, device type, and platform scopes are tried from
    most specific to least specific. A rule platform matches the device platform and
    each of its ancestors, and a closer platform comes before a farther one.
    """
    if module_type is None:
        return None

    exact_rules, regex_rules, platform_parents, memo = _get_enabled_rules()
    scope_ids = _scope_ids(parent_module_type, device_type, platform)
    # Regex matching reads the live model name, so the memo key must include it.
    signature = (module_type.pk, module_type.model, *scope_ids)
    # A single dictionary read cannot race with another thread's memo clear between
    # a membership check and a later subscript.
    cached = memo.get(signature, _MEMO_MISS)
    if cached is not _MEMO_MISS:
        return cached

    parent_module_type_id, device_type_id, platform_id = scope_ids
    candidates = _build_candidates(
        parent_module_type_id, device_type_id, _platform_lineage(platform_id, platform_parents)
    )
    result = _find_exact_match(module_type, candidates, exact_rules) or _find_regex_match(
        module_type.model,
        candidates,
        regex_rules,
    )
    if len(memo) >= _MEMO_MAX:
        memo.clear()
    memo[signature] = result
    return result
