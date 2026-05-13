# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import migrations, models


class Migration(migrations.Migration):
    """Tighten the module-type mode check to require a non-empty pattern in regex mode."""

    dependencies = [
        ("netbox_interface_name_rules", "0007_alter_optional_fks_set_null"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="interfacenamerule",
            name="interfacenamerule_module_type_xor_pattern",
        ),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(module_type_is_regex=True, module_type__isnull=True, module_type_pattern__gt="")
                    | models.Q(module_type_is_regex=False, module_type__isnull=False)
                ),
                name="interfacenamerule_module_type_mode_check",
            ),
        ),
    ]
