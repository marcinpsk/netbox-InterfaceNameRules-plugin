# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

from django.db import migrations


def validate_re2_patterns(apps, schema_editor):
    """Stop the upgrade before RE2-incompatible stored patterns can execute."""
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
    if invalid_ids:
        label = "ID" if len(invalid_ids) == 1 else "IDs"
        identifiers = ", ".join(str(pk) for pk in invalid_ids)
        raise RuntimeError(
            f"RE2 cannot compile stored patterns for InterfaceNameRule {label}: {identifiers}. "
            "Update these patterns to RE2 syntax, then retry the migration."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0013_interfacenamerule_breakout_mode_and_more"),
    ]

    operations = [
        migrations.RunPython(validate_re2_patterns, migrations.RunPython.noop),
    ]
