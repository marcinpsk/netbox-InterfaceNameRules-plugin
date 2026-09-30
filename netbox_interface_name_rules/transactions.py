# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Atomic blocks whose NetBox events count only when the block commits."""

from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction
from netbox.context import events_queue

# The event queues that enclose the current block, outermost first.
_enclosing_queues = ContextVar("atomic_with_events_enclosing_queues", default=())


@contextmanager
def atomic_with_events(using=None):
    """Run the block in ``transaction.atomic()`` with its own NetBox event queue, kept only when the block commits.

    A block that raises, or that ``transaction.set_rollback()`` rolls back, drops the events that it queued.
    """
    enclosing = (*_enclosing_queues.get(), events_queue.get())
    enclosing_token = _enclosing_queues.set(enclosing)
    queue_token = events_queue.set({})
    outermost = transaction.get_autocommit(using=using)
    commit_marks = []
    released = False
    try:
        with transaction.atomic(using=using):
            if outermost:
                # The first commit callback: it runs after COMMIT, before a callback that can raise.
                transaction.on_commit(lambda: commit_marks.append(True), using=using)
            yield
            rollback_marked = transaction.get_rollback(using=using)
        released = not rollback_marked
    finally:
        events = events_queue.get()
        events_queue.reset(queue_token)
        _enclosing_queues.reset(enclosing_token)
        # COMMIT runs only at the exit of the outermost block, and it can fail there or in a callback after it.
        if commit_marks if outermost else released:
            _keep(events)
        else:
            _point_at_saved_rows(events, enclosing, using)


def _point_at_saved_rows(dropped, enclosing, using):
    """Point each enclosing event of an object that the rolled-back block queued at a new copy of its saved row.

    NetBox 4.5 and later serialize the queued instance at the flush, and the block can have changed it.
    """
    from core.events import OBJECT_DELETED

    for event in (queue[key] for key in dropped for queue in enclosing if key in queue):
        if event["event_type"] == OBJECT_DELETED or "object" not in event:
            continue
        model = event["object_type"].model_class()
        row = model._base_manager.using(using).filter(pk=event["object_id"]).first()
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
