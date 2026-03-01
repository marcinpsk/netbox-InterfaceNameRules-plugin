# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Feature-detection utilities for the interface name rules plugin."""


def supports_module_path():
    """Check if the running NetBox supports the {module_path} template token.

    Detects by checking for MODULE_PATH_TOKEN in dcim.constants rather than
    comparing version strings — works with patched/pre-release builds too.
    """
    try:
        from dcim.constants import MODULE_PATH_TOKEN  # noqa: F401

        return True
    except ImportError:
        return False
