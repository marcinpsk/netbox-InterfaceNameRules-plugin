# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

import logging

from django.core.exceptions import ValidationError
from django.db import migrations

from netbox_interface_name_rules.name_template import validate_rule

logger = logging.getLogger(__name__)


def audit(apps, schema_editor):
    """Report refused stored templates without changing rules or blocking the upgrade."""
    rule_model = apps.get_model("netbox_interface_name_rules", "InterfaceNameRule")
    rules = rule_model.objects.using(schema_editor.connection.alias).values(
        "pk", "breakout_mode", "channel_count", "name_template", "parent_name_template", "applies_to_device_interfaces"
    )
    for fields in rules.iterator():
        pk = fields.pop("pk")
        try:
            validate_rule(**fields)
        except ValidationError as exc:
            for field, messages in exc.message_dict.items():
                for message in messages:
                    logger.warning("InterfaceNameRule ID %s: %s: %s", pk, field, message)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0016_restrict_breakout_mode_values"),
    ]

    operations = [migrations.RunPython(audit, migrations.RunPython.noop)]
