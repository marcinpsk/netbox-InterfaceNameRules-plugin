# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

import logging

from django.db import migrations
from django.db.models import Q

logger = logging.getLogger(__name__)

_NARROWING_SHORTHANDS = frozenset("dsw")
_BREAKING_SHORTHANDS = frozenset("DSW")
_POSIX_CLASSES = frozenset(
    {
        "alnum",
        "alpha",
        "ascii",
        "blank",
        "cntrl",
        "digit",
        "graph",
        "lower",
        "print",
        "punct",
        "space",
        "upper",
        "word",
        "xdigit",
    }
)


def _is_ascii_decimal(value):
    """Return whether a repetition bound contains only ASCII decimal digits."""
    return bool(value) and all("0" <= character <= "9" for character in value)


def _counted_repeat_uses_different_semantics(pattern, opening_index):
    """Return whether Python accepts a counted repeat that RE2 treats literally."""
    closing_index = pattern.find("}", opening_index + 1)
    if closing_index < 0:
        return False
    bounds = pattern[opening_index + 1 : closing_index]
    if "," not in bounds:
        return _is_ascii_decimal(bounds) and len(bounds) > 1 and bounds.startswith("0")
    if bounds.count(",") != 1:
        return False
    lower, upper = bounds.split(",", 1)
    if (lower and not _is_ascii_decimal(lower)) or (upper and not _is_ascii_decimal(upper)):
        return False
    return not lower or (len(lower) > 1 and lower.startswith("0")) or (len(upper) > 1 and upper.startswith("0"))


def _escaped_shorthand_re2_differences(shorthand, in_character_class, character_class_is_negated):
    """Classify one escaped shorthand by its match direction under RE2."""
    if shorthand in _NARROWING_SHORTHANDS:
        breaks = in_character_class and character_class_is_negated
        return breaks, not breaks
    if shorthand in _BREAKING_SHORTHANDS:
        narrows = in_character_class and character_class_is_negated
        return not narrows, narrows
    breaks = shorthand in "bB" and not in_character_class
    return breaks, False


def _case_insensitive_re2_differences(case_insensitive, has_negated_character_class):
    """Classify case-insensitive matching by whether the pattern also negates a character class."""
    return (
        case_insensitive and has_negated_character_class,
        case_insensitive and not has_negated_character_class,
    )


def _inline_case_flag(pattern, opening_index, current):
    """Return the end, scope type, and case state for one inline flag group."""
    flags_start = opening_index + 2
    flags_end = flags_start
    while flags_end < len(pattern) and pattern[flags_end] in "imsU-":
        flags_end += 1
    if flags_end == flags_start or flags_end >= len(pattern) or pattern[flags_end] not in ":)":
        return None

    enabled, separator, disabled = pattern[flags_start:flags_end].partition("-")
    case_insensitive = current
    if "i" in enabled:
        case_insensitive = True
    if separator and "i" in disabled:
        case_insensitive = False
    return flags_end, pattern[flags_end] == ":", case_insensitive


