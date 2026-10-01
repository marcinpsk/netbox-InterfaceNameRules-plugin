# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""An atomic block keeps the NetBox events it queued only when it commits, coalesced as NetBox does."""

import contextvars
import uuid
from contextlib import contextmanager, nullcontext
from itertools import product
from unittest import skipUnless

from core.events import OBJECT_CREATED, OBJECT_DELETED, OBJECT_UPDATED
from dcim.models import Interface, Site
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.http import HttpRequest
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
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
from netbox_interface_name_rules.transactions import atomic_with_events, on_commit, write_scope


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
            run_as_job_user(make_job("TxEvents"), body, branch_schema_id=None)

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
            with atomic_with_events() as block:
                interface.pk = pk
                interface.save()
                block.set_rollback()

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
            with atomic_with_events() as block:
                # Django inserts the row again when the update finds none.
                _describe(self.interface, "inside the block")
                block.set_rollback()

        with self.assertRaisesMessage(RuntimeError, f"dcim.Interface {pk} has a queued event but no row"):
            run_as_job_user(make_job("TxEventsGone"), body, branch_schema_id=None)

    def test_a_block_that_sets_rollback_drops_its_events(self):
        def body():
            with atomic_with_events() as block:
                _describe(self.interface, "inside the block")
                block.set_rollback()

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


def _finish(outcome, block):
    """End *block* as *outcome* says: return, raise, or mark it for rollback."""
    if outcome == "raises":
        raise _BlockRollbackError
    if outcome == "sets rollback":
        block.set_rollback()


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


class _CallbackError(Exception):
    """Raised by a commit callback after the database committed."""


def _raise_after_commit():
    raise _CallbackError


# How a block ends, and whether its changes reach the database.
BLOCK_ENDINGS = {
    "returns": True,
    "raises": False,
    "sets rollback": False,
    "fails at COMMIT": False,
    "has a commit callback that raises": True,
}


class CommitOutcomeTest(TransactionTestCase):
    """The helper keeps a block's events exactly when its changes committed, as the outermost block or a savepoint.

    Only an outermost block runs COMMIT and its commit callbacks at its exit. A savepoint defers its callbacks to
    the enclosing transaction, and releasing a savepoint checks no deferred constraint.
    """

    def setUp(self):
        self.device = make_device("TxOutcome", make_device_type(make_manufacturer("TxOutcome"), "TxOutcome"))
        self.user = get_user_model().objects.create_user(username="txoutcome-operator")

    def test_every_block_ending(self):
        cases = [
            (outermost, ending)
            for outermost, ending in product((True, False), BLOCK_ENDINGS)
            if outermost or ending != "fails at COMMIT"
        ]
        self.assertEqual(len(cases), 9)
        for number, (outermost, ending) in enumerate(cases):
            with self.subTest(outermost=outermost, ending=ending):
                self._check(number, outermost, ending)

    def _check(self, number, outermost, ending):
        existing = Interface.objects.create(device=self.device, name=f"txoutcome-{number}", type="1000base-t")
        callbacks = []
        with request_context(self.user):
            if outermost:
                new_pk = self._change(number, existing, ending, callbacks)
            else:
                with self.assertRaises(_CallbackError) if "callback" in ending else nullcontext():
                    with transaction.atomic():
                        new_pk = self._change(number, existing, ending, callbacks)
                        # A savepoint leaves its commit callbacks to the enclosing transaction.
                        self.assertEqual(callbacks, [])
            queue = dict(events_queue.get())
        self.assertEqual(Interface.objects.filter(pk=new_pk).exists(), BLOCK_ENDINGS[ending])
        self.assertEqual(callbacks, ["ran"] if "callback" in ending else [])
        rows = Interface.objects.filter(pk__in=(existing.pk, new_pk))
        self.assertEqual({key: _payload(event) for key, event in queue.items()}, _saved_payloads(rows))

    def _change(self, number, existing, ending, callbacks):
        """Queue an event for *existing*, then change it and create a row in a block that ends as *ending* says."""
        _describe(existing, "before the block")
        new_pk = None
        try:
            with atomic_with_events() as block:
                _describe(existing, "inside the block")
                new_pk = Interface.objects.create(
                    device=self.device, name=f"txoutcome-new-{number}", type="1000base-t"
                ).pk
                self._end(number, ending, callbacks, block)
        except (_BlockRollbackError, IntegrityError):
            return new_pk
        except _CallbackError:
            self.assertEqual(callbacks, ["ran"])
        return new_pk

    @staticmethod
    def _end(number, ending, callbacks, block):
        if ending == "raises":
            raise _BlockRollbackError
        if ending == "sets rollback":
            block.set_rollback()
        elif ending == "fails at COMMIT":
            # No tenant has this ID; PostgreSQL checks the deferred foreign key at COMMIT.
            Site.objects.create(
                name=f"TxOutcome dangling {number}", slug=f"txoutcome-dangling-{number}", tenant_id=2_000_000_000
            )
        elif ending == "has a commit callback that raises":
            transaction.on_commit(lambda: callbacks.append("ran"))
            transaction.on_commit(_raise_after_commit)


