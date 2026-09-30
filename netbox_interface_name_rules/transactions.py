# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The one owner of the plugin's database connections and transaction state.

Every plugin write runs in a write scope. On main the scope holds ``default``. In a netbox-branching
branch it holds ``default`` and the branch alias: netbox-branching writes the ChangeDiff rows of a
branch change on ``default``, so a block opens both, ``default`` outside the branch.
"""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial

from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, router, transaction
from netbox.context import events_queue

# PostgreSQL does not detect a lock cycle through the two sessions of one request in a branch.
LOCK_TIMEOUT = "10s"
READ_LOCK_TIMEOUT = "SHOW lock_timeout"
# Sets the session value, which the setup and each restore use.
SET_LOCK_TIMEOUT = "SELECT set_config('lock_timeout', %s, false)"

# The aliases of the open write scope, or None outside one.
_scope_aliases = ContextVar("write_scope_aliases", default=None)
# The event queues that enclose the current block, outermost first.
_enclosing_queues = ContextVar("atomic_with_events_enclosing_queues", default=())


def write_alias() -> str:
    """Return the alias that NetBox's router gives for a write of an interface."""
    from dcim.models import Interface

    return router.db_for_write(Interface)


@contextmanager
def write_scope(expected_alias=None):
    """Pin the aliases of one plugin operation and yield them: ``default``, then the branch alias in a branch.

    It raises before any query when the write alias is not *expected_alias*. A nested scope joins the
    open one, and raises when its write alias differs. In a branch the outermost scope sets
    ``lock_timeout`` on both connections and restores each value when it exits; on main it runs no query.
    """
    alias = write_alias()
    if expected_alias is not None and alias != expected_alias:
        raise RuntimeError(f"The write alias is {alias!r}, but the operation expects {expected_alias!r}.")
    enclosing = _scope_aliases.get()
    if enclosing is not None:
        if alias != enclosing[-1]:
            raise RuntimeError(f"The write alias is {alias!r}, but the open write scope writes to {enclosing[-1]!r}.")
        yield enclosing
        return
    aliases = (DEFAULT_DB_ALIAS,) if alias == DEFAULT_DB_ALIAS else (DEFAULT_DB_ALIAS, alias)
    token = _scope_aliases.set(aliases)
    try:
        if len(aliases) == 1:
            yield aliases
        else:
            with _lock_timeout(aliases):
                yield aliases
    finally:
        _scope_aliases.reset(token)


@contextmanager
def _lock_timeout(aliases):
    """Set ``lock_timeout`` on the connection of each of *aliases*, and restore each value when the block exits.

    A caller's open transaction holds both the set and the restore, so either outcome of it keeps the
    value from before it. A setup that fails restores the connections it changed.
    """
    previous = {}
    try:
        for alias in aliases:
            connection = connections[alias]
            with connection.cursor() as cursor:
                cursor.execute(READ_LOCK_TIMEOUT)
                (value,) = cursor.fetchone()
                cursor.execute(SET_LOCK_TIMEOUT, [LOCK_TIMEOUT])
            previous[alias] = value
        yield
    finally:
        _restore_lock_timeouts(previous)


def _restore_lock_timeouts(previous):
    """Restore each connection's ``lock_timeout`` from *previous*; discard each connection whose restore fails."""
    failures = []
    for alias, value in previous.items():
        connection = connections[alias]
        try:
            with connection.cursor() as cursor:
                cursor.execute(SET_LOCK_TIMEOUT, [value])
        except DatabaseError as error:
            _discard(connection)
            failures.append((alias, error))
    if failures:
        names = ", ".join(repr(alias) for alias, _ in failures)
        raise RuntimeError(f"Could not restore lock_timeout on {names}; the connection is closed.") from failures[0][1]


def _discard(connection):
    """Close the session of *connection*, so that no pool can hand out its changed setting."""
    session = connection.connection
    try:
        if session is not None:
            session.close()
    finally:
        connection.close()


