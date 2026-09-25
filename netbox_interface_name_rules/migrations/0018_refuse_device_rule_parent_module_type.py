# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Make the database refuse a parent module type on a device rule, and clear the rows that carry one."""

import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)


def clear_device_rule_parent_module_types(apps, schema_editor):
    """Clear the parent module type of each device rule and log what was cleared.

    The engine never matched a device rule on it, so the only effect was on the rule ranking.
    """
    rule_model = apps.get_model("netbox_interface_name_rules", "InterfaceNameRule")
    rules = rule_model.objects.using(schema_editor.connection.alias).filter(
        applies_to_device_interfaces=True, parent_module_type__isnull=False
    )
    for pk, module_type_id, model in rules.values_list("pk", "parent_module_type", "parent_module_type__model"):
        logger.warning(
            "InterfaceNameRule ID %s: cleared parent module type %s (%s) from a device rule", pk, module_type_id, model
        )
    rules.update(parent_module_type=None)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0017_audit_name_templates"),
    ]

    operations = [
        migrations.RunPython(clear_device_rule_parent_module_types, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.CheckConstraint(
                condition=models.Q(applies_to_device_interfaces=False) | models.Q(parent_module_type__isnull=True),
                name="interfacenamerule_device_rule_scope_check",
            ),
        ),
    ]
