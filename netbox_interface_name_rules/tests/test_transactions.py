# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""An atomic block keeps the NetBox events it queued only when it commits, coalesced as NetBox does."""

import contextvars
import uuid
from contextlib import contextmanager
from itertools import product
from unittest import skipUnless

from core.events import OBJECT_CREATED, OBJECT_DELETED, OBJECT_UPDATED
from dcim.models import Interface, Site
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.http import HttpRequest
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from extras import events as netbox_events
from extras.events import serialize_for_event
from extras.models import Tag
from netbox.context import current_request, events_queue

from netbox_interface_name_rules.jobs import run_as_job_user
from netbox_interface_name_rules.tests.helpers import (
    empty_the_webhook_queue,
    make_device,
    make_device_type,
    make_interface_webhook_rule,
    make_job,
    make_manufacturer,
    queued_webhook_jobs,
    queued_webhooks,
)
from netbox_interface_name_rules.transactions import atomic_with_events


def _describe(interface, description):
    interface.snapshot()
    interface.description = description
    interface.save()


class AtomicWithEventsTest(TestCase):
    """Each test runs as a job request, and reads the webhooks that the flush at its end queued."""

    @classmethod
    def setUpTestData(cls):
        cls.device = make_device("TxEvents", make_device_type(make_manufacturer("TxEvents"), "TxEvents"))
        cls.interface = Interface.objects.create(device=cls.device, name="eth0", type="1000base-t")
        cls.event_rule = make_interface_webhook_rule("TxEvents")

    def setUp(self):
        empty_the_webhook_queue(self)

    def _run_as_a_request(self, body):
        # django-rq enqueues a webhook when the transaction commits.
        with self.captureOnCommitCallbacks(execute=True):
            run_as_job_user(make_job("TxEvents"), body)

    def test_a_change_before_and_inside_a_committed_block_is_one_event(self):
        def body():
            _describe(self.interface, "before the block")
            with atomic_with_events():
                _describe(self.interface, "inside the block")

        self._run_as_a_request(body)

        [webhook] = queued_webhook_jobs(self.event_rule)
        snapshots = webhook.kwargs["snapshots"]
        self.assertEqual(webhook.kwargs["event_type"], OBJECT_UPDATED)
        self.assertEqual(
            (snapshots["prechange"]["description"], snapshots["postchange"]["description"]), ("", "inside the block")
        )
        self.assertEqual(webhook.kwargs["data"]["description"], "inside the block")

    def test_a_delete_inside_a_committed_block_replaces_the_event_type(self):
        def body():
            _describe(self.interface, "before the block")
            with atomic_with_events():
                self.interface.delete()

        self._run_as_a_request(body)

        self.assertEqual(queued_webhooks(self.event_rule), [(OBJECT_DELETED, "eth0")])

    def test_a_block_that_raises_drops_only_its_own_events(self):
        def change_and_fail():
            with atomic_with_events():
                _describe(self.interface, "inside the block")
                raise RuntimeError("roll the block back")

        def body():
            _describe(self.interface, "before the block")
            with self.assertRaises(RuntimeError):
                change_and_fail()

        self._run_as_a_request(body)

        [webhook] = queued_webhook_jobs(self.event_rule)
        self.assertEqual(webhook.kwargs["snapshots"]["postchange"]["description"], "before the block")
        self.assertEqual(webhook.kwargs["data"]["description"], "before the block")
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.description, "before the block")

    def test_a_block_that_raises_leaves_the_payload_of_a_second_instance_at_its_saved_state(self):
        """NetBox 4.7 serializes the latest instance that queued the object, which here the block then changed."""
        second = Interface.objects.get(pk=self.interface.pk)

        def change_and_fail():
            with atomic_with_events():
                _describe(second, "inside the block")
                raise RuntimeError("roll the block back")

        def body():
            _describe(self.interface, "first instance")
            _describe(second, "second instance")
            with self.assertRaises(RuntimeError):
                change_and_fail()

        self._run_as_a_request(body)

        [webhook] = queued_webhook_jobs(self.event_rule)
        self.assertEqual(webhook.kwargs["data"]["description"], "second instance")

    def test_a_nested_block_that_raises_restores_the_payload_of_an_event_before_both_blocks(self):
        """The event is two queues up, and the helper leaves the caller's instance as the block left it."""

        def change_and_fail():
            with atomic_with_events():
                _describe(self.interface, "inside the inner block")
                raise RuntimeError("roll the inner block back")

        def body():
            _describe(self.interface, "before the blocks")
            with atomic_with_events(), self.assertRaises(RuntimeError):
                change_and_fail()

        self._run_as_a_request(body)

        [webhook] = queued_webhook_jobs(self.event_rule)
        self.assertEqual(webhook.kwargs["data"]["description"], "before the blocks")
        self.assertEqual(self.interface.description, "inside the inner block")

    def test_a_delete_that_a_block_rolls_back_keeps_the_event_of_the_row(self):
        # NetBox skips a second delete of one object on a thread, so this test deletes a row of its own.
        interface = Interface.objects.create(device=self.device, name="eth1", type="1000base-t")

        def delete_and_fail():
            with atomic_with_events():
                interface.delete()
                raise RuntimeError("roll the delete back")

        def body():
            _describe(interface, "before the block")
            with self.assertRaises(RuntimeError):
                delete_and_fail()

        self._run_as_a_request(body)

        [webhook] = queued_webhook_jobs(self.event_rule)
        self.assertEqual(
            (webhook.kwargs["event_type"], webhook.kwargs["data"]["description"]), (OBJECT_UPDATED, "before the block")
        )

    def test_a_delete_before_a_block_that_recreates_the_row_and_rolls_back_stays_a_delete(self):
        interface = Interface.objects.create(device=self.device, name="eth2", type="1000base-t")
        pk = interface.pk

        def body():
            interface.delete()
            with atomic_with_events():
                interface.pk = pk
                interface.save()
                transaction.set_rollback(True)

        self._run_as_a_request(body)

        [webhook] = queued_webhook_jobs(self.event_rule)
        self.assertEqual((webhook.kwargs["event_type"], webhook.kwargs["data"]["id"]), (OBJECT_DELETED, pk))
        self.assertFalse(Interface.objects.filter(pk=pk).exists())

    @skipUnless(
        hasattr(netbox_events, "EventContext"), "NetBox before 4.5 serializes a payload when it queues the event"
    )
    def test_an_event_whose_row_is_gone_after_a_rollback_stops_the_request(self):
        """A row removed without a NetBox event leaves the helper no saved state to send, so it refuses to go on."""
        pk = self.interface.pk

        def body():
            _describe(self.interface, "before the block")
            with connection.cursor() as cursor:
                cursor.execute('DELETE FROM "dcim_interface" WHERE id = %s', [pk])
            with atomic_with_events():
                # Django inserts the row again when the update finds none.
                _describe(self.interface, "inside the block")
                transaction.set_rollback(True)

        with self.assertRaisesMessage(RuntimeError, f"dcim.Interface {pk} has a queued event but no row"):
            run_as_job_user(make_job("TxEventsGone"), body)

    def test_a_block_that_sets_rollback_drops_its_events(self):
        def body():
            with atomic_with_events():
                _describe(self.interface, "inside the block")
                transaction.set_rollback(True)

        self._run_as_a_request(body)

        self.assertEqual(queued_webhooks(self.event_rule), [])

    def test_outside_a_request_the_block_queues_nothing_and_leaves_the_queue_unset(self):
        def body():
            with atomic_with_events():
                _describe(self.interface, "outside a request")
            return events_queue in contextvars.copy_context()

        self.assertFalse(contextvars.Context().run(body))
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.description, "outside a request")


