# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Outcome facts of a reapply: what happened to each interface that a matching rule acted on.

An interface that already has its correct name, or that the rule does not claim, has no outcome.
"""

from dataclasses import dataclass
from enum import StrEnum


class OutcomeKind(StrEnum):
    """What a reapply did to one interface."""

    RENAMED = "renamed"
    BLOCKED = "blocked"
    UNRESOLVED_VARIABLE = "unresolved variable"
    UNCLAIMED = "unclaimed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RenameOutcome:
    """One outcome fact. *interface_name* is None for a failure that stopped the whole reapply."""

    kind: OutcomeKind
    interface_name: str | None
    reason: str = ""
    target_name: str | None = None


def renamed_count(outcomes) -> int:
    """Return the number of interfaces that *outcomes* renamed or created."""
    return sum(outcome.kind == OutcomeKind.RENAMED for outcome in outcomes)
