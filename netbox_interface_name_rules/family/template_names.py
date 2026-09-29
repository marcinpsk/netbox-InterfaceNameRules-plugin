# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve current and historical NetBox interface-template names.

The refetch and template queries here use the default manager, and the block cache is keyed by
primary key alone. Both hold because NetBox configures one database alias and no router.
"""

import contextlib
import copy
import re
import threading
from dataclasses import dataclass, replace
from re import Pattern

from dcim.models import InterfaceTemplate, Module, VirtualChassis

from ..rule_selection import compile_stored_pattern

BAY_CHAIN_RELATIONS = (
    "device",
    "device__virtual_chassis",
    "module_bay",
    "module_bay__parent",
    "module_bay__module",
    "module_bay__module__module_bay",
    "module_bay__module__module_bay__parent",
    "module_bay__module__module_bay__module",
)

# PostgreSQL text cannot hold NUL, so no stored template name or bay position can spell this marker.
_VC_SENTINEL = "\x00{}\x00"
_VC_SENTINEL_RE = re.compile(r"\x00(\d+)\x00")
# NetBox stores vc_position in a PositiveIntegerField, so ten digits cover every valid value.
VC_POSITION_DIGITS = r"\d{1,10}"


@dataclass(frozen=True, slots=True)
class ResolvedTemplateName:
    """One template's current name, its optional historical-name matcher, and what NetBox resolved it from.

    ``source`` holds the template and the module that NetBox resolved the name against. It is None
    on a NetBox without the ``{vc_position}`` token, because the name is then the same at every position.
    """

    pk: int
    template_name: str
    resolved: str
    historical_pattern: Pattern[str] | None
    parent_id: int | None
    channel_id: int | None
    channels: int | None
    source: tuple | None = None

    def at_chassis_position(self, vc_position, *, move=False):
        """Return this template as NetBox names it at *vc_position*, which None puts off a chassis.

        NetBox names an installed component with ``resolve_name``, and a moved one with the resolver of
        its move planner; *move* selects that one.
        """
        if self.source is None:
            return self
        template, module = self.source
        stand_in = _module_at_chassis_position(module, vc_position)
        return replace(self, resolved=_move_name(template, stand_in) if move else template.resolve_name(stand_in))


def _move_name(template, module):  # pragma: no cover - requires a NetBox that renames moved components
    """Return the name that NetBox's move planner gives *template* on *module* in its bay.

    Unlike ``resolve_name``, the planner resolves ``{vc_position}`` also when only a bay position brings
    the token in. Positions that do not fit the template raise here as they do in ``resolve_name``.
    """
    from dcim.models.module_moves import ModuleMovePlan
    from dcim.utils import get_module_bay_positions

    positions = get_module_bay_positions(module.module_bay)
    return ModuleMovePlan._resolve(template, template.name, positions, module.device)


def _module_at_chassis_position(module, vc_position):  # pragma: no cover - requires virtual-chassis token support
    """Return a copy of *module* whose device is a copy at *vc_position*; None puts that device off a chassis.

    NetBox reads only whether the device has a virtual chassis, and its position. The copies share the
    bay chain that *module* already loaded, change nothing on *module* or its device, and are not saved.
    """
    device = copy.copy(module.device)
    device.virtual_chassis = None if vc_position is None else VirtualChassis()
    device.vc_position = vc_position
    stand_in = copy.copy(module)
    stand_in.device = device
    return stand_in


def vc_position_re():
    """Return NetBox's virtual-chassis template-token pattern when available."""
    try:
        from dcim.constants import VC_POSITION_RE
    except ImportError:
        return None
    return VC_POSITION_RE  # pragma: no cover - only available on NetBox releases with the token


def _vc_position_alternatives(fallback):  # pragma: no cover - requires virtual-chassis token support
    """Return every value represented by one virtual-chassis position token."""
    if fallback is None:
        return VC_POSITION_DIGITS
    return f"(?:{VC_POSITION_DIGITS}|{re.escape(fallback)})"


