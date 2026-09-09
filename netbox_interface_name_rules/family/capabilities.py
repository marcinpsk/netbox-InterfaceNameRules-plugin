# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Probe what the active NetBox data model can represent."""

from dcim.models import Interface
from django.core.exceptions import FieldDoesNotExist


def supports_channelization() -> bool:
    """Return whether this NetBox models channelized subinterfaces (4.7+).

    Probed from the Interface model rather than a version comparison, so a backport or a
    development build is detected by what it actually provides.
    """
    try:
        Interface._meta.get_field("channel_id")
    except FieldDoesNotExist:
        return False
    return True  # pragma: no cover - only reachable on a NetBox that models channelization