def _saved_payloads(rows):
    """Return the payload a flush must send for each row, keyed as NetBox keys its queue."""
    return {_event_key(row.pk): serialize_for_event(row) for row in rows}


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
            with atomic_with_events() as block:
                self._apply(operation, row if instance == "same instance" else None, row.pk, depth, created)
                if depth + 1 < len(levels):
                    self._run_level(levels, depth + 1, row, created)
                _finish(outcome, block)
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


class WriteInterfacesTo:
    """A database router that sends each write of an interface to one alias."""

    def __init__(self, alias):
        self.alias = alias

    def db_for_write(self, model, **hints):
        return self.alias if model is Interface else None


class WriteScopeOnMainTest(TestCase):
    """On main a write scope holds ``default`` alone, and the scope and its blocks add no query."""

    def test_the_scope_pins_default_and_runs_no_query(self):
        with self.assertNumQueries(0), write_scope() as aliases:
            self.assertEqual(aliases, ("default",))

    def test_an_unexpected_write_alias_raises_before_any_query(self):
        with self.assertNumQueries(0), self.assertRaisesMessage(RuntimeError, "'default'") as raised:
            with write_scope(expected_alias="schema_elsewhere"):
                self.fail("the scope opened")

        self.assertIn("'schema_elsewhere'", str(raised.exception))

    def test_a_nested_scope_joins_the_open_scope(self):
        with write_scope() as outer, write_scope(expected_alias="default") as inner:
            self.assertIs(inner, outer)

    def test_a_nested_scope_on_another_write_alias_raises_before_any_query(self):
        with write_scope(), override_settings(DATABASE_ROUTERS=[WriteInterfacesTo("schema_elsewhere")]):
            with self.assertNumQueries(0), self.assertRaisesMessage(RuntimeError, "'schema_elsewhere'"):
                with write_scope():
                    self.fail("the nested scope opened")

    def test_on_commit_outside_a_scope_raises(self):
        with self.assertRaisesMessage(RuntimeError, "write scope"):
            on_commit(lambda: None)

    def test_on_commit_registers_the_callback_itself_once(self):
        def callback():
            pass

        with self.captureOnCommitCallbacks() as callbacks, write_scope():
            on_commit(callback)

        self.assertEqual(callbacks, [callback])

    def test_a_block_marked_for_rollback_writes_nothing(self):
        with atomic_with_events() as block:
            self.assertEqual(block.aliases, ("default",))
            site = Site.objects.create(name="TxScope rollback", slug="txscope-rollback")
            block.set_rollback()

        self.assertFalse(Site.objects.filter(pk=site.pk).exists())

    def test_a_block_runs_the_statements_of_one_atomic_block(self):
        with CaptureQueriesContext(connection) as plain, transaction.atomic():
            Site.objects.create(name="TxScope plain", slug="txscope-plain")
        with CaptureQueriesContext(connection) as block, atomic_with_events():
            Site.objects.create(name="TxScope block", slug="txscope-block")

        def verbs(queries):
            return [query["sql"].split()[0] for query in queries.captured_queries]

        self.assertEqual(verbs(block), verbs(plain))