class _BlockRollbackError(Exception):
    """Raised inside a block to roll it back; the level around the block catches it."""


def _finish(outcome):
    """End a block as *outcome* says: return, raise, or mark it for rollback."""
    if outcome == "raises":
        raise _BlockRollbackError
    if outcome == "sets rollback":
        transaction.set_rollback(True)


# Each step changes the row the case starts from, through the same instance or a second one, or creates a row.
STEPS = (*product(("update", "delete", "tag"), ("same instance", "second instance")), ("create", "new row"))
OUTCOMES = ("commits", "raises", "sets rollback")


def generated_cases():
    """Yield ``(event before the blocks, levels)``, each level a ``((operation, instance), outcome)`` pair."""
    for event_before in (False, True):
        for depth in (1, 2):
            for levels in product(product(STEPS, OUTCOMES), repeat=depth):
                operations = [operation for (operation, _instance), _outcome in levels]
                # A step after a delete of the row has no row to change.
                if "delete" in operations[:-1] and operations[operations.index("delete") + 1 :] != ["create"]:
                    continue
                yield event_before, levels


@contextmanager
def request_context(user):
    """Set the request and the event queue as NetBox's event_tracking does, without its flush."""
    request = HttpRequest()
    request.user = user
    request.id = uuid.uuid4()
    request_token = current_request.set(request)
    queue_token = events_queue.set({})
    try:
        yield
    finally:
        events_queue.reset(queue_token)
        current_request.reset(request_token)


def _event_key(pk):
    return f"dcim.interface:{pk}"


def _payload(event):
    """Return what the flush serializes for *event*; a delete freezes it before the row goes, so only its ID is read."""
    return event["data"]["id"] if event["event_type"] == OBJECT_DELETED else event["data"]