def _historical_pattern(template, module, token_re):  # pragma: no cover - requires virtual-chassis token support
    """Return a matcher for every historical resolution of *template*."""
    fallbacks = []

    def mark(match):
        fallbacks.append(match.group(1))
        return _VC_SENTINEL.format(len(fallbacks) - 1)

    marked = token_re.sub(mark, template.name)
    if not fallbacks:
        return None
    stub = copy.copy(template)
    stub.name = marked
    literals = _VC_SENTINEL_RE.split(stub.resolve_name(module))[0::2]
    # Adjacent tokens cannot be told apart, and their alternatives would backtrack without bound.
    if any(not literal for literal in literals[1:-1]):
        return None
    pattern = re.escape(literals[0])
    for fallback, literal in zip(fallbacks, literals[1:], strict=True):
        pattern += _vc_position_alternatives(fallback) + re.escape(literal)
    return compile_stored_pattern(pattern)


# One batch of modules shares its module chains, template rows and resolved names, thread-locally.
_pin = threading.local()


@contextlib.contextmanager
def pinned_template_cache(modules=()):
    """Share resolved interface templates across every module in one batch.

    Each module in *modules* must already carry ``BAY_CHAIN_RELATIONS``, so its templates resolve
    without a refetch. Modules the block meets later are chained and cached on first use, and one
    module type's template rows are read once. Nested blocks share the outermost cache.
    """
    depth = getattr(_pin, "depth", 0)
    _pin.depth = depth + 1
    try:
        # Setting up the cache must sit inside the try, or a raise here strands the depth forever.
        if depth == 0:
            _pin.chained = {}
            _pin.templates = {}
            _pin.resolved = {}
        _pin.chained.update({module.pk: module for module in modules})
        yield
    finally:
        _pin.depth -= 1
        if _pin.depth == 0:
            for attr in ("chained", "templates", "resolved"):
                _pin.__dict__.pop(attr, None)


def module_with_bay_chain(module):
    """Re-fetch *module* with every relation template name resolution uses."""
    chained = getattr(_pin, "chained", None)
    if chained is None:
        return Module.objects.select_related(*BAY_CHAIN_RELATIONS).get(pk=module.pk)
    if module.pk not in chained:
        chained[module.pk] = Module.objects.select_related(*BAY_CHAIN_RELATIONS).get(pk=module.pk)
    return chained[module.pk]


def _interface_templates(module_type_id):
    """Return one module type's interface templates in primary-key order."""
    templates = getattr(_pin, "templates", None)
    if templates is None:
        return list(InterfaceTemplate.objects.filter(module_type_id=module_type_id).order_by("pk"))
    if module_type_id not in templates:
        templates[module_type_id] = list(InterfaceTemplate.objects.filter(module_type_id=module_type_id).order_by("pk"))
    return templates[module_type_id]


def _resolve_template(template, module, token_re) -> ResolvedTemplateName:
    """Resolve one interface template against *module*."""
    return ResolvedTemplateName(
        pk=template.pk,
        template_name=template.name,
        resolved=template.resolve_name(module),
        historical_pattern=None if token_re is None else _historical_pattern(template, module, token_re),
        parent_id=getattr(template, "parent_id", None),
        channel_id=getattr(template, "channel_id", None),
        channels=getattr(template, "channels", None),
        source=None if token_re is None else (template, module),
    )


def resolve_templates(templates, module) -> tuple[ResolvedTemplateName, ...]:
    """Resolve already-loaded interface templates against *module*."""
    token_re = vc_position_re()
    return tuple(_resolve_template(template, module, token_re) for template in templates)


def resolved_template_names(module) -> tuple[ResolvedTemplateName, ...]:
    """Load and resolve every interface template for *module* once."""
    resolved = getattr(_pin, "resolved", None)
    if resolved is not None and module.pk in resolved:
        return resolved[module.pk]
    chained = module_with_bay_chain(module)
    names = resolve_templates(_interface_templates(chained.module_type_id), chained)
    if resolved is not None:
        resolved[module.pk] = names
    return names


class TemplateNames:
    """The module type's resolved template names, read only where a plan needs them."""

    def __init__(self, module):
        self._module = module
        self._templates = None

    def get(self):
        """Return every resolved template name for the module, loading them once."""
        if self._templates is None:
            self._templates = resolved_template_names(self._module)
        return self._templates
