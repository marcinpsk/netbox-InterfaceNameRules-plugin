# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

from django.db import migrations

_UNICODE_SHORTHANDS = frozenset("dDsSwW")


def _uses_python_unicode_semantics(pattern):
    """Return whether Python and RE2 can interpret a legacy construct differently."""
    in_character_class = False
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            shorthand = pattern[index + 1]
            if shorthand in _UNICODE_SHORTHANDS or (shorthand in "bB" and not in_character_class):
                return True
            index += 2
            continue
        if in_character_class and pattern.startswith("[:", index):
            return True
        if character == "[":
            in_character_class = True
        elif character == "]" and in_character_class:
            in_character_class = False
        elif not in_character_class and pattern.startswith("(?", index):
            flags_end = index + 2
            while flags_end < len(pattern) and pattern[flags_end] in "imsU-":
                flags_end += 1
            flag_spec = pattern[index + 2 : flags_end]
            enabled_flags = flag_spec.split("-", 1)[0]
            if flags_end < len(pattern) and pattern[flags_end] in ":)" and "i" in enabled_flags:
                return True
        index += 1
    return False


def validate_re2_patterns(apps, schema_editor):
    """Stop the upgrade before stored patterns can change meaning under RE2."""
    import re2

    options = re2.Options()
    options.log_errors = False
    Rule = apps.get_model("netbox_interface_name_rules", "InterfaceNameRule")
    invalid_ids = []
    rules = Rule.objects.using(schema_editor.connection.alias).exclude(module_type_pattern="")
    for pk, pattern in rules.values_list("pk", "module_type_pattern").iterator():
        try:
            re2.compile(pattern, options=options)
        except re2.error:
            invalid_ids.append(pk)
            continue
        if _uses_python_unicode_semantics(pattern):
            invalid_ids.append(pk)
    if invalid_ids:
        label = "ID" if len(invalid_ids) == 1 else "IDs"
        identifiers = ", ".join(str(pk) for pk in invalid_ids)
        raise RuntimeError(
            f"Stored patterns require RE2 review for InterfaceNameRule {label}: {identifiers}. "
            "Rewrite Python-specific syntax or Unicode shorthand with explicit RE2 syntax, then retry the migration."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0013_interfacenamerule_breakout_mode_and_more"),
    ]

    operations = [
        migrations.RunPython(validate_re2_patterns, migrations.RunPython.noop),
    ]
