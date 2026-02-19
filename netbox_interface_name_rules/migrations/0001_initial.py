import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("dcim", "0225_gfk_indexes"),
        ("extras", "0134_owner"),
    ]

    operations = [
        migrations.CreateModel(
            name="InterfaceNameRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder),
                ),
                (
                    "name_template",
                    models.CharField(
                        max_length=255,
                        help_text=(
                            "Interface name template expression, e.g. "
                            "'GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}'"
                        ),
                    ),
                ),
                (
                    "channel_count",
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text="Number of breakout channels (0 = no breakout). Creates this many interfaces per template.",
                    ),
                ),
                (
                    "channel_start",
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text="Starting channel number for breakout interfaces (e.g., 0 for Juniper, 1 for Cisco)",
                    ),
                ),
                (
                    "description",
                    models.TextField(blank=True, help_text="Optional description or notes about this rule"),
                ),
                (
                    "module_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="dcim.moduletype",
                        help_text="The module type whose installation triggers this rename rule",
                    ),
                ),
                (
                    "parent_module_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="dcim.moduletype",
                        help_text="If set, rule only applies when installed inside this parent module type",
                    ),
                ),
                (
                    "device_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="dcim.devicetype",
                        help_text="If set, rule only applies to devices of this parent device type",
                    ),
                ),
                (
                    "tags",
                    taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag", related_name="+"),
                ),
            ],
            options={
                "ordering": ["module_type__model"],
                "unique_together": {("module_type", "parent_module_type", "device_type")},
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
    ]
