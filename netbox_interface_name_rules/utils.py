"""Version-detection utilities for the interface name rules plugin."""

import re

MODULE_PATH_MIN_VERSION = "4.9.0"


def supports_module_path():
    """Check if the running NetBox version supports the {module_path} template token."""
    from django.conf import settings

    version_str = getattr(settings, "VERSION", "0.0.0")
    version_str = version_str.split("-")[0]
    try:
        current = tuple(int(x) for x in version_str.split("."))
        required = tuple(int(x) for x in MODULE_PATH_MIN_VERSION.split("."))
        return current >= required
    except (ValueError, TypeError):
        return False
