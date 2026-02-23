# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Change on_delete for optional scope FKs from CASCADE to SET_NULL.

    When a referenced DeviceType, Platform, or parent ModuleType is deleted,
    the rule should broaden its scope (FK becomes NULL) rather than being
    deleted along with the referenced object.
    """

    dependencies = [
        ("netbox_interface_name_rules", "0006_alter_interfacenamerule_options"),
        ("dcim", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="interfacenamerule",
            name="parent_module_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dcim.moduletype",
                verbose_name="Parent Module Type",
                help_text="If set, rule only applies when installed inside this parent module type",
            ),
        ),
        migrations.AlterField(
            model_name="interfacenamerule",
            name="device_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dcim.devicetype",
                verbose_name="Device Type",
                help_text="If set, rule only applies to devices of this device type",
            ),
        ),
        migrations.AlterField(
            model_name="interfacenamerule",
            name="platform",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dcim.platform",
                verbose_name="Platform",
                help_text="If set, rule only applies to devices running this software platform/OS",
            ),
        ),
    ]
