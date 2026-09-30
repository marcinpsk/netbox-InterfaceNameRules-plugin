# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""An atomic block keeps the NetBox events it queued only when it commits, coalesced as NetBox does."""

import contextvars

from core.events import OBJECT_DELETED, OBJECT_UPDATED
from dcim.models import Interface
from django.db import transaction
from django.test import TestCase
from netbox.context import events_queue

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
        device = make_device("TxEvents", make_device_type(make_manufacturer("TxEvents"), "TxEvents"))
        cls.interface = Interface.objects.create(device=device, name="eth0", type="1000base-t")
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
