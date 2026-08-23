# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve current and historical NetBox interface-template names."""

import copy
import re
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
    return VC_POSITION_RE


def _vc_position_alternatives(fallback):  # pragma: no cover - requires virtual-chassis token support
    """Return every value represented by one virtual-chassis position token."""
    if fallback is None:
        return r"\d+"
    return f"(?:\\d+|{re.escape(fallback)})"


def _historical_pattern(template, module, token_re):
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
    pattern = re.escape(stub.resolve_name(module))
    for index, fallback in enumerate(fallbacks):
        pattern = pattern.replace(_VC_SENTINEL.format(index), _vc_position_alternatives(fallback))
    return _compile_pattern(pattern)


def module_with_bay_chain(module):
    """Re-fetch *module* with every relation template name resolution uses."""
    return Module.objects.select_related(*BAY_CHAIN_RELATIONS).get(pk=module.pk)


def raw_matchers(templates, module):
    """Resolve templates into current names and historical token matchers."""
    token_re = vc_position_re()
    names = set()
    matchers = []
    for template in templates:
        resolved = template.resolve_name(module)
        names.add(resolved)
        pattern = None if token_re is None else _historical_pattern(template, module, token_re)
        if pattern is not None:
            matchers.append(RawMatcher(template.name, resolved, pattern))
    return RawNames(names, matchers)


def raw_name_matchers(module):
    """Return current and historical raw template names for *module*."""
    module = module_with_bay_chain(module)
    templates = InterfaceTemplate.objects.filter(module_type_id=module.module_type_id)
    return raw_matchers(templates, module)


def raw_name_patterns(module):
    """Return historical matchers for the module's token templates."""
    return [matcher.pattern for matcher in raw_name_matchers(module).matchers]


def raw_names_by_module(modules):
    """Resolve raw names for a prefetched module batch with one template query."""
    by_module_type = {}
    for template in InterfaceTemplate.objects.filter(module_type_id__in={module.module_type_id for module in modules}):
        by_module_type.setdefault(template.module_type_id, []).append(template)
    return {module.pk: raw_matchers(by_module_type.get(module.module_type_id, ()), module) for module in modules}


def resolved_template_names(module) -> tuple[ResolvedTemplateName, ...]:
    """Load and resolve every interface template for *module* once."""
    module = module_with_bay_chain(module)
    token_re = vc_position_re()
    templates = InterfaceTemplate.objects.filter(module_type_id=module.module_type_id).order_by("pk")
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
