# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add DB-level constraints to enforce module type mutual exclusivity and uniqueness."""

    dependencies = [
        ("netbox_interface_name_rules", "0002_regex_pattern_matching"),
    ]

    operations = [
        # Enforce that exactly one of (module_type FK, module_type_pattern) is set
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(module_type_is_regex=True, module_type__isnull=True)
                    | models.Q(module_type_is_regex=False, module_type__isnull=False)
                ),
                name="interfacenamerule_module_type_xor_pattern",
            ),
        ),
        # Unique constraint for exact-match rules (module_type set, regex off)
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.UniqueConstraint(
                fields=["module_type", "parent_module_type", "device_type"],
                condition=models.Q(module_type_is_regex=False),
                name="interfacenamerule_unique_exact",
            ),
        ),
        # Unique constraint for regex rules (module_type_pattern set, regex on)
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.UniqueConstraint(
                fields=["module_type_pattern", "parent_module_type", "device_type"],
                condition=models.Q(module_type_is_regex=True),
                name="interfacenamerule_unique_regex",
            ),
        ),
    ]
