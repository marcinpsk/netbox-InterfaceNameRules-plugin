# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Choice sets for InterfaceNameRule."""

from utilities.choices import ChoiceSet


class BreakoutModeChoices(ChoiceSet):
    """The interface topology a breakout rule produces.

    Values name the topology itself, never its relation to a release: a future NetBox model
    gets its own value instead of relabelling the existing ones.
    """

    FLAT = "flat"
    CHANNELIZED = "channelized"

    CHOICES = [
        (FLAT, "Flat", "gray"),
        (CHANNELIZED, "Channelized", "blue"),
    ]
