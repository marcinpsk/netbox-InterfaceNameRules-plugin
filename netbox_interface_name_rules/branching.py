# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The one module that uses netbox-branching. The plugin loads it only when netbox-branching is installed."""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from packaging.version import Version

SUPPORTED_RELEASE = (1, 2)
SUPPORTED_SERIES = ".".join(map(str, SUPPORTED_RELEASE)) + ".x"


def check_version(version: str) -> None:
    """Raise ``ImproperlyConfigured`` unless *version* is a release of the ``SUPPORTED_SERIES``."""
    if Version(version).release[:2] != SUPPORTED_RELEASE:
        raise ImproperlyConfigured(
            f"Interface Name Rules supports netbox-branching {SUPPORTED_SERIES} only. The installed version is {version}."
        )


def check_installed_version() -> None:
    """Check the installed netbox-branching when NetBox starts."""
    check_version(apps.get_app_config("netbox_branching").version)
