# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Fail a test when plugin code writes an existing change-logged row without a current snapshot.

A snapshot is current when no earlier write of the same instance used it.
"""

import inspect
import weakref
from dataclasses import dataclass
from types import FrameType

from django.db.models.signals import m2m_changed, pre_save
from netbox.context import current_request

PLUGIN_PACKAGE = "netbox_interface_name_rules"
TESTS_PACKAGE = f"{PLUGIN_PACKAGE}.tests"
DISPATCH_UID = f"{TESTS_PACKAGE}.snapshot_guard"
M2M_WRITES = ("pre_add", "pre_remove", "pre_clear")
OWN_SAVE_METHODS = ("save", "save_base")
NO_SNAPSHOT = "no prechange snapshot"
EARLIER_SNAPSHOT = "the prechange snapshot of an earlier write"


class MissingSnapshotError(AssertionError):
    """Plugin code wrote an existing change-logged row without a current snapshot."""


@dataclass(frozen=True)
class Write:
    """The last write of one instance: the snapshot it used and, for a plugin write, the call that made it."""

    instance: weakref.ref
    snapshot: dict | None
    # Held so that its id() is not reused while recorded; the frame keeps the instance alive until the test ends.
    call: FrameType | None
    plugin_insert: bool
    request_id: object


_writes: dict[int, Write] = {}
_violations: list[str] = []


def _module_of(frame) -> str:
    return frame.f_globals.get("__name__", "")


def _is_django(frame) -> bool:
    module = _module_of(frame)
    return module == "django" or module.startswith("django.")


def _in_package(frame, package) -> bool:
    module = _module_of(frame)
    return module == package or module.startswith(f"{package}.")


def _is_plugin(frame) -> bool:
    return _in_package(frame, PLUGIN_PACKAGE) and not _in_package(frame, TESTS_PACKAGE)


def _is_own_write(frame, instance, via_manager) -> bool:
    """Return whether *frame* runs the instance's own save, or for an M2M change, its own related manager."""
    if frame.f_code.co_name in OWN_SAVE_METHODS:
        return frame.f_locals.get("self") is instance
    if not via_manager or _in_package(frame, PLUGIN_PACKAGE):
        return False
    return getattr(frame.f_locals.get("self"), "instance", None) is instance


def _calling_frames(instance, via_manager):
    """Return the nearest frame that called the write, and the outermost frame of the write call itself."""
    frame = inspect.currentframe()
    while frame is not None and _module_of(frame) == __name__:
        frame = frame.f_back
    call = None
    while frame is not None and (_is_django(frame) or _is_own_write(frame, instance, via_manager)):
        call = frame
        frame = frame.f_back
    return frame, call


def _problem(snapshot, last) -> str:
    """Return why *snapshot* is not current, or an empty string."""
    if snapshot is None:
        return NO_SNAPSHOT
    if last is not None and last.snapshot is snapshot:
        return EARLIER_SNAPSHOT
    return ""


def _last_write(instance):
    """Return the last recorded write of *instance*, or None."""
    last = _writes.get(id(instance))
    return last if last is not None and last.instance() is instance else None


def _check(instance, *, m2m, inserting=False):
    """Record one write of *instance*, and refuse it when plugin code makes it without a current snapshot."""
    frame, call = _calling_frames(instance, via_manager=m2m)
    last = _last_write(instance)
    snapshot = getattr(instance, "_prechange_snapshot", None)
    plugin_caller = frame is not None and _is_plugin(frame)
    same_call = call is not None and last is not None and last.call is call
    request_id = getattr(current_request.get(), "id", None)
    # NetBox merges an M2M change into the create record of the same request ID, so it needs no before-state.
    joins_insert = last is not None and last.plugin_insert and last.request_id == request_id and (m2m or same_call)
    plugin_insert = (inserting and plugin_caller) or joins_insert
    plugin_call = call if plugin_caller else None
    _writes[id(instance)] = Write(weakref.ref(instance), snapshot, plugin_call, plugin_insert, request_id)
    if not plugin_caller or same_call or plugin_insert:
        return
    problem = _problem(snapshot, last)
    if not problem:
        return
    message = (
        f"{instance._meta.label} pk={instance.pk} was written with {problem} "
        f"from {frame.f_code.co_filename}:{frame.f_lineno}. Call snapshot() before each change."
    )
    _violations.append(message)
    raise MissingSnapshotError(message)


def check_save(sender, instance, raw=False, **kwargs):
    """Check a save of a change-logged row."""
    if raw or not hasattr(instance, "snapshot"):
        return
    _check(instance, m2m=False, inserting=instance._state.adding)


def check_m2m_change(sender, instance, action, pk_set=None, **kwargs):
    """Check a many-to-many change of a change-logged row; an add or remove of nothing changes nothing."""
    if action not in M2M_WRITES or not hasattr(instance, "snapshot"):
        return
    if action != "pre_clear" and not pk_set:
        return
    _check(instance, m2m=True)


def connect() -> None:
    """Check every save and many-to-many change until ``disconnect()``."""
    pre_save.connect(check_save, dispatch_uid=DISPATCH_UID, weak=False)
    m2m_changed.connect(check_m2m_change, dispatch_uid=DISPATCH_UID, weak=False)


def disconnect() -> None:
    """Stop checking writes."""
    pre_save.disconnect(dispatch_uid=DISPATCH_UID)
    m2m_changed.disconnect(dispatch_uid=DISPATCH_UID)


def take_violations() -> list[str]:
    """Return and forget every violation recorded since the last call, and forget every recorded write."""
    taken = list(_violations)
    _violations.clear()
    _writes.clear()
    return taken
