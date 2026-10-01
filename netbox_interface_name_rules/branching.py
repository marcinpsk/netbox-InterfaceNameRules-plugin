# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The one module that uses netbox-branching. It imports netbox-branching only in a function that needs it."""

import functools
from contextvars import ContextVar

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from packaging.version import Version

APP_LABEL = "netbox_branching"
SUPPORTED_RELEASE = (1, 2)
SUPPORTED_SERIES = ".".join(map(str, SUPPORTED_RELEASE)) + ".x"
# The methods of netbox-branching's Branch that replay logged changes.
REPLAYING_METHODS = ("merge", "revert", "sync")
# The attribute that marks a method this module wrapped.
REPLAY_MARK = "marks_a_replay"

_replaying = ContextVar("netbox_interface_name_rules_replay", default=False)


def check_version(version: str) -> None:
    """Raise ``ImproperlyConfigured`` unless *version* is a release of the ``SUPPORTED_SERIES``."""
    if Version(version).release[:2] != SUPPORTED_RELEASE:
        raise ImproperlyConfigured(
            f"Interface Name Rules supports netbox-branching {SUPPORTED_SERIES} only. The installed version is {version}."
        )


def installed_version() -> str:
    """Return the version of the installed netbox-branching."""
    return apps.get_app_config(APP_LABEL).version


def ready() -> None:
    """When netbox-branching is installed, check its version and mark each of its replays in the context that runs it."""
    if not apps.is_installed(APP_LABEL):
        return
    check_version(installed_version())
    from netbox_branching.models import Branch

    for name in REPLAYING_METHODS:
        method = getattr(Branch, name)
        if not getattr(method, REPLAY_MARK, False):
            setattr(Branch, name, _marking_a_replay(method))


def _marking_a_replay(method):
    """Return *method*, wrapped so that the context that runs it is in a replay until it returns or raises."""

    @functools.wraps(method)
    def replay(*args, **kwargs):
        token = _replaying.set(True)
        try:
            return method(*args, **kwargs)
        finally:
            _replaying.reset(token)

    setattr(replay, REPLAY_MARK, True)
    return replay


def replay_in_progress() -> bool:
    """Return whether a netbox-branching merge, revert or sync runs in this context."""
    return _replaying.get()


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
