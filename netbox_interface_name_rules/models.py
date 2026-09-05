# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import ast
import re

from dcim.models import DeviceType, ModuleType, Platform
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel
from taggit.managers import TaggableManager

from .choices import BreakoutModeChoices
from .regex_safety import compile_module_type_pattern

_TEMPLATE_FIELD = re.compile(r"\{([^{}]*)\}")


def _expression_names_channel(field):
    """Return True when the brace group *field* parses as an expression naming ``channel``.

    Mirrors how ``evaluate_name_template`` reads a brace group — ``ast.parse`` plus a walk — so
    ``{channel + 1}`` is caught here instead of failing at evaluation time.  A group that is not an
    expression at all is left to the plain-name check.
    """
    try:
        tree = ast.parse(field.strip(), mode="eval")
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(node, ast.Name) and node.id == "channel" for node in ast.walk(tree))


def _references_channel(template):
    """Return True when *template* names ``{channel}`` in any spelling the engine can be handed.

    Covers the plain form, the conversion and format-spec forms (``{channel!r}``, ``{channel:>2}``),
    the nested-arithmetic one (``{{channel} + 1}``) and the identifier inside an arithmetic
    expression (``{channel + 1}``).  ``string.Formatter().parse()`` is not used here: it rejects the
    plugin's own arithmetic templates as malformed field names.
    """
    for field in _TEMPLATE_FIELD.findall(template):
        name = field.split("!", 1)[0].split(":", 1)[0].strip()
        if name == "channel" or name.startswith(("channel.", "channel[")):
            return True
        if _expression_names_channel(field):
            return True
    return False


def _has_unbalanced_braces(template):
    """Return True when *template*'s braces do not pair up.

    Nested groups are the plugin's arithmetic form (``{8 + ({x} - 1) * 2}``), so depth is counted
    rather than matched pairwise.
    """
    depth = 0
    for char in template:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return True
    return depth != 0


