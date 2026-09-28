# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The names a module's templates had before a move, rebuilt without a database write.

A reapply after a move recognises an interface by the names its template had in the previous state:
the raw template name resolved there, and the output of the rule that state selected, built from
its template variables. Everything here evaluates values read before the save.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .installed import device_interface_families
from .targets import intended_family_names
from .template_names import ResolvedTemplateName


@dataclass(frozen=True, slots=True)
class PreviousForms:
    """What rebuilds the names a module's interfaces had in the previous state.

    *rule* is the rule the previous state selects, or None when no rule matched there. *variables*
    are the previous template variables, and *templates* maps each interface template's primary key
    to the name it resolved to in the previous state.
    """

    rule: object | None
    variables: Mapping[str, str]
    templates: Mapping[int, ResolvedTemplateName]


def _names_given_by(previous_forms) -> frozenset[str]:
    """Return every name the previous rule gave the module's templates, other than a raw name."""
    return frozenset(
        name
        for template in previous_forms.templates.values()
        if template.channel_id is None
        for name in intended_family_names(
            previous_forms.rule, previous_forms.variables, template.resolved, template.resolved
        )
        if name != template.resolved
    )


def names_the_previous_rule_gave(previous_forms, interfaces) -> tuple[str, ...]:
    """Return the name of each of *interfaces* that the previous rule gave, in interface order.

    *previous_forms* must hold a rule. A template the rule could not evaluate kept its raw name. A
    channel that a simple rule renamed in lockstep with its parent follows the parent.
    """
    given = _names_given_by(previous_forms)
    names = []
    for interface, channels in device_interface_families(interfaces):
        parent_named = interface.name in given
        names.extend(row.name for row in (interface, *channels) if parent_named or row.name in given)
    return tuple(names)
