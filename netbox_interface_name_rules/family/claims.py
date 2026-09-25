# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve two-sided uniqueness for complete template claim relations."""

from collections import defaultdict
from dataclasses import dataclass

_DRIFT_CAUSE = "since this device's virtual-chassis position changed"
_RAW_BASE = "raw base"


@dataclass(frozen=True, slots=True)
class TemplateClaim:
    """Labels claimed by one template in a selection scope."""

    claimant_id: int
    template_name: str
    labels: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "labels", tuple(self.labels))


def resolve_template_claims(claims, *, module, label_kind):
    """Return accepted pairs in claimant order and rendered collision messages.

    Pass the complete relation, including templates with multiple labels.
    Repeated edges count once. Claimant IDs must be unique.
    The caller emits the messages through its own logger.
    """
    if label_kind not in ("interface name", "family base", _RAW_BASE):
        raise ValueError(f"label_kind must be 'interface name', 'family base' or '{_RAW_BASE}'")

    by_id = {}
    claimants = defaultdict(list)
    ambiguous = set()
    messages = []
    for claim in claims:
        if claim.claimant_id in by_id:
            raise ValueError(f"Duplicate claimant_id: {claim.claimant_id}")
        labels = tuple(dict.fromkeys(claim.labels))
        by_id[claim.claimant_id] = (claim.template_name, labels)
        for label in labels:
            claimants[label].append(claim.claimant_id)
        if len(labels) > 1:
            ambiguous.add(claim.claimant_id)
            cause = "as its raw name or its renamed form" if label_kind == _RAW_BASE else _DRIFT_CAUSE
            messages.append(
                f"Interface template {claim.template_name!r} of {module} could name any of {sorted(labels)} "
                f"{cause}; skipping them all rather than renaming a guess."
            )

    subject = "Family base" if label_kind == "family base" else "Interface"
    form = "raw or renamed name" if label_kind == _RAW_BASE else "drifted name"
    for label, ids in claimants.items():
        if len(ids) > 1:
            messages.append(
                f"{subject} {label!r} on {module} could be the {form} of any of the templates "
                f"{sorted(by_id[claimant_id][0] for claimant_id in ids)}; "
                "skipping it rather than renaming a guess."
            )
            ambiguous.update(ids)

    accepted = tuple(
        (claimant_id, labels[0])
        for claimant_id, (_, labels) in by_id.items()
        if labels and claimant_id not in ambiguous
    )
    return accepted, tuple(messages)
