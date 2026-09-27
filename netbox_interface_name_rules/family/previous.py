# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The names a module's templates had before a rename trigger, rebuilt without a database write.

A reapply after a move recognises an interface by the names its template had in the previous state:
the raw template name resolved there, and the output of the rule that state selected, built from
its template variables. Everything here evaluates values read before the save.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .targets import intended_family_names
from .template_names import ResolvedTemplateName


@dataclass(frozen=True, slots=True)
class PreviousNames:
    """What named a module's interfaces in the previous state.

    *rule* is the rule the previous state selects, or None when no rule matched there. *variables*
    are the previous template variables, and *templates* maps each interface template's primary key
    to the name it resolved to in the previous state.
    """

    rule: object | None
    variables: Mapping[str, str]
    templates: Mapping[int, ResolvedTemplateName]


def previous_rule_names(previous) -> frozenset[str]:
    """Return every name the previous rule gave the module's templates, other than a raw name.

    *previous* must hold a rule. A template the rule could not evaluate kept its raw name.
    """
    return frozenset(
        name
        for template in previous.templates.values()
        if template.channel_id is None
        for name in intended_family_names(previous.rule, previous.variables, template.resolved, template.resolved)
        if name != template.resolved
    )
