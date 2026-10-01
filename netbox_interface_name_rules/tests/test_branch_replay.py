# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""A netbox-branching merge, revert or sync replays logged changes, and the rename triggers do nothing then.

A test starts the operation on netbox-branching's page of the branch, as an operator does, and runs
the job that the page enqueued from the queue, as a worker does, or it calls the operation as a shell
does. netbox-branching replays only the changes that NetBox logged, so the changes that a test
replays go through requests.
"""

from core.choices import JobStatusChoices
from core.models import Job, ObjectChange
from dcim.models import Device, Interface, InterfaceTemplate, Module, VirtualChassis
from django.db import transaction
from django.urls import reverse
from extras.models import JournalEntry

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.branch_cases import BranchWriteCase, KeptChannelCase
from netbox_interface_name_rules.tests.helpers import (
    PLAIN_TYPE,
    branch_cookie,
    install_form,
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
    queued_job,
)

COMPLETED = JobStatusChoices.STATUS_COMPLETED
ERRORED = JobStatusChoices.STATUS_ERRORED
ITERATIVE = "iterative"
SQUASH = "squash"
POSITIONS = (0, 1, 2)
# The names of each bay after the branch installed a module in bay 0 and moved the module of bay 1 to bay 2.
BRANCH_NAMES = {0: ["a0", "b0"], 1: [], 2: ["a2", "b2"]}
NAMES_BEFORE = {0: [], 1: ["a1", "b1"], 2: []}
# The names that NetBox gives the channelized family in bay 1.
RAW_FAMILY = ["1", "1:1", "1:2", "1:3", "1:4"]


def merging(strategy, commit=True):
    """Return the form that merges the branch with *strategy*, or only tries it when *commit* is false."""
    return {"merge_strategy": strategy, **({"commit": "on"} if commit else {})}


class _ReplayCase(BranchWriteCase):
    """Run netbox-branching's merge, revert and sync as an operator and a worker do."""

    def enqueue(self, action, **form):
        """Post *form* on netbox-branching's *action* page of the branch and return the job that it enqueued."""
        before = set(Job.objects.values_list("pk", flat=True))

        response = self.client.post(
            reverse(f"plugins:netbox_branching:branch_{action}", kwargs={"pk": self.branch.pk}), form
        )

        self.assertEqual(response.status_code, 302)
        [job] = Job.objects.exclude(pk__in=before)
        return job

    def run_queued(self, job):
        """Run *job* from the queue's record as a worker does, and return it reloaded."""
        queued_job(self, job).perform()
        job.refresh_from_db()
        return job

    def act(self, action, **form):
        """Post *form* on netbox-branching's *action* page of the branch, run the job it enqueued, and return it."""
        return self.run_queued(self.enqueue(action, **form))

    def on_main(self):
        """Send the next requests of the test client to main."""
        del self.client.cookies[branch_cookie()]

    def last_change_on_main(self):
        """Return the primary key of the newest change that NetBox logged on main."""
        return ObjectChange.objects.order_by("-pk").values_list("pk", flat=True).first() or 0

    def assert_only_replayed_changes_on_main(self, since):
        """Assert that each change logged on main after *since* replays a change of the branch, and no journal entry."""
        replayed = set(ObjectChange.objects.using(self.alias).values_list("request_id", flat=True))
        logged = set(ObjectChange.objects.filter(pk__gt=since).values_list("request_id", flat=True))

        self.assertTrue(logged)
        self.assertLessEqual(logged, replayed)
        self.assertFalse(JournalEntry.objects.exists())


