import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add platform scoping field to InterfaceNameRule.

    Allows rules to be scoped to devices running a specific software platform/OS
    (e.g. SONiC, EOS, IOS-XR), enabling a single rule to replace multiple
    device-type-specific rules for the same platform.

    Unique constraints are extended to include the platform dimension so that
    (module_type, parent_module_type, device_type, platform) remains unique
    with NULL treated as equal (nulls_distinct=False).
    """

    dependencies = [
        ("netbox_interface_name_rules", "0004_nulls_distinct"),
        ("dcim", "0001_initial"),
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
        migrations.AddField(
            model_name="interfacenamerule",
            name="platform",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="dcim.platform",
                verbose_name="Platform",
                help_text="If set, rule only applies to devices running this software platform/OS",
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.UniqueConstraint(
                fields=["module_type", "parent_module_type", "device_type", "platform"],
                condition=models.Q(module_type_is_regex=False),
                nulls_distinct=False,
                name="interfacenamerule_unique_exact",
            ),
        ),
        migrations.AddConstraint(
            model_name="interfacenamerule",
            constraint=models.UniqueConstraint(
                fields=["module_type_pattern", "parent_module_type", "device_type", "platform"],
                condition=models.Q(module_type_is_regex=True),
                nulls_distinct=False,
                name="interfacenamerule_unique_regex",
            ),
        ),
    ]
