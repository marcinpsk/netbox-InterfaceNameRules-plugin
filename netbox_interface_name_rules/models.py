from dcim.models import DeviceType, ModuleType
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel
from taggit.managers import TaggableManager


class InterfaceNameRule(NetBoxModel):
    """Post-install interface rename rule for module types.

    Handles cases where NetBox's position-based naming can't produce
    the correct interface name, such as converter offset (CVR-X2-SFP)
    or breakout transceivers (QSFP+ 4x10G).

    The name_template uses Python str.format() syntax with these variables:
      {slot}               - Slot number from parent module bay position
      {bay_position}       - Position of the bay this module is installed into
      {bay_position_num}   - Numeric suffix of bay position (e.g., "swp1" → "1")
      {parent_bay_position} - Position of the parent module's bay
      {sfp_slot}           - Sub-bay index within the parent module
      {base}               - Base interface name from NetBox position resolution
      {channel}            - Channel number (iterated for breakout)
    """

    module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.CASCADE,
        related_name="+",
        help_text="The module type whose installation triggers this rename rule",
    )
    parent_module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        help_text="If set, rule only applies when installed inside this parent module type",
    )
    device_type = models.ForeignKey(
        DeviceType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        help_text="If set, rule only applies to devices of this parent device type",
    )
    name_template = models.CharField(
        max_length=255,
        help_text=(
            "Interface name template expression, e.g. "
            "'GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}'"
        ),
    )
    channel_count = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of breakout channels (0 = no breakout). Creates this many interfaces per template.",
    )
    channel_start = models.PositiveSmallIntegerField(
        default=0,
        help_text="Starting channel number for breakout interfaces (e.g., 0 for Juniper, 1 for Cisco)",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this rule",
    )

    # Override inherited tags to avoid reverse accessor clash when co-installed
    # with another plugin that has a model of the same name.
    tags = TaggableManager(through="extras.TaggedItem", related_name="+")

    def get_absolute_url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_detail", args=[self.pk])

    class Meta:
        unique_together = ["module_type", "parent_module_type", "device_type"]
        ordering = ["module_type__model"]

    def __str__(self):
        parent = f" in {self.parent_module_type.model}" if self.parent_module_type else ""
        device = f" on {self.device_type.model}" if self.device_type else ""
        return f"{self.module_type.model}{parent}{device} → {self.name_template}"
