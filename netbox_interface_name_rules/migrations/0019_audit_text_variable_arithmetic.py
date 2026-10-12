# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

import logging

from django.db import migrations

from netbox_interface_name_rules.name_template import log_refused_rules

logger = logging.getLogger(__name__)


def audit(apps, schema_editor):
    """Report refused stored templates without changing rules or blocking the upgrade."""
    rule_model = apps.get_model("netbox_interface_name_rules", "InterfaceNameRule")
    log_refused_rules(rule_model.objects.using(schema_editor.connection.alias), logger)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_interface_name_rules", "0018_refuse_device_rule_parent_module_type"),
    ]

    operations = [migrations.RunPython(audit, migrations.RunPython.noop)]