class CommitFailureTest(TransactionTestCase):
    """The block is the outermost atomic block, and PostgreSQL refuses its COMMIT on a deferred foreign key."""

    def setUp(self):
        device = make_device("TxCommit", make_device_type(make_manufacturer("TxCommit"), "TxCommit"))
        self.interface = Interface.objects.create(device=device, name="eth0", type="1000base-t")
        self.user = get_user_model().objects.create_user(username="txcommit-operator")

    def test_a_block_whose_commit_fails_keeps_no_event_and_restores_the_payload_before_it(self):
        with request_context(self.user):
            _describe(self.interface, "before the block")
            with self.assertRaises(IntegrityError), atomic_with_events():
                _describe(self.interface, "inside the block")
                # No tenant has this ID; PostgreSQL checks the deferred foreign key at COMMIT.
                Site.objects.create(name="TxCommit dangling", slug="txcommit-dangling", tenant_id=2_000_000_000)
            queue = dict(events_queue.get())

        self.assertEqual(list(queue), [f"dcim.interface:{self.interface.pk}"])
        self.assertEqual(queue[f"dcim.interface:{self.interface.pk}"]["data"]["description"], "before the block")
        self.assertFalse(Site.objects.filter(slug="txcommit-dangling").exists())


class _SequenceCases:
    """Every combination of nesting, outcome, operation and instance leaves the queue that NetBox would flush right.

    The oracle reads each event as the flush does. Its payload is the saved row, its before-state is the row
    before the first change of the request, and a row whose changes all rolled back has no event.
    """

    EVENT_BEFORE = None

    @classmethod
    def setUpTestData(cls):
        prefix = f"TxSeq{cls.EVENT_BEFORE}"
        cls.device = make_device(prefix, make_device_type(make_manufacturer(prefix), prefix))
        cls.user = get_user_model().objects.create_user(username=f"{prefix.lower()}-operator")
        cls.tags = [
            Tag.objects.create(name=f"{prefix} {level}", slug=f"{prefix.lower()}-{level}") for level in range(2)
        ]

    def test_every_generated_case(self):
        cases = [
            (event_before, levels) for event_before, levels in generated_cases() if event_before == self.EVENT_BEFORE
        ]
        self.assertEqual(len(cases), 354)
        for event_before, levels in cases:
            with self.subTest(event_before=event_before, levels=levels), transaction.atomic():
                self._check_case(event_before, levels)
                transaction.set_rollback(True)

    def _check_case(self, event_before, levels):
        pk = Interface.objects.create(device=self.device, name="txseq-row", type="1000base-t").pk
        row, start = Interface.objects.get(pk=pk), Interface.objects.get(pk=pk)
        start.snapshot()
        created = []
        with request_context(self.user):
            if event_before:
                _describe(row, "before the blocks")
            self._run_level(levels, 0, row, created)
            queue = dict(events_queue.get())
        found = {
            key: (event["event_type"], event["snapshots"]["prechange"], _payload(event)) for key, event in queue.items()
        }
        self.assertEqual(found, self._expected(event_before, levels, pk, start._prechange_snapshot, created))

    def _run_level(self, levels, depth, row, created):
        (operation, instance), outcome = levels[depth]
        try:
            with atomic_with_events():
                self._apply(operation, row if instance == "same instance" else None, row.pk, depth, created)
                if depth + 1 < len(levels):
                    self._run_level(levels, depth + 1, row, created)
                _finish(outcome)
        except _BlockRollbackError:
            return

    def _apply(self, operation, instance, pk, depth, created):
        if operation == "create":
            new = Interface.objects.create(device=self.device, name=f"txseq-new-{depth}", type="1000base-t")
            created.append((new.pk, depth))
            return
        instance = instance or Interface.objects.get(pk=pk)
        if operation == "update":
            _describe(instance, f"level {depth}")
        elif operation == "delete":
            instance.delete()
        else:
            instance.snapshot()
            instance.tags.add(self.tags[depth])

    @staticmethod
    def _kept(levels, depth):
        return all(outcome == "commits" for _step, outcome in levels[: depth + 1])

    def _expected(self, event_before, levels, pk, start, created):
        kept = [depth for depth in range(len(levels)) if self._kept(levels, depth)]
        operations = {levels[depth][0][0] for depth in kept}
        expected = {}
        if "delete" in operations:
            expected[_event_key(pk)] = (OBJECT_DELETED, start, pk)
        elif event_before or operations - {"create"}:
            expected[_event_key(pk)] = (OBJECT_UPDATED, start, serialize_for_event(Interface.objects.get(pk=pk)))
        for new_pk, depth in created:
            if depth in kept:
                payload = serialize_for_event(Interface.objects.get(pk=new_pk))
                expected[_event_key(new_pk)] = (OBJECT_CREATED, None, payload)
        return expected


class SequenceWithoutAnEarlierEventTest(_SequenceCases, TestCase):
    EVENT_BEFORE = False


class SequenceAfterAnEarlierEventTest(_SequenceCases, TestCase):
    EVENT_BEFORE = True


class GeneratedCaseCountTest(SimpleTestCase):
    def test_the_generator_yields_every_valid_combination(self):
        """42 cases of one block, and 666 of two: 882 minus 216 that change the row after its delete."""
        self.assertEqual(len(list(generated_cases())), 708)
