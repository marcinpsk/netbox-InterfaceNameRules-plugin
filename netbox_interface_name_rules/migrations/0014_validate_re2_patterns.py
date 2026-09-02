# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

_UNICODE_SHORTHANDS = frozenset("dDsSwW")
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


def _re2_differences(pattern):
    """Return whether *pattern* can match different text under RE2, and whether it only narrows to ASCII.

    A narrowing construct drops Unicode matches that RE2 reads as ASCII, so it can skip a rename.
    A breaking construct can match text Python never matched, so it can rename the wrong interface.
    """
    breaks = False
    narrows = False
    in_character_class = False
    character_class_has_content = False
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            shorthand = pattern[index + 1]
            if shorthand in _UNICODE_SHORTHANDS or (shorthand in "bB" and not in_character_class):
                narrows = True
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
            elif character != "^" or character_class_has_content:
                character_class_has_content = True
        elif character == "[":
            in_character_class = True
            character_class_has_content = False
        elif character == "{" and _counted_repeat_uses_different_semantics(pattern, index):
            breaks = True
        elif pattern.startswith("(?", index):
            flags_end = index + 2
            while flags_end < len(pattern) and pattern[flags_end] in "imsU-":
                flags_end += 1
            flag_spec = pattern[index + 2 : flags_end]
            enabled_flags = flag_spec.split("-", 1)[0]
            if flags_end < len(pattern) and pattern[flags_end] in ":)" and "i" in enabled_flags:
                narrows = True
        index += 1
    return breaks, narrows


def _uses_different_re2_semantics(pattern):
    """Return whether RE2 can match text on this pattern that Python never matched."""
    return _re2_differences(pattern)[0]


def _narrows_to_ascii(pattern):
    """Return whether RE2 reads this pattern as ASCII where Python read it as Unicode."""
    return _re2_differences(pattern)[1]


def validate_re2_patterns(apps, schema_editor):
    """Stop the upgrade before stored patterns can match a different interface under RE2."""
    import re

    import re2

    options = re2.Options()
    options.log_errors = False
    Rule = apps.get_model("netbox_interface_name_rules", "InterfaceNameRule")
    invalid_ids = []
    narrowed_ids = []
    rules = Rule.objects.using(schema_editor.connection.alias).exclude(module_type_pattern="")
    for pk, pattern in rules.values_list("pk", "module_type_pattern").iterator():
        breaks, narrows = _re2_differences(pattern)
        if narrows:
            narrowed_ids.append(pk)
        if breaks:
            invalid_ids.append(pk)
            continue
        try:
            re.compile(pattern)
            re2.compile(pattern, options=options)
        except (OverflowError, re.error, re2.error):
            invalid_ids.append(pk)
    if narrowed_ids:
        label = "ID" if len(narrowed_ids) == 1 else "IDs"
        identifiers = ", ".join(str(pk) for pk in narrowed_ids)
        logger.warning(
            "RE2 reads \\d, \\s, \\w and \\b as ASCII for InterfaceNameRule %s: %s. "
            "These rules keep matching ASCII interface names. They no longer match a non-ASCII digit "
            "or letter. Write an RE2 Unicode property such as \\p{L} where a rule must still match one.",
            label,
            identifiers,
        )
    if invalid_ids:
        label = "ID" if len(invalid_ids) == 1 else "IDs"
        identifiers = ", ".join(str(pk) for pk in invalid_ids)
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