class _CommitJoin:
    """Run a callback once, when every alias of the scope acknowledged the commit of its transaction."""

    def __init__(self, callback, aliases):
        self._callback = callback
        # The whole set is pending before the first registration: a connection in autocommit acknowledges at once.
        self._pending = set(aliases)
        self._fired = False

    def acknowledge(self, alias):
        self._pending.discard(alias)
        if self._pending or self._fired:
            return
        self._fired = True
        self._callback()


def on_commit(callback):
    """Run *callback* once, after the transactions open at the call commit on every alias of the write scope.

    Each alias acknowledges through Django's ``on_commit``, which drops the acknowledgement when its
    transaction, or a savepoint around the call, rolls back; *callback* then never runs.
    """
    aliases = _scope_aliases.get()
    if aliases is None:
        raise RuntimeError("on_commit() runs only inside a write scope.")
    if len(aliases) == 1:
        transaction.on_commit(callback, using=aliases[0])
        return
    join = _CommitJoin(callback, aliases)
    for alias in aliases:
        transaction.on_commit(partial(join.acknowledge, alias), using=alias)


@dataclass(frozen=True)
class Block:
    """An open ``atomic_with_events()`` block, with one atomic block on each alias of its write scope."""

    aliases: tuple[str, ...]

    def set_rollback(self):
        """Mark the block for rollback on every alias."""
        for alias in self.aliases:
            transaction.set_rollback(True, using=alias)


@contextmanager
def atomic_with_events():
    """Run the block atomically on every alias of the write scope, with its own NetBox event queue.

    It joins the open write scope or opens one. It opens ``default`` first and the branch alias inside
    it, so the branch commits first. The block keeps its queued events when the branch block commits
    (outermost) or is released (nested) with no alias marked for rollback, and drops them otherwise.
    """
    with write_scope() as aliases:
        enclosing = (*_enclosing_queues.get(), events_queue.get())
        enclosing_token = _enclosing_queues.set(enclosing)
        queue_token = events_queue.set({})
        write = aliases[-1]
        outermost = transaction.get_autocommit(using=write)
        commit_marks = []
        released = False
        try:
            with ExitStack() as blocks:
                for alias in aliases:
                    blocks.enter_context(transaction.atomic(using=alias))
                if outermost:
                    # The first commit callback of the write connection: it runs after COMMIT, before a callback can raise.
                    transaction.on_commit(lambda: commit_marks.append(True), using=write)
                yield Block(aliases)
                rollback_marked = any(transaction.get_rollback(using=alias) for alias in aliases)
            released = not rollback_marked
        finally:
            events = events_queue.get()
            events_queue.reset(queue_token)
            _enclosing_queues.reset(enclosing_token)
            # COMMIT runs only at the exit of the outermost block, and it can fail there or in a callback after it.
            if commit_marks if outermost else released:
                _keep(events)
            else:
                _point_at_saved_rows(events, enclosing)


def _point_at_saved_rows(dropped, enclosing):
    """Point each enclosing event of an object that the rolled-back block queued at a new copy of its saved row.

    NetBox 4.5 and later serialize the queued instance at the flush, and the block can have changed it.
    """
    from core.events import OBJECT_DELETED

    for event in (queue[key] for key in dropped for queue in enclosing if key in queue):
        if event["event_type"] == OBJECT_DELETED or "object" not in event:
            continue
        model = event["object_type"].model_class()
        row = model._base_manager.filter(pk=event["object_id"]).first()
        if row is None:
            raise RuntimeError(
                f"{model._meta.label} {event['object_id']} has a queued event but no row after a rollback"
            )
        if hasattr(event, "refresh_serialization_source"):
            event.refresh_serialization_source(row)
        else:
            event["object"] = row
            if "data" in event:
                del event["data"]


def _keep(events):
    """Add the events of a committed block to the enclosing queue, coalesced as NetBox's enqueue_event() does."""
    if not events:
        return
    from core.events import OBJECT_DELETED

    queue = events_queue.get()
    for key, event in events.items():
        earlier = queue.get(key)
        if earlier is not None:
            # The object keeps its first before-state and event type, unless the block deleted it.
            event["snapshots"]["prechange"] = earlier["snapshots"]["prechange"]
            if event["event_type"] != OBJECT_DELETED:
                event["event_type"] = earlier["event_type"]
        queue[key] = event
    events_queue.set(queue)
