# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The one module that uses netbox-branching. It imports netbox-branching only in a function that needs it."""

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from packaging.version import Version

APP_LABEL = "netbox_branching"
SUPPORTED_RELEASE = (1, 2)
SUPPORTED_SERIES = ".".join(map(str, SUPPORTED_RELEASE)) + ".x"


def check_version(version: str) -> None:
    """Raise ``ImproperlyConfigured`` unless *version* is a release of the ``SUPPORTED_SERIES``."""
    if Version(version).release[:2] != SUPPORTED_RELEASE:
        raise ImproperlyConfigured(
            f"Interface Name Rules supports netbox-branching {SUPPORTED_SERIES} only. The installed version is {version}."
        )


def check_installed_version() -> None:
    """Check netbox-branching when NetBox starts, if it is installed."""
    if apps.is_installed(APP_LABEL):
        check_version(apps.get_app_config(APP_LABEL).version)
