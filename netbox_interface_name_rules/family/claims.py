# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve two-sided uniqueness for complete template claim relations."""

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TemplateClaim:
    """The units one template claims in a selection scope.

    A unit is the interface names one form spells: one interface, or the members of one flat family.
    """

    claimant_id: object
    template_name: str
    units: tuple[tuple[str, ...], ...]

    def __post_init__(self):
        object.__setattr__(self, "units", tuple(tuple(dict.fromkeys(unit)) for unit in self.units))


def resolve_template_claims(claims, *, module):
    """Return accepted ``(claimant_id, unit)`` pairs in claimant order, and rendered collision messages.

    Pass the complete relation. Repeated units count once. Claimant IDs must be unique.
    A template claims one unit when exactly one of its units holds every name it claims. A template
    that claims more, or a name that more than one template claims, disqualifies every claim involved.
    The caller emits the messages through its own logger.
    """
    by_id, claimants = _index_claims(claims)
    ambiguous = set()
    messages = []
    for claimant_id, (template_name, units, names) in by_id.items():
        if units and len(_covering(units, names)) != 1:
            ambiguous.add(claimant_id)
            messages.append(
                f"Interface template {template_name!r} of {module} could name any of {sorted(names)} "
                "as its raw name or its renamed form; skipping them all rather than renaming a guess."
            )

    for name, ids in claimants.items():
        if len(ids) > 1:
            messages.append(
                f"Interface {name!r} on {module} could be the raw or renamed name of any of the templates "
                f"{sorted(by_id[claimant_id][0] for claimant_id in ids)}; "
                "skipping it rather than renaming a guess."
            )
            ambiguous.update(ids)

    accepted = tuple(
        (claimant_id, _covering(units, names)[0])
        for claimant_id, (_, units, names) in by_id.items()
        if units and claimant_id not in ambiguous
    )
    return accepted, tuple(messages)


def _covering(units, names):
    """Return the units that hold every one of *names*."""
    return [unit for unit in units if len(unit) == len(names)]


def _index_claims(claims):
    """Return each claimant's template name, distinct units and names, and the claimants of each name."""
    by_id = {}
    claimants = defaultdict(list)
    for claim in claims:
        if claim.claimant_id in by_id:
            raise ValueError(f"Duplicate claimant_id: {claim.claimant_id}")
        units = tuple(dict.fromkeys(claim.units))
        names = tuple(dict.fromkeys(name for unit in units for name in unit))
        by_id[claim.claimant_id] = (claim.template_name, units, names)
        for name in names:
            claimants[name].append(claim.claimant_id)
    return by_id, claimants
