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

from dcim.models import InterfaceTemplate, Module

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
class VcTokenParts:
    """A template name resolved around its ``{vc_position}`` tokens.

    ``literals`` hold the text before, between and after the tokens, and ``fallbacks`` hold the
    explicit fallback of each token, or None.
    """

    literals: tuple[str, ...]
    fallbacks: tuple[str | None, ...]

    def resolve(self, vc_position):  # pragma: no cover - requires virtual-chassis token support
        """Return the name as NetBox resolves it at *vc_position*: else each token's fallback, else 0."""
        values = [
            str(vc_position) if vc_position is not None else ("0" if fallback is None else fallback)
            for fallback in self.fallbacks
        ]
        return self.literals[0] + "".join(
            value + literal for value, literal in zip(values, self.literals[1:], strict=True)
        )


@dataclass(frozen=True, slots=True)
class ResolvedTemplateName:
    """One template's current name, its optional historical-name matcher, and its ``VcTokenParts``."""

    pk: int
    template_name: str
    resolved: str
    historical_pattern: Pattern[str] | None
    parent_id: int | None
    channel_id: int | None
    channels: int | None
    vc_parts: VcTokenParts | None = None

    def at_chassis_position(self, vc_position):
        """Return this template resolved at *vc_position*, which None puts off a chassis."""
        if self.vc_parts is None:
            return self
        return replace(self, resolved=self.vc_parts.resolve(vc_position))


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


def _vc_parts(template, module, token_re):  # pragma: no cover - requires virtual-chassis token support
    """Return the ``VcTokenParts`` of *template* resolved against *module*, or None when its name has no token.

    NetBox resolves ``{module}`` first, and then every token in the result, also a token that a bay
    position brought in. The stub hides the template's own tokens from NetBox as markers, and puts them
    back after the ``{module}`` pass, so the split sees every token that NetBox resolves.
    """
    tokens = []

    def mark(match):
        tokens.append(match.group(0))
        return _VC_SENTINEL.format(len(tokens) - 1)

    marked = token_re.sub(mark, template.name)
    if not tokens:
        return None
    stub = copy.copy(template)
    stub.name = marked
    expanded = _VC_SENTINEL_RE.sub(lambda match: tokens[int(match.group(1))], stub.resolve_name(module))
    parts = token_re.split(expanded)
    return VcTokenParts(tuple(parts[0::2]), tuple(parts[1::2]))


def _historical_pattern(parts):  # pragma: no cover - requires virtual-chassis token support
    """Return a matcher for every resolution of the template name that *parts* describe, or None."""
    # Adjacent tokens cannot be told apart, and their alternatives would backtrack without bound.
    if any(not literal for literal in parts.literals[1:-1]):
        return None
    pattern = re.escape(parts.literals[0])
    for fallback, literal in zip(parts.fallbacks, parts.literals[1:], strict=True):
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
    parts = None if token_re is None else _vc_parts(template, module, token_re)
    return ResolvedTemplateName(
        pk=template.pk,
        template_name=template.name,
        resolved=template.resolve_name(module),
        historical_pattern=None if parts is None else _historical_pattern(parts),
        parent_id=getattr(template, "parent_id", None),
        channel_id=getattr(template, "channel_id", None),
        channels=getattr(template, "channels", None),
        vc_parts=parts,
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