class _InstallAndMoveCase(_ReplayCase):
    """A device with three module bays, and a module in bay 1 whose interfaces NetBox named ``a1`` and ``b1``."""

    def build(self):
        manufacturer = make_manufacturer(self.PREFIX)
        device_type = make_device_type(manufacturer, self.PREFIX)
        make_module_bay_templates(device_type, tuple(f"Bay {position}" for position in POSITIONS))
        self.device = make_device(self.PREFIX, device_type)
        self.module_type = make_module_type(manufacturer, self.PREFIX)
        for template in ("a{module}", "b{module}"):
            InterfaceTemplate.objects.create(module_type=self.module_type, name=template, type=PLAIN_TYPE)
        with transaction.atomic():
            self.module = Module.objects.create(
                device=self.device, module_bay=self.bay(1), module_type=self.module_type
            )

    def add_rule(self):
        return InterfaceNameRule.objects.create(module_type=self.module_type, name_template="{base}.br")

    def install_and_move(self):
        """Install a module in bay 0 through the UI and move the module of bay 1 to bay 2 through the REST API."""
        response = self.client.post(reverse("dcim:module_add"), install_form(self.bay(0), self.module_type))
        self.assertEqual(response.status_code, 302)
        response = self.client.patch(
            reverse("dcim-api:module-detail", kwargs={"pk": self.module.pk}),
            {"module_bay": self.bay(2).pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def names(self):
        """Return the sorted interface names of the module in each bay, on the active branch or on main."""
        return {position: sorted(name for _, name in self.interfaces_at(position)) for position in POSITIONS}

    def branch_names(self):
        with self.in_branch():
            return self.names()


class _MergeAndRevertTests:
    """The rule exists on main only, so the branch keeps NetBox's names, and a rename trigger on main would not."""

    STRATEGY = None

    def setUp(self):
        super().setUp()
        self.add_rule()
        self.install_and_move()

    def test_the_merge_gives_main_the_names_of_the_branch_and_only_the_replayed_changes(self):
        job = self.enqueue("merge", **merging(self.STRATEGY))
        since = self.last_change_on_main()

        job = self.run_queued(job)

        self.assertEqual((job.status, job.error), (COMPLETED, ""))
        self.assertEqual(self.branch_names(), BRANCH_NAMES)
        self.assertEqual(self.names(), BRANCH_NAMES)
        self.assert_only_replayed_changes_on_main(since)

    def test_the_revert_of_the_merge_gives_main_the_names_from_before_the_branch(self):
        self.assertEqual(self.names(), NAMES_BEFORE)
        self.assertEqual(self.act("merge", **merging(self.STRATEGY)).status, COMPLETED)
        job = self.enqueue("revert", commit="on")
        since = self.last_change_on_main()

        job = self.run_queued(job)

        self.assertEqual((job.status, job.error), (COMPLETED, ""))
        self.assertEqual(self.names(), NAMES_BEFORE)
        self.assert_only_replayed_changes_on_main(since)


class IterativeMergeTest(_MergeAndRevertTests, _InstallAndMoveCase):
    PREFIX = "BrMergeIter"
    STRATEGY = ITERATIVE


class SquashMergeTest(_MergeAndRevertTests, _InstallAndMoveCase):
    PREFIX = "BrMergeSquash"
    STRATEGY = SQUASH


class SyncTest(_InstallAndMoveCase):
    """The rule exists in the branch only, so main keeps NetBox's names, and a rename trigger in the branch would not."""

    PREFIX = "BrSync"

    def test_a_sync_writes_no_unlogged_rename_into_the_branch(self):
        with self.in_branch():
            self.add_rule()
        self.on_main()
        self.install_and_move()
        on_main = self.names()

        job = self.act("sync", commit="on")

        self.assertEqual((job.status, job.error), (COMPLETED, ""))
        self.assertEqual(on_main, BRANCH_NAMES)
        self.assertEqual(self.branch_names(), on_main)


class ReplayInTheActiveBranchTest(_ReplayCase):
    """A merge and a revert started from a shell in which the branch is active.

    The branch changes the virtual-chassis position of a device, which is a rename trigger. While the
    branch is active, netbox-branching itself refuses a replayed create and the revert of a module move.
    """

    PREFIX = "BrReplayActive"

    def build(self):
        chassis = VirtualChassis.objects.create(name=f"{self.PREFIX} chassis")
        device_type = make_device_type(make_manufacturer(self.PREFIX), self.PREFIX)
        self.device = make_device(self.PREFIX, device_type, virtual_chassis=chassis, vc_position=1)

    def position_on_main(self):
        return Device.objects.get(pk=self.device.pk).vc_position

    def test_a_merge_and_a_revert_started_in_the_branch_replay_a_rename_trigger_without_an_error(self):
        response = self.client.patch(
            reverse("dcim-api:device-detail", kwargs={"pk": self.device.pk}),
            {"vc_position": 2},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.branch.refresh_from_db()

        with self.in_branch():
            self.branch.merge(user=self.user)
        merged = self.position_on_main()
        with self.in_branch():
            self.branch.revert(user=self.user)

        self.assertEqual((merged, self.position_on_main()), (2, 1))


class MergeExitTest(_InstallAndMoveCase):
    """A merge that returns early or fails leaves the next save on main a rename trigger.

    The rule exists on main and in the branch, so an install on main gets its names only from a rename
    trigger.
    """

    PREFIX = "BrMergeExit"

    def build(self):
        super().build()
        self.add_rule()

    def assert_an_install_on_main_gets_the_names_of_the_rule(self):
        with transaction.atomic():
            Module.objects.create(device=self.device, module_bay=self.bay(0), module_type=self.module_type)

        self.assertEqual(self.names()[0], ["a0.br", "b0.br"])

    def test_a_merge_without_a_change_leaves_the_next_save_a_rename_trigger(self):
        job = self.act("merge", **merging(ITERATIVE))

        self.assertEqual((job.status, job.error), (COMPLETED, ""))
        self.assert_an_install_on_main_gets_the_names_of_the_rule()

    def test_a_dry_run_merge_leaves_the_next_save_a_rename_trigger(self):
        self.install_and_move()

        job = self.act("merge", **merging(ITERATIVE, commit=False))

        self.assertEqual((job.status, job.error), (COMPLETED, ""))
        self.assertEqual(self.names(), NAMES_BEFORE)
        self.assert_an_install_on_main_gets_the_names_of_the_rule()

    def test_a_failed_merge_leaves_the_next_save_a_rename_trigger(self):
        self.install_and_move()
        # The merge replays the creation of interface a0, and the device on main has an interface of that name now.
        Interface.objects.create(device=self.device, name="a0", type=PLAIN_TYPE)

        job = self.act("merge", **merging(ITERATIVE))

        self.assertEqual(job.status, ERRORED)
        self.assertEqual(self.names(), NAMES_BEFORE)
        Interface.objects.filter(device=self.device, name="a0").delete()
        self.assert_an_install_on_main_gets_the_names_of_the_rule()

    def test_a_merge_of_the_branch_in_another_worker_does_not_skip_a_save_here(self):
        """The replay mark belongs to the context that ran the operation, not to the branch status that workers share."""
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.models import Branch

        self.install_and_move()
        self.assertEqual(self.act("merge", **merging(ITERATIVE, commit=False)).status, COMPLETED)
        # netbox-branching sets this status first when another worker starts to merge the branch.
        Branch.objects.filter(pk=self.branch.pk).update(status=BranchStatusChoices.MERGING)

        self.assert_an_install_on_main_gets_the_names_of_the_rule()


class _CascadeCase(KeptChannelCase, _ReplayCase):
    """NetBox's cascade renames a kept channel again at the commit of a replay: the documented limit.

    The rule keeps channel 2 at ``1:2`` while it renames the parent. A replay renames the parent before
    its commit, so NetBox's cascade gives channel 2 the name ``et-0/0/1:2`` at that commit.
    """

    def apply_on_the_page(self):
        response = self.client.post(
            self.apply_url(self.rule), {"action": "apply", "interface_ids": [str(self.parent("1").pk)]}
        )
        self.assertEqual(response.status_code, 302)

    def names_on(self, alias):
        return sorted(Interface.objects.using(alias).filter(module=self.modules["1"]).values_list("name", flat=True))


class _CascadeMergeTests:
    """A merge with one strategy, and the revert of that merge."""

    STRATEGY = None

    def test_a_merge_renames_the_kept_channel_on_main_and_a_revert_restores_the_names_from_before(self):
        self.apply_on_the_page()
        self.assertEqual(self.names_on(self.alias), self.kept("1"))

        merged = self.act("merge", **merging(self.STRATEGY))
        names_after_the_merge = self.names_on("default")
        reverted = self.act("revert", commit="on")

        self.assertEqual((merged.status, reverted.status), (COMPLETED, COMPLETED))
        self.assertEqual(names_after_the_merge, self.cascaded("1"))
        self.assertEqual(self.names_on(self.alias), self.kept("1"))
        self.assertEqual(self.names_on("default"), RAW_FAMILY)


class IterativeCascadeTest(_CascadeMergeTests, _CascadeCase):
    PREFIX = "BrCascadeIter"
    STRATEGY = ITERATIVE


class SquashCascadeTest(_CascadeMergeTests, _CascadeCase):
    PREFIX = "BrCascadeSquash"
    STRATEGY = SQUASH


class SyncCascadeTest(_CascadeCase):
    PREFIX = "BrCascadeSync"

    def test_a_sync_renames_the_kept_channel_in_the_branch(self):
        self.on_main()
        self.apply_on_the_page()
        self.assertEqual(self.names_on("default"), self.kept("1"))

        job = self.act("sync", commit="on")

        self.assertEqual((job.status, job.error), (COMPLETED, ""))
        self.assertEqual(self.names_on(self.alias), self.cascaded("1"))
        self.assertEqual(self.names_on("default"), self.kept("1"))
