# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Background jobs for bulk rule application."""

import contextvars
import functools
import logging
from abc import abstractmethod
from contextlib import ExitStack

from netbox.jobs import JobRunner
from netbox.registry import registry
from utilities.request import NetBoxFakeRequest


def _run_under_request_processors(request, body):
    """Call *body* inside every registered request processor, the way NetBox runs a script job."""
    with ExitStack() as stack:
        for request_processor in registry["request_processors"]:
            stack.enter_context(request_processor(request))
        return body()


def run_as_job_user(job, body):
    """Call *body*, which takes no arguments, as a request of the user who enqueued *job*.

    NetBox writes a change log only while a request is current, and a worker has none. The request
    carries the job ID, so the change log lists the changes of one job under one request ID.
    """
    if job.user is None:
        raise ValueError(f"Job {job.pk} has no user, and the change log must name the user of each change.")
    request = NetBoxFakeRequest(
        {
            "META": {},
            "COOKIES": {},
            "POST": {},
            "GET": {},
            "FILES": {},
            "user": job.user,
            "method": "POST",
            "path": "",
            "id": job.job_id,
        }
    )
    # The copy discards what the processors set; before NetBox 4.7, event_tracking keeps it when the body raises.
    return contextvars.copy_context().run(_run_under_request_processors, request, body)


class RuleJobRunner(JobRunner):
    """A background job over the rule named by rule_id, run as a request of the user who enqueued it."""

    def __init__(self, job):
        super().__init__(job)
        # NetBox 4.4 added this logger, which also writes the job log; NetBox 4.3 has none.
        if not hasattr(self, "logger"):
            self.logger = logging.getLogger(f"netbox.jobs.{type(self).__name__}")

    def run(self, *args, **kwargs):
        """Run the job on the rule named by rule_id in kwargs; a missing rule is a warning, not an error."""
        run_as_job_user(self.job, functools.partial(self._run_on_rule_id, kwargs.get("rule_id")))

    def _run_on_rule_id(self, rule_id):
        from .models import InterfaceNameRule

        if not rule_id:
            self.logger.warning("%s called without rule_id; skipping.", type(self).__name__)
            return
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
