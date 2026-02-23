# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0001_initial"),
    ]

    operations = [
        # Add regex pattern fields
        migrations.AddField(
            model_name="interfacenamerule",
            name="module_type_pattern",
            field=models.CharField(
                blank=True,
                default="",
                max_length=255,
                help_text=(
                    "Regex pattern to match module type model name (e.g. 'QSFP-DD-400G-.*'). "
                    "Uses Python re.fullmatch() — pattern must match the entire model name."
                ),
            ),
        ),
        migrations.AddField(
            model_name="interfacenamerule",
            name="module_type_is_regex",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, use regex pattern instead of exact module type FK",
            ),
        ),
        # Make module_type FK nullable (required for regex-only rules)
        migrations.AlterField(
            model_name="interfacenamerule",
            name="module_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="dcim.moduletype",
                help_text="The module type whose installation triggers this rename rule (exact match)",
            ),
        ),
        # Remove old unique_together (module_type is now nullable)
        migrations.AlterUniqueTogether(
            name="interfacenamerule",
            unique_together=set(),
        ),
    ]