def _re2_differences(pattern):
    """Return whether *pattern* can match different text under RE2, and whether it only narrows matching.

    A narrowing construct drops Unicode matches under RE2, so it can skip a rename.
    A breaking construct can match text Python never matched, so it can rename the wrong interface.
    """
    breaks = False
    narrows = False
    has_case_insensitive_scope = False
    has_case_insensitive_negated_character_class = False
    case_insensitive_scopes = [False]
    in_character_class = False
    character_class_has_content = False
    character_class_is_negated = False
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            shorthand = pattern[index + 1]
            escape_breaks, escape_narrows = _escaped_shorthand_re2_differences(
                shorthand, in_character_class, character_class_is_negated
            )
            breaks = breaks or escape_breaks
            narrows = narrows or escape_narrows
            character_class_has_content = character_class_has_content or in_character_class
            index += 2
            continue
        if in_character_class:
            if pattern.startswith("[:", index):
                class_end = pattern.find(":]", index + 2)
                if class_end >= 0:
                    class_name = pattern[index + 2 : class_end].removeprefix("^")
                    if class_name in _POSIX_CLASSES:
                        breaks = True
            if character == "]" and character_class_has_content:
                in_character_class = False
            elif character == "^" and not character_class_has_content and not character_class_is_negated:
                character_class_is_negated = True
                has_case_insensitive_negated_character_class = (
                    has_case_insensitive_negated_character_class or case_insensitive_scopes[-1]
                )
            else:
                character_class_has_content = True
        elif character == "[":
            in_character_class = True
            character_class_has_content = False
            character_class_is_negated = False
        elif character == "{" and _counted_repeat_uses_different_semantics(pattern, index):
            breaks = True
        elif character == "(":
            inline_flag = _inline_case_flag(pattern, index, case_insensitive_scopes[-1])
            if inline_flag is not None:
                flags_end, is_scoped, case_insensitive = inline_flag
                has_case_insensitive_scope = has_case_insensitive_scope or case_insensitive
                if is_scoped:
                    case_insensitive_scopes.append(case_insensitive)
                else:
                    case_insensitive_scopes[-1] = case_insensitive
                index = flags_end + 1
                continue
            case_insensitive_scopes.append(case_insensitive_scopes[-1])
        elif character == ")" and len(case_insensitive_scopes) > 1:
            case_insensitive_scopes.pop()
        index += 1
    case_breaks, case_narrows = _case_insensitive_re2_differences(
        has_case_insensitive_scope,
        has_case_insensitive_negated_character_class,
    )
    return breaks or case_breaks, narrows or case_narrows


def _uses_different_re2_semantics(pattern):
    """Return whether RE2 can match text on this pattern that Python never matched."""
    return _re2_differences(pattern)[0]


def _narrows_to_ascii(pattern):
    """Return whether RE2 can only drop matches from this pattern."""
    return _re2_differences(pattern)[1]


def validate_re2_patterns(apps, schema_editor):
    """Stop the upgrade before stored patterns can match a different interface under RE2."""
    import re

    import re2

    options = re2.Options()
    options.log_errors = False
    rule_model = apps.get_model("netbox_interface_name_rules", "InterfaceNameRule")
    invalid_patterns = []
    narrowed_ids = []
    rules = (
        rule_model.objects.using(schema_editor.connection.alias)
        .filter(Q(module_type_is_regex=True) | Q(applies_to_device_interfaces=True))
        .exclude(module_type_pattern="")
    )
    for pk, pattern in rules.values_list("pk", "module_type_pattern").iterator():
        breaks, narrows = _re2_differences(pattern)
        if narrows:
            narrowed_ids.append(pk)
        if breaks:
            invalid_patterns.append((pk, pattern))
            continue
        try:
            re.compile(pattern)
            re2.compile(pattern, options=options)
        except (OverflowError, re.error, re2.error):
            invalid_patterns.append((pk, pattern))
    if narrowed_ids:
        label = "ID" if len(narrowed_ids) == 1 else "IDs"
        identifiers = ", ".join(str(pk) for pk in narrowed_ids)
        logger.warning(
            "RE2 narrows Unicode matching for InterfaceNameRule %s: %s. It reads \\d, \\s and \\w as ASCII, "
            "and handles some Unicode case folds more narrowly. These rules can skip a non-ASCII match. "
            "Write an RE2 Unicode property such as \\p{L} where a rule must still match one.",
            label,
            identifiers,
        )
    if invalid_patterns:
        label = "ID" if len(invalid_patterns) == 1 else "IDs"
        identifiers = ", ".join(f"{pk} ({pattern!r})" for pk, pattern in invalid_patterns)
        raise RuntimeError(
            f"Stored patterns require RE2 review for InterfaceNameRule {label}: {identifiers}. "
            "Rewrite Python-specific or semantically different shared syntax with explicit RE2 syntax, then retry the migration."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0013_interfacenamerule_breakout_mode_and_more"),
    ]

    operations = [
        migrations.RunPython(validate_re2_patterns, migrations.RunPython.noop),
    ]
