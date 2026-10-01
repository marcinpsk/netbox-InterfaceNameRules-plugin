# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Plugin jobs enqueued in a real netbox-branching branch run in that branch, and only there.

Each test enqueues its job through the Apply page with netbox-branching's cookie, then runs the job
from what the queue stored, as a worker does. A background REST request for a rule in a branch is
refused before it writes.
"""

from core.choices import JobStatusChoices
from core.models import Job, ObjectChange
from django.conf import settings
from django.urls import reverse

from netbox_interface_name_rules.api.views import BACKGROUND_IN_A_BRANCH
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.branch_cases import FLAT_NAMES, BranchWriteCase, ConversionCase, PlainModuleCase
from netbox_interface_name_rules.tests.helpers import (
    branch_cookie,
    make_manufacturer,
    make_module_type,
    names_of,
    queued_job,
    register_a_worker,
)

COMPLETED = JobStatusChoices.STATUS_COMPLETED
ERRORED = JobStatusChoices.STATUS_ERRORED


class _JobCase(BranchWriteCase):
    """Enqueue a job through the Apply page in the branch, and run it from the queue as a worker does."""

    def enqueue(self, action):
        """Post *action* on the Apply page of the rule and return the one job that it enqueued."""
        before = set(Job.objects.values_list("pk", flat=True))

        response = self.client.post(self.apply_url(self.rule), {"action": action})

        self.assertEqual(response.status_code, 302)
        [job] = Job.objects.exclude(pk__in=before)
        return job

    def run_queued(self, job):
        """Run *job* from the queue's record as a worker does, outside the branch, and return it reloaded."""
        queued_job(self, job).perform()
        job.refresh_from_db()
        return job

    def make_not_ready(self):
        """Mark the branch merged, as a merge does, so netbox-branching no longer activates it."""
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.models import Branch

        Branch.objects.filter(pk=self.branch.pk).update(status=BranchStatusChoices.MERGED)

    def alias_error(self):
        """Return the error of a job that started on main although it was enqueued in the branch."""
        return repr(RuntimeError(f"The write alias is 'default', but the operation expects {self.alias!r}."))


class ApplyJobInABranchTest(_JobCase, PlainModuleCase):
    PREFIX = "BrJobApply"

    def build(self):
        super().build()
        self.rule = InterfaceNameRule.objects.create(
            module_type=self.module_type, name_template="et-0/0/{bay_position}"
        )

    def test_the_job_stores_the_branch_and_its_alias_and_no_session_cookie(self):
        job = self.enqueue("background")

        queued = queued_job(self, job)

        self.assertEqual(
            queued.kwargs,
            {
                "job": job,
                "rule_id": self.rule.pk,
                "branch_schema_id": self.branch.schema_id,
                "expected_alias": self.alias,
            },
        )
        session = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertNotIn(session.encode(), queued.data)

    def test_the_job_renames_in_the_branch_only(self):
        job = self.run_queued(self.enqueue("background"))

        self.assertEqual((job.status, job.error), (COMPLETED, ""))
        with self.in_branch():
            self.assertEqual(names_of(self.module), ["et-0/0/0"])
        self.assertEqual(names_of(self.module), ["0"])
        self.assertEqual(self.branch_updates_of(self.interface), [("0", "et-0/0/0")])
        self.assertEqual(list(self.change_diffs().values_list("object_id", flat=True)), [self.interface.pk])

    def test_a_job_whose_branch_is_no_longer_ready_fails_naming_both_aliases_and_renames_nothing(self):
        job = self.enqueue("background")
        self.make_not_ready()

        job = self.run_queued(job)

        self.assertEqual((job.status, job.error), (ERRORED, self.alias_error()))
        with self.in_branch():
            self.assertEqual(names_of(self.module), ["0"])
        self.assertEqual(names_of(self.module), ["0"])
        self.assertFalse(self.change_diffs().exists())


