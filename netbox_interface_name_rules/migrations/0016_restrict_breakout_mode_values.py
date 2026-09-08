# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Restrict breakout modes to the supported values."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0015_align_rule_constraints_with_clean"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="interfacenamerule",
            name="interfacenamerule_breakout_topology_check",
        ),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("breakout_mode__in", ["flat", "channelized"]),
                    models.Q(
                        ("applies_to_device_interfaces", False),
                        models.Q(("breakout_mode", "channelized"), _negated=True),
                        _connector="OR",
                    ),
                    models.Q(("applies_to_device_interfaces", False), ("parent_name_template", ""), _connector="OR"),
                    models.Q(("parent_name_template", ""), ("breakout_mode", "channelized"), _connector="OR"),
                    models.Q(
                        models.Q(("breakout_mode", "channelized"), _negated=True),
                        models.Q(("channel_count", 0), _negated=True),
                        _connector="OR",
                    ),
                ),
                name="interfacenamerule_breakout_topology_check",
            ),
        ),
    ]
