# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import re

from dcim.models import DeviceType, ModuleType, Platform
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel
from taggit.managers import TaggableManager


_REDOS_PATTERN = re.compile(r"(\+\*|\*\+|\?\?|\)\s*[\+\*\?]\s*[\+\*\?]|\)\s*\{[^{}]+\}\s*[\+\*\?])")


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

    Module type matching supports two modes:
      - Exact: FK reference to a specific ModuleType (default)
      - Regex: Pattern matched against ModuleType.model via re.fullmatch()

    Scoping fields (all optional):
      - parent_module_type: match only when installed inside this module type
      - device_type:        match only devices of this hardware model
      - platform:           match only devices running this software platform/OS
    """

    module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Module Type",
        help_text="The module type whose installation triggers this rename rule (exact match)",
    )
    module_type_pattern = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Module Type Pattern",
        help_text="Regex pattern to match module type model name (e.g. 'QSFP-DD-400G-.*'). "
        "Uses Python re.fullmatch() — pattern must match the entire model name.",
    )
    module_type_is_regex = models.BooleanField(
        default=False,
        verbose_name="Use Regex Pattern",
        help_text="When enabled, use regex pattern instead of exact module type FK",
    )
    parent_module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Parent Module Type",
        help_text="If set, rule only applies when installed inside this parent module type",
    )
    device_type = models.ForeignKey(
        DeviceType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Device Type",
        help_text="If set, rule only applies to devices of this device type",
    )
    platform = models.ForeignKey(
        Platform,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Platform",
        help_text="If set, rule only applies to devices running this software platform/OS",
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
        help_text="Starting channel number for breakout interfaces (e.g., 0 for Juniper; Cisco varies by model—check device docs)",
    )
    description = models.TextField(
        blank=True,
        help_text="Optional description or notes about this rule",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="When disabled, this rule is ignored during module installation and Apply Rules operations.",
    )

    # Override inherited tags to avoid reverse accessor clash when co-installed
    # with another plugin that has a model of the same name.
    tags = TaggableManager(through="extras.TaggedItem", related_name="+")

    def clean(self):
        super().clean()
        if self.module_type_is_regex:
            if not self.module_type_pattern:
                raise ValidationError({"module_type_pattern": "Regex pattern is required when regex mode is enabled."})
            if self.module_type:
                raise ValidationError({"module_type": "Cannot set both module type FK and regex pattern. Choose one."})
            try:
                re.compile(self.module_type_pattern)
            except re.error as e:
                raise ValidationError({"module_type_pattern": f"Invalid regex pattern: {e}"})
            # Basic ReDoS guard: reject common nested-quantifier constructs
            if _REDOS_PATTERN.search(self.module_type_pattern):
                raise ValidationError(
                    {"module_type_pattern": "Pattern contains potentially unsafe nested quantifiers."}
                )
        else:
            # Clear any stale pattern so it does not persist when switching modes
            self.module_type_pattern = ""
            if not self.module_type:
                raise ValidationError({"module_type": "Module type is required when regex mode is disabled."})

    def get_absolute_url(self):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_detail", args=[self.pk])

    clone_fields = [
        "module_type",
        "module_type_pattern",
        "module_type_is_regex",
        "parent_module_type",
        "device_type",
        "platform",
        "name_template",
        "channel_count",
        "channel_start",
        "description",
        "enabled",
    ]

    @property
    def specificity_score(self) -> int:
        """Numeric priority score — higher beats lower in rule lookup.

        The engine selects rules in this order (``find_matching_rule``):

        1. **Exact FK match** always outranks regex at any scope.
        2. **Scope specificity** (more constraints = higher priority):
           parent_module_type contributes 4 pts, device_type 2 pts, platform 1 pt.
           This mirrors the candidate-iteration order in the engine.
        3. **Regex pattern length** (longer = more specific string match).

        Score layout:
          - Exact FK rules: 1000 + scope  (1000–1007)
          - Regex rules:    scope × 100 + len(pattern)
            e.g. device-scoped 15-char pattern → 2×100+15 = 215
                 platform-scoped 2-char pattern → 1×100+2 = 102
          Exact rules always outrank regex (max regex score with scope=7,
          max_length=255 would be 7×100+255 = 955, so 1000 safely exceeds
          any possible regex score).

        Scope bit weights: parent_module_type=4, device_type=2, platform=1.
        Two rules with the same score fall back to lowest pk (first created).
        """
        scope = (
            (4 if self.parent_module_type_id else 0)
            + (2 if self.device_type_id else 0)
            + (1 if self.platform_id else 0)
        )
        if not self.module_type_is_regex:
            return 1000 + scope
        return scope * 100 + len(self.module_type_pattern)

    @property
    def specificity_label(self) -> str:
        """Short human-readable description of what this rule matches."""
        mode = "exact" if not self.module_type_is_regex else f"regex({len(self.module_type_pattern)})"
        parts = []
        if self.parent_module_type_id:
            parts.append("parent")
        if self.device_type_id:
            parts.append("device")
        if self.platform_id:
            parts.append("platform")
        scope = "+".join(parts) if parts else "global"
        return f"{mode} / {scope}"

    class Meta:
        ordering = ["module_type__model", "pk"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(module_type_is_regex=True, module_type__isnull=True, module_type_pattern__gt="")
                    | models.Q(module_type_is_regex=False, module_type__isnull=False)
                ),
                name="interfacenamerule_module_type_mode_check",
            ),
            models.UniqueConstraint(
                fields=["module_type", "parent_module_type", "device_type", "platform"],
                condition=models.Q(module_type_is_regex=False),
                nulls_distinct=False,
                name="interfacenamerule_unique_exact",
            ),
            models.UniqueConstraint(
                fields=["module_type_pattern", "parent_module_type", "device_type", "platform"],
                condition=models.Q(module_type_is_regex=True),
                nulls_distinct=False,
                name="interfacenamerule_unique_regex",
            ),
        ]

    def __str__(self):
        if self.module_type_is_regex:
            module = f"/{self.module_type_pattern}/"
        else:
            module = self.module_type.model if self.module_type else "?"
        parent = f" in {self.parent_module_type.model}" if self.parent_module_type else ""
        device = f" on {self.device_type.model}" if self.device_type else ""
        platform = f" [{self.platform.name}]" if self.platform else ""
        return f"{module}{parent}{device}{platform} → {self.name_template}"