def _validate_breakout_topology(breakout_mode, channel_count, parent_name_template, applies_to_device_interfaces=False):
    """Check that the mode, the channel count and the parent template describe one topology.

    Raises ``ValidationError`` blaming the field that makes the combination impossible.  Shared by
    the model's ``clean()`` and by ``RuleTestForm`` so the tester refuses exactly what a save would.
    """
    channelized = breakout_mode == BreakoutModeChoices.CHANNELIZED
    if applies_to_device_interfaces:
        # The device-level path renames existing interfaces; it never creates a family to name.
        if channelized:
            raise ValidationError({"breakout_mode": "Device-level interface rules cannot build a channelized family."})
        if parent_name_template:
            raise ValidationError(
                {"parent_name_template": "Parent name template is not available for device-level interface rules."}
            )
    if parent_name_template:
        if not channelized:
            raise ValidationError(
                {"parent_name_template": "Parent name template requires the channelized breakout mode."}
            )
        if _has_unbalanced_braces(parent_name_template):
            # Parent template only: stray braces in name_template predate this and are already stored.
            raise ValidationError(
                {"parent_name_template": "Unbalanced braces — every '{' in the template needs a '}'."}
            )
        if _references_channel(parent_name_template):
            raise ValidationError(
                {"parent_name_template": "The parent interface has no channel number; remove {channel}."}
            )
    if channelized and not channel_count:
        raise ValidationError({"channel_count": "A channelized rule must define at least one channel."})


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
      - Regex: RE2 pattern matched against the complete ModuleType.model value

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
        help_text=(
            "RE2 pattern matched against the complete module type model name (e.g. 'QSFP-DD-400G-.*'). "
            "With Applies to Device Interfaces enabled it filters the complete interface name instead."
        ),
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
    parent_name_template = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Parent Name Template",
        help_text=(
            "Optional name template for the channelized parent interface, e.g. 'et-0/0/{bay_position}'. "
            "Same variables as the name template, minus {channel}. Blank leaves the parent's current name."
        ),
    )
    breakout_mode = models.CharField(
        max_length=20,
        choices=BreakoutModeChoices,
        default=BreakoutModeChoices.FLAT,
        verbose_name="Breakout Mode",
        help_text=(
            "Topology a breakout rule produces: 'flat' renames the base to the first channel and creates "
            "the remaining channels as sibling interfaces; 'channelized' turns the base into a channelized "
            "parent with one channel subinterface per channel (requires a NetBox that models channels)."
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
    applies_to_device_interfaces = models.BooleanField(
        default=False,
        verbose_name="Applies to Device Interfaces",
        help_text=(
            "When enabled, this rule renames device-level interfaces (module=None) when the device "
            "joins or changes position in a Virtual Chassis. "
            "The Module Type field must be empty; the Module Type Pattern (if set) is used as a regex "
            "to filter which interface names to rename."
        ),
    )

    # Override inherited tags to avoid reverse accessor clash when co-installed
    # with another plugin that has a model of the same name.
    tags = TaggableManager(through="extras.TaggedItem", related_name="+")

    def clean(self):
        """Validate regex/FK mode exclusivity and required fields."""
        super().clean()
        if self.applies_to_device_interfaces:
            # Device-level rules must not reference a module type
            if self.module_type:
                raise ValidationError({"module_type": "Module type must be empty for device-level interface rules."})
            # module_type_pattern is an optional interface-name filter regex
            if self.module_type_pattern:
                compile_module_type_pattern(self.module_type_pattern)
            # Force regex mode off — module_type_is_regex has no meaning here
            self.module_type_is_regex = False
        elif self.module_type_is_regex:
            if not self.module_type_pattern:
                raise ValidationError({"module_type_pattern": "Regex pattern is required when regex mode is enabled."})
            if self.module_type:
                raise ValidationError({"module_type": "Cannot set both module type FK and regex pattern. Choose one."})
            compile_module_type_pattern(self.module_type_pattern)
        else:
            # Clear any stale pattern so it does not persist when switching modes
            self.module_type_pattern = ""
            if not self.module_type:
                raise ValidationError({"module_type": "Module type is required when regex mode is disabled."})
        _validate_breakout_topology(
            self.breakout_mode,
            self.channel_count,
            self.parent_name_template,
            self.applies_to_device_interfaces,
        )

    def get_absolute_url(self):
        """Return the detail URL for this rule."""
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_detail", args=[self.pk])

    clone_fields = [
        "module_type",
        "module_type_pattern",
        "module_type_is_regex",
        "parent_module_type",
        "device_type",
        "platform",
        "name_template",
        "parent_name_template",
        "breakout_mode",
        "channel_count",
        "channel_start",
        "description",
        "enabled",
        "applies_to_device_interfaces",
    ]

    def get_breakout_mode_color(self):
        """Return the badge colour NetBox renders the breakout mode with."""
        return BreakoutModeChoices.colors.get(self.breakout_mode)

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
        if self.applies_to_device_interfaces:
            mode = f"iface-filter({len(self.module_type_pattern)})" if self.module_type_pattern else "iface-filter(*)"
        else:
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
                condition=(
                    models.Q(applies_to_device_interfaces=True, module_type__isnull=True)
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
            models.UniqueConstraint(
                fields=["module_type", "parent_module_type", "device_type", "platform"],
                condition=models.Q(module_type_is_regex=False, applies_to_device_interfaces=False),
                nulls_distinct=False,
                name="interfacenamerule_unique_exact",
            ),
            models.UniqueConstraint(
                fields=["module_type_pattern", "parent_module_type", "device_type", "platform"],
                condition=models.Q(module_type_is_regex=True),
                nulls_distinct=False,
                name="interfacenamerule_unique_regex",
            ),
            models.UniqueConstraint(
                fields=["module_type_pattern", "device_type", "platform"],
                condition=models.Q(applies_to_device_interfaces=True),
                nulls_distinct=False,
                name="interfacenamerule_unique_device_iface",
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

    csv_headers = [
        "module_type",
        "module_type_pattern",
        "module_type_is_regex",
        "parent_module_type",
        "device_type",
        "platform",
        "name_template",
        "parent_name_template",
        "breakout_mode",
        "channel_count",
        "channel_start",
        "description",
        "enabled",
        "applies_to_device_interfaces",
    ]

    def to_csv(self):
        """Return a tuple of field values for CSV export (matches csv_headers order)."""
        return (
            self.module_type.model if self.module_type else "",
            self.module_type_pattern,
            self.module_type_is_regex,
            self.parent_module_type.model if self.parent_module_type else "",
            self.device_type.model if self.device_type else "",
            self.platform.name if self.platform else "",
            self.name_template,
            self.parent_name_template,
            self.breakout_mode,
            self.channel_count,
            self.channel_start,
            self.description,
            self.enabled,
            self.applies_to_device_interfaces,
        )

    def to_yaml(self):
        """Return a YAML document for this rule (used by NetBox's built-in Export)."""
        import yaml

        entry = {}
        for header, value in zip(self.csv_headers, self.to_csv()):
            if (value != "" and value is not None) or header in {"name_template"}:
                entry[header] = value
        return yaml.dump([entry], default_flow_style=False, allow_unicode=True, sort_keys=False)