class StartCheckBeforeTheRuleReadTest(_JobCase, PlainModuleCase):
    """The rule exists in the branch only, so a job that read it on main would find no rule and complete."""

    PREFIX = "BrJobRuleRead"

    def setUp(self):
        super().setUp()
        with self.in_branch():
            self.rule = InterfaceNameRule.objects.create(
                module_type=self.module_type, name_template="et-0/0/{bay_position}"
            )

    def test_a_job_whose_branch_is_no_longer_ready_fails_before_it_reads_the_rule(self):
        job = self.enqueue("background")
        self.make_not_ready()

        job = self.run_queued(job)

        self.assertEqual((job.status, job.error), (ERRORED, self.alias_error()))
        with self.in_branch():
            self.assertEqual(names_of(self.module), ["0"])


class ConvertJobInABranchTest(_JobCase, ConversionCase):
    PREFIX = "BrJobConvert"

    def test_the_job_converts_in_the_branch_only(self):
        job = self.run_queued(self.enqueue("convert_background"))

        self.assertEqual((job.status, job.error), (COMPLETED, ""))
        with self.in_branch():
            self.assertEqual(names_of(self.module), ["et-0/0/3", *FLAT_NAMES])
        self.assertEqual(names_of(self.module), list(FLAT_NAMES))

    def test_a_job_whose_branch_is_no_longer_ready_fails_and_converts_nothing(self):
        job = self.enqueue("convert_background")
        self.make_not_ready()

        job = self.run_queued(job)

        self.assertEqual((job.status, job.error), (ERRORED, self.alias_error()))
        with self.in_branch():
            self.assertEqual(names_of(self.module), list(FLAT_NAMES))
        self.assertEqual(names_of(self.module), list(FLAT_NAMES))


class BackgroundRuleRequestTest(BranchWriteCase):
    """NetBox runs a background REST request on main, so the rule endpoints refuse one in a branch."""

    PREFIX = "BrBackground"
    TEMPLATE = "et-0/0/{bay_position}"
    EDITED = "xe-0/0/{bay_position}"

    def build(self):
        self.module_type = make_module_type(make_manufacturer(self.PREFIX), self.PREFIX)
        self.rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template=self.TEMPLATE)

    def setUp(self):
        super().setUp()
        del self.client.cookies[branch_cookie()]
        # Without the refusal, NetBox would accept the request, because a worker is registered.
        register_a_worker(self)

    def background(self, method, payload, **headers):
        url = reverse("plugins-api:netbox_interface_name_rules-api:interfacenamerule-list")
        return getattr(self.client, method)(
            f"{url}?background=true", payload, content_type="application/json", headers=headers
        )

    def rules(self):
        return list(InterfaceNameRule.objects.values_list("pk", "name_template"))

    def test_a_background_rule_request_in_a_branch_is_refused_and_writes_nothing_on_either_alias(self):
        requests = {
            "post": [{"module_type": self.module_type.pk, "name_template": self.EDITED}],
            "put": [{"id": self.rule.pk, "module_type": self.module_type.pk, "name_template": self.EDITED}],
            "patch": [{"id": self.rule.pk, "name_template": self.EDITED}],
            "delete": [{"id": self.rule.pk}],
        }
        for method, payload in requests.items():
            with self.subTest(method=method):
                response = self.background(method, payload, **{"X-NetBox-Branch": self.branch.schema_id})

                self.assertEqual((response.status_code, response.json()), (400, [BACKGROUND_IN_A_BRANCH]))

        self.assertFalse(Job.objects.exists())
        self.assertEqual(self.rules(), [(self.rule.pk, self.TEMPLATE)])
        with self.in_branch():
            self.assertEqual(self.rules(), [(self.rule.pk, self.TEMPLATE)])
        self.assertFalse(ObjectChange.objects.using(self.alias).exists())
        self.assertFalse(self.change_diffs().exists())
