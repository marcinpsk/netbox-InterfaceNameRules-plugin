# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve current and historical NetBox interface-template names."""

import contextlib
import copy
import re
import threading
from collections import namedtuple
from dataclasses import dataclass
from re import Pattern

from dcim.models import InterfaceTemplate, Module

from ..rule_selection import _compile_pattern

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

_VC_SENTINEL = "InrVcPositionSentinel{}End"
_VC_SENTINEL_RE = re.compile(r"InrVcPositionSentinel(\d+)End")
# NetBox stores vc_position in a PositiveIntegerField, so ten digits cover every valid value.
_VC_POSITION_DIGITS = r"\d{1,10}"

RawMatcher = namedtuple("RawMatcher", ("template_name", "resolved", "pattern"))
RawNames = namedtuple("RawNames", ("names", "matchers"))


@dataclass(frozen=True, slots=True)
class ResolvedTemplateName:
    """One template's current name and optional historical-name matcher."""

    pk: int
    template_name: str
    resolved: str
    historical_pattern: Pattern[str] | None
    parent_id: int | None
    channel_id: int | None
    channels: int | None


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
        return _VC_POSITION_DIGITS
    return f"(?:{_VC_POSITION_DIGITS}|{re.escape(fallback)})"


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
    parts = _VC_SENTINEL_RE.split(re.escape(stub.resolve_name(module)))
    literals = parts[0::2]
    indexes = parts[1::2]
    # A sentinel-shaped literal in the template name would shift these indexes; refuse to guess.
    if indexes != [str(index) for index in range(len(fallbacks))]:
        return None
    # Adjacent tokens cannot be told apart, and their alternatives would backtrack without bound.
    if any(not literal for literal in literals[1:-1]):
        return None
    pattern = literals[0]
    for index, literal in zip(indexes, literals[1:], strict=True):
        pattern += _vc_position_alternatives(fallbacks[int(index)]) + literal
    return _compile_pattern(pattern)


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
    if depth == 0:
        _pin.chained = {}
        _pin.templates = {}
        _pin.resolved = {}
    _pin.chained.update({module.pk: module for module in modules})
    try:
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


def resolve_templates(templates, module) -> tuple[ResolvedTemplateName, ...]:
    """Resolve already-loaded interface templates against *module*."""
    token_re = vc_position_re()
    return tuple(
        ResolvedTemplateName(
            pk=template.pk,
            template_name=template.name,
            resolved=template.resolve_name(module),
            historical_pattern=None if token_re is None else _historical_pattern(template, module, token_re),
            parent_id=getattr(template, "parent_id", None),
            channel_id=getattr(template, "channel_id", None),
            channels=getattr(template, "channels", None),
        )
        for template in templates
    )


def raw_names_from(templates) -> RawNames:
    """Return the current names and historical matchers of already-resolved templates."""
    matchers = [
        RawMatcher(template.template_name, template.resolved, template.historical_pattern)
        for template in templates
        if template.historical_pattern is not None  # pragma: no cover - token templates only
    ]
    return RawNames({template.resolved for template in templates}, matchers)


def raw_name_matchers(module):
    """Return current and historical raw template names for *module*."""
    return raw_names_from(resolved_template_names(module))


def raw_name_patterns(module):
    """Return historical matchers for the module's token templates."""
    return [matcher.pattern for matcher in raw_name_matchers(module).matchers]


def raw_names_by_module(modules):  # pragma: no cover - only the channel conversion scan batches names
    """Resolve raw names for a prefetched module batch with one template query."""
    by_module_type = {}
    for template in InterfaceTemplate.objects.filter(module_type_id__in={module.module_type_id for module in modules}):
        by_module_type.setdefault(template.module_type_id, []).append(template)
    return {
        module.pk: raw_names_from(resolve_templates(by_module_type.get(module.module_type_id, ()), module))
        for module in modules
    }


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
