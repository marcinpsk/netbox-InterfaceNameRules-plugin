# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Atomic blocks whose NetBox events count only when the block commits."""

from contextlib import contextmanager

from django.db import transaction
from netbox.context import events_queue


@contextmanager
def atomic_with_events(using=None):
    """Run the block in ``transaction.atomic()`` with its own NetBox event queue, kept only when the block commits.

    A block that raises, or that ``transaction.set_rollback()`` rolls back, drops the events that it queued.
    """
    token = events_queue.set({})
    committed = False
    try:
        with transaction.atomic(using=using):
            yield
            committed = not transaction.get_rollback(using=using)
    finally:
        events = events_queue.get()
        events_queue.reset(token)
        if not committed:
            _restore_payloads(events)
    if committed:
        _keep(events)


def _restore_payloads(dropped):
    """Reload the object of each enclosing event that the rolled-back block queued again, as the database holds it.

    NetBox 4.5 and later serialize a queued object at the flush, and the block can have changed that same instance.
    """
    from core.events import OBJECT_DELETED

    queue = events_queue.get()
    for key in dropped:
        earlier = queue.get(key)
        instance = None if earlier is None or earlier["event_type"] == OBJECT_DELETED else earlier.get("object")
        if instance is None:
            continue
        instance.refresh_from_db()
        # NetBox 4.7 serializes the latest instance that queued the object, which can be another one.
        if hasattr(earlier, "refresh_serialization_source"):
            earlier.refresh_serialization_source(instance)


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
