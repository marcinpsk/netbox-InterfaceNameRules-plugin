# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Background jobs for bulk rule application."""

import contextvars
import logging
from contextlib import ExitStack

from netbox.jobs import JobRunner
from netbox.registry import registry
from utilities.request import NetBoxFakeRequest


def _run_under_request_processors(request, body, args, kwargs):
    """Run *body* inside every registered request processor, the way NetBox runs a script job."""
    with ExitStack() as stack:
        for request_processor in registry["request_processors"]:
            stack.enter_context(request_processor(request))
        return body(*args, **kwargs)


def run_as_job_user(job, body, *args, **kwargs):
    """Run *body* as a request of the user who enqueued *job*, so NetBox logs each change it writes.

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
    return contextvars.copy_context().run(_run_under_request_processors, request, body, args, kwargs)


class RuleJobRunner(JobRunner):
    """A background job over one rule, with the runner logger that NetBox 4.3 does not give."""

    def __init__(self, job):
        super().__init__(job)
        # NetBox 4.4 added this logger, which also writes the job log; NetBox 4.3 has none.
        if not hasattr(self, "logger"):
            self.logger = logging.getLogger(f"netbox.jobs.{type(self).__name__}")


class ApplyRuleJob(RuleJobRunner):
    """Apply an InterfaceNameRule retroactively to all matching installed modules."""

    class Meta:
        name = "Apply Interface Name Rule"

    def run(self, *args, **kwargs):
        """Apply the rule named by rule_id in kwargs to all matching interfaces, as the job's user."""
        run_as_job_user(self.job, self._apply, kwargs.get("rule_id"))

    def _apply(self, rule_id):
        """Retrieve the rule by pk and apply it to all matching interfaces."""
        from .engine import apply_rule_to_existing
        from .models import InterfaceNameRule

        if not rule_id:
            self.logger.warning("ApplyRuleJob called without rule_id; skipping.")
            return

        try:
            rule = InterfaceNameRule.objects.get(pk=rule_id)
        except InterfaceNameRule.DoesNotExist:
            self.logger.warning("InterfaceNameRule with pk=%s does not exist; skipping.", rule_id)
            return

        try:
            outcome = apply_rule_to_existing(rule)
        except Exception:
            self.logger.exception("Failed to apply rule '%s'", rule_id)
            raise

        self.logger.info("Renamed %d interface(s) using rule '%s'", outcome.changed_count, rule)
        if outcome.skipped_members:
            self.logger.warning("%d interface(s) skipped. The plugin log names each one.", len(outcome.skipped_members))


class ConvertFlatFamiliesJob(RuleJobRunner):
    """Convert the flat breakout families a rule's modules still carry to the channelized topology."""

    class Meta:
        name = "Convert Flat Interface Families"

    def run(self, *args, **kwargs):
        """Convert every convertible flat family of the rule named by rule_id in kwargs, as the job's user."""
        run_as_job_user(self.job, self._convert, kwargs.get("rule_id"))

    def _convert(self, rule_id):
        """Convert every convertible flat family of the rule identified by *rule_id*."""
        from .engine import convert_flat_families
        from .models import InterfaceNameRule

        if not rule_id:
            self.logger.warning("ConvertFlatFamiliesJob called without rule_id; skipping.")
            return

        try:
            rule = InterfaceNameRule.objects.get(pk=rule_id)
        except InterfaceNameRule.DoesNotExist:
            self.logger.warning("InterfaceNameRule with pk=%s does not exist; skipping.", rule_id)
            return

        try:
            outcome = convert_flat_families(rule)
        except Exception:
            self.logger.exception("Failed to convert families for rule '%s'", rule_id)
            raise

        self.logger.info("Converted %d interface family(ies) using rule '%s'", len(outcome.changed_families), rule)
        if outcome.blocked_families:
            self.logger.warning("%d family(ies) skipped. The plugin log names each one.", len(outcome.blocked_families))
