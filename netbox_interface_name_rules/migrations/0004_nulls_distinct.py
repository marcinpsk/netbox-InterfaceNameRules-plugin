# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import migrations, models


class Migration(migrations.Migration):
    """Update UniqueConstraints to use nulls_distinct=False.

    NULL values in parent_module_type and device_type are now treated as equal
    for uniqueness purposes, preventing duplicate rules that differ only by NULL
    vs NULL in optional scoping fields.
    """

    dependencies = [
        ("netbox_interface_name_rules", "0003_constraints"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="interfacenamerule",
            name="interfacenamerule_unique_exact",
        ),
        migrations.RemoveConstraint(
            model_name="interfacenamerule",
            name="interfacenamerule_unique_regex",
        ),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.UniqueConstraint(
                fields=["module_type", "parent_module_type", "device_type"],
                condition=models.Q(module_type_is_regex=False),
                nulls_distinct=False,
                name="interfacenamerule_unique_exact",
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.UniqueConstraint(
                fields=["module_type_pattern", "parent_module_type", "device_type"],
                condition=models.Q(module_type_is_regex=True),
                nulls_distinct=False,
                name="interfacenamerule_unique_regex",
            ),
        ),
    ]
