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


def branch_identity() -> str | None:
    """Return the schema ID of the active branch, or None on main and when netbox-branching is not installed."""
    if not apps.is_installed(APP_LABEL):
        return None
    from netbox_branching.contextvars import active_branch

    branch = active_branch.get()
    return None if branch is None else branch.schema_id


def activate_on(request, schema_id: str | None) -> None:
    """Put netbox-branching's branch cookie for *schema_id* on *request*; without either, the request stays on main."""
    if schema_id is None or not apps.is_installed(APP_LABEL):
        return
    from netbox_branching.constants import COOKIE_NAME

    request.COOKIES[COOKIE_NAME] = schema_id
