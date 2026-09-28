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

    by_id, claimants = _index_claims(claims)
    ambiguous = set()
    messages = []
    cause = "as its raw name or its renamed form" if label_kind == _RAW_BASE else _DRIFT_CAUSE
    for claimant_id, (template_name, labels) in by_id.items():
        if len(labels) > 1:
            ambiguous.add(claimant_id)
            messages.append(
                f"Interface template {template_name!r} of {module} could name any of {sorted(labels)} "
                f"{cause}; skipping them all rather than renaming a guess."
            )

    for label, ids in claimants.items():
        if len(ids) > 1:
            messages.append(_shared_label_message(label, ids, by_id, module, label_kind))
            ambiguous.update(ids)

    accepted = tuple(
        (claimant_id, labels[0])
        for claimant_id, (_, labels) in by_id.items()
        if labels and claimant_id not in ambiguous
    )
    return accepted, tuple(messages)


def _shared_label_message(label, ids, by_id, module, label_kind):
    """Return the message for a label that the templates *ids* all claim."""
    subject = "Family base" if label_kind == "family base" else "Interface"
    form = "raw or renamed name" if label_kind == _RAW_BASE else "drifted name"
    return (
        f"{subject} {label!r} on {module} could be the {form} of any of the templates "
        f"{sorted(by_id[claimant_id][0] for claimant_id in ids)}; "
        "skipping it rather than renaming a guess."
    )


def resolve_group_claims(claims, *, module):
    """Return accepted pairs and messages for claims whose labels are groups of interface names.

    A group is one interface, or every member of one interface family. A template's group is
    accepted only when the template claims no other group and no other template claims any name
    in it, in any group: groups that share a name are one ambiguity, as equal labels are.
    """
    accepted, messages = resolve_template_claims(claims, module=module, label_kind=_RAW_BASE)
    by_id, claimants_by_group = _index_claims(claims)
    claimants_by_name = defaultdict(set)
    for group, ids in claimants_by_group.items():
        for name in group:
            claimants_by_name[name].update(ids)
    shared = {name: ids for name, ids in claimants_by_name.items() if len(ids) > 1}
    # A group that several templates claim whole was reported above; a name shared across groups was not.
    reported = {name for group, ids in claimants_by_group.items() if len(ids) > 1 for name in group}
    messages += tuple(
        _shared_label_message(name, ids, by_id, module, _RAW_BASE)
        for name, ids in shared.items()
        if name not in reported
    )
    return tuple((claimant_id, group) for claimant_id, group in accepted if shared.keys().isdisjoint(group)), messages


def _index_claims(claims):
    """Return each claimant's template name and distinct labels, and the claimants of each label."""
    by_id = {}
    claimants = defaultdict(list)
    for claim in claims:
        if claim.claimant_id in by_id:
            raise ValueError(f"Duplicate claimant_id: {claim.claimant_id}")
        labels = tuple(dict.fromkeys(claim.labels))
        by_id[claim.claimant_id] = (claim.template_name, labels)
        for label in labels:
            claimants[label].append(claim.claimant_id)
    return by_id, claimants
