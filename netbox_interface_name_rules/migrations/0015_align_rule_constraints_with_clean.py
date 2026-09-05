# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Make the database refuse the rules ``clean()`` refuses, and normalize the rows that predate it."""

from django.db import migrations, models

from netbox_interface_name_rules.choices import BreakoutModeChoices


def normalize_invalid_rules(apps, schema_editor):
    """Rewrite stored rules to the shape ``clean()`` would have produced.

    Each rewrite records what the engine already does with the row, so no rule changes behavior:
    a device-level rule never reads the regex flag, never builds a family and never reads a parent
    template, and a rule with no channels takes the plain-rename branch whatever its mode says.
    """
    rule_model = apps.get_model("netbox_interface_name_rules", "InterfaceNameRule")
    device_rules = rule_model.objects.filter(applies_to_device_interfaces=True)
    device_rules.filter(module_type_is_regex=True).update(module_type_is_regex=False)
    device_rules.filter(breakout_mode=BreakoutModeChoices.CHANNELIZED).update(breakout_mode=BreakoutModeChoices.FLAT)
    rule_model.objects.filter(breakout_mode=BreakoutModeChoices.CHANNELIZED, channel_count=0).update(
        breakout_mode=BreakoutModeChoices.FLAT
    )
    rule_model.objects.exclude(breakout_mode=BreakoutModeChoices.CHANNELIZED).exclude(parent_name_template="").update(
        parent_name_template=""
    )


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0014_validate_re2_patterns"),
    ]

    operations = [
        migrations.RunPython(normalize_invalid_rules, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="interfacenamerule",
            name="interfacenamerule_module_type_mode_check",
        ),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        applies_to_device_interfaces=True,
                        module_type__isnull=True,
                        module_type_is_regex=False,
                    )
                    | models.Q(
                        applies_to_device_interfaces=False,
                        module_type_is_regex=True,
                        module_type__isnull=True,
                        module_type_pattern__gt="",
                    )
                    | models.Q(
                        applies_to_device_interfaces=False,
                        module_type_is_regex=False,
                        module_type__isnull=False,
                    )
                ),
                name="interfacenamerule_module_type_mode_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(applies_to_device_interfaces=False)
                        | ~models.Q(breakout_mode=BreakoutModeChoices.CHANNELIZED)
                    )
                    & (models.Q(applies_to_device_interfaces=False) | models.Q(parent_name_template=""))
                    & (models.Q(parent_name_template="") | models.Q(breakout_mode=BreakoutModeChoices.CHANNELIZED))
                    & (~models.Q(breakout_mode=BreakoutModeChoices.CHANNELIZED) | ~models.Q(channel_count=0))
                ),
                name="interfacenamerule_breakout_topology_check",
            ),
        ),
    ]
