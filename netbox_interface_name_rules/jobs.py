# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Background jobs for bulk rule application."""

from netbox.jobs import JobRunner


class ApplyRuleJob(JobRunner):
    """Apply an InterfaceNameRule retroactively to all matching installed modules."""

    class Meta:
        name = "Apply Interface Name Rule"

    def run(self, *args, **kwargs):
        """Retrieve the rule by pk from kwargs and apply it to all matching interfaces."""
        from .engine import apply_rule_to_existing
        from .models import InterfaceNameRule

        rule_id = kwargs.get("rule_id")
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
        except Exception as exc:
            self.logger.exception("Failed to apply rule '%s': %s", rule_id, exc)
            raise

        self.logger.info("Renamed %d interface(s) using rule '%s'", outcome.changed_count, rule)
        if outcome.skipped_members:
            self.logger.warning(
                "%d interface(s) skipped — the plugin log names each one.", len(outcome.skipped_members)
            )


class ConvertFlatFamiliesJob(JobRunner):
    """Convert the flat breakout families a rule's modules still carry to the channelized topology."""

    class Meta:
        name = "Convert Flat Interface Families"

    def run(self, *args, **kwargs):
        """Convert every convertible flat family of the rule identified by rule_id in kwargs."""
        from .engine import convert_flat_families
        from .models import InterfaceNameRule

        rule_id = kwargs.get("rule_id")
        if not rule_id:
            self.logger.warning("ConvertFlatFamiliesJob called without rule_id; skipping.")
            return

        try:
            rule = InterfaceNameRule.objects.get(pk=rule_id)
        except InterfaceNameRule.DoesNotExist:
            self.logger.warning("InterfaceNameRule with pk=%s does not exist; skipping.", rule_id)
            return

        conflicts = []
        try:
            count = convert_flat_families(rule, conflicts=conflicts)
        except Exception as exc:
            self.logger.exception("Failed to convert families for rule '%s': %s", rule_id, exc)
            raise

        self.logger.info("Converted %d interface family(ies) using rule '%s'", count, rule)
        if conflicts:
            self.logger.warning("%d family(ies) skipped — the plugin log names each one.", len(conflicts))
