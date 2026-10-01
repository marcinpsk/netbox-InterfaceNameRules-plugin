# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Background jobs for bulk rule application."""

import contextvars
import functools
import logging
from abc import abstractmethod
from contextlib import ExitStack

from django.http import HttpRequest
from netbox.jobs import JobRunner
from netbox.registry import registry

from . import branching
from .transactions import write_alias, write_scope


def rule_job_kwargs(rule_id):
    """Return the kwargs of a job on the rule *rule_id* in the branch of the current request, derived on the server."""
    return {"rule_id": rule_id, "branch_schema_id": branching.branch_identity(), "expected_alias": write_alias()}


def _run_under_request_processors(request, body):
    """Call *body* inside every registered request processor; unlike NetBox, a processor that fails to enter raises."""
    with ExitStack() as stack:
        for request_processor in registry["request_processors"]:
            stack.enter_context(request_processor(request))
        return body()


def run_as_job_user(job, body, *, branch_schema_id):
    """Call *body*, which takes no arguments, as a request of the user who enqueued *job*.

    NetBox writes a change log only while a request is current, and a worker has none. The request
    carries the job ID, so the change log lists the changes of one job under one request ID. The
    request is in the branch *branch_schema_id*, or on main for None.
    """
    if job.user is None:
        raise ValueError(f"Job {job.pk} has no user, and the change log must name the user of each change.")
    # A Django request has each attribute that NetBox copies when an event rule acts on a change.
    request = HttpRequest()
    request.method = "POST"
    request.user = job.user
    request.id = job.job_id
    branching.activate_on(request, branch_schema_id)
    # The copy discards what the processors set; before NetBox 4.7, event_tracking keeps it when the body raises.
    return contextvars.copy_context().run(_run_under_request_processors, request, body)


class RuleJobRunner(JobRunner):
    """A background job over the rule named by rule_id, run as a request of the user who enqueued it.

    Its kwargs are those of ``rule_job_kwargs``. A job enqueued without them fails.
    """

    def __init__(self, job):
        super().__init__(job)
        # NetBox 4.4 added this logger, which also writes the job log; NetBox 4.3 has none.
        if not hasattr(self, "logger"):
            self.logger = logging.getLogger(f"netbox.jobs.{type(self).__name__}")

    def run(self, *, rule_id, branch_schema_id, expected_alias):
        """Run the job on the rule *rule_id* in the branch it was enqueued from; a missing rule is a warning."""
        run_as_job_user(
            self.job,
            functools.partial(self._run_on_rule_id, rule_id, expected_alias),
            branch_schema_id=branch_schema_id,
        )

    def _run_on_rule_id(self, rule_id, expected_alias):
        from .models import InterfaceNameRule

        # A branch that is not ready does not activate, so the scope raises before a read on main.
        with write_scope(expected_alias=expected_alias):
            rule = InterfaceNameRule.objects.filter(pk=rule_id).first()
            if rule is None:
                self.logger.warning("InterfaceNameRule with pk=%s does not exist; skipping.", rule_id)
                return
            try:
                self.run_on_rule(rule)
            except Exception:
                self.logger.exception("%s failed on rule '%s'", self.name, rule_id)
                raise

    @abstractmethod
    def run_on_rule(self, rule):
        """Run the job on *rule* and log what it did."""


class ApplyRuleJob(RuleJobRunner):
    """Apply an InterfaceNameRule retroactively to all matching installed modules."""

    class Meta:
        name = "Apply Interface Name Rule"

    def run_on_rule(self, rule):
        """Apply *rule* to all matching interfaces."""
        from .engine import apply_rule_to_existing

        outcome = apply_rule_to_existing(rule)
        self.logger.info("Renamed %d interface(s) using rule '%s'", outcome.changed_count, rule)
        if outcome.skipped_members:
            self.logger.warning("%d interface(s) skipped. The plugin log names each one.", len(outcome.skipped_members))


class ConvertFlatFamiliesJob(RuleJobRunner):
    """Convert the flat breakout families a rule's modules still carry to the channelized topology."""

    class Meta:
        name = "Convert Flat Interface Families"

    def run_on_rule(self, rule):
        """Convert every convertible flat family of *rule*."""
        from .engine import convert_flat_families

        outcome = convert_flat_families(rule)
        self.logger.info("Converted %d interface family(ies) using rule '%s'", len(outcome.changed_families), rule)
        if outcome.blocked_families:
            self.logger.warning("%d family(ies) skipped. The plugin log names each one.", len(outcome.blocked_families))
