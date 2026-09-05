# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Test documentation statements that define plugin behavior."""

import importlib
import re
import unittest
from pathlib import Path

import re2
import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_RE2_AUDIT = importlib.import_module("netbox_interface_name_rules.migrations.0014_validate_re2_patterns")


# _blocking_reason() and the staleness check decide these before _rewrite() runs, so NetBox never sees them.
_PREFLIGHT_REASONS = (
    "missing member",
    "stale family",
    "occupied parent name",
    "another channel family",
    "cabled sibling",
)


def _conversion_sentences():
    """Return the conversion section as lowercased, whitespace-collapsed sentences."""
    guide = (_PROJECT_ROOT / "docs" / "template-variables.md").read_text()
    section = guide.split("### Converting an installed flat family", 1)[1].split("### Converter Offset", 1)[0]
    return [sentence for sentence in " ".join(section.lower().split()).split(". ") if sentence]


def _example_conversion_sentences():
    """Return the conversion example as lowercased, whitespace-collapsed sentences."""
    guide = (_PROJECT_ROOT / "docs" / "examples.md").read_text()
    section = guide.split("### Converting a flat family (NetBox 4.7+)", 1)[1].split("What the conversion does", 1)[0]
    return [sentence for sentence in " ".join(section.lower().split()).split(". ") if sentence]


class ConversionDocumentationTest(unittest.TestCase):
    """Keep the conversion preflight description consistent with its implementation."""

    def test_missing_members_are_not_described_as_a_rollback_rejection(self):
        normalized = ". ".join(_conversion_sentences())

        self.assertIn("rejects missing members locally before a conversion transaction starts", normalized)
        self.assertNotIn("a missing sibling", normalized)

    def test_preflight_reasons_are_credited_to_the_plugin(self):
        sentences = _conversion_sentences()
        local = " ".join(s for s in sentences if "preflight" in s or "locally" in s)

        for reason in _PREFLIGHT_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, local)

    def test_no_preflight_reason_is_credited_to_netbox(self):
        for sentence in _conversion_sentences():
            if "netbox" not in sentence:
                continue
            for reason in _PREFLIGHT_REASONS:
                with self.subTest(reason=reason, sentence=sentence):
                    self.assertNotIn(reason, sentence)

    def test_example_separates_plugin_preflight_from_netbox_rejection(self):
        sentences = _example_conversion_sentences()
        local = " ".join(s for s in sentences if "preflight" in s or "locally" in s)

        for reason in _PREFLIGHT_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, local)
        for sentence in sentences:
            if "netbox" not in sentence:
                continue
            for reason in _PREFLIGHT_REASONS:
                with self.subTest(reason=reason, sentence=sentence):
                    self.assertNotIn(reason, sentence)


class PerformanceDocumentationTest(unittest.TestCase):
    """Keep the performance narrative consistent with the committed comparison."""

    def test_statement_attribution_matches_the_comparison(self):
        comparison = (_PROJECT_ROOT / "performance" / "comparisons" / "family-package-vs-existing.md").read_text()
        attribution = comparison.split("### Where those statements come from", 1)[1]
        changes_by_scenario = {}
        for line in attribution.splitlines():
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if len(cells) != 5 or not cells[0].startswith("module."):
                continue
            change = int(cells[4])
            if change:
                changes_by_scenario.setdefault(cells[0], {})[cells[1]] = change

        readme = (_PROJECT_ROOT / "performance" / "README.md").read_text()
        result = readme.split("## Result of the interface-family comparison", 1)[1]
        readme_changes = {}
        for line in result.split("Count the statements", 1)[0].splitlines():
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if len(cells) == 4 and re.fullmatch(r"[-+]?\d+", cells[3]):
                readme_changes[cells[0]] = int(cells[3])

        narrative = readme.split("No shared-buffer reads were observed", 1)[1].split("That revalidation", 1)[0].lower()
        sources = {source for changes in changes_by_scenario.values() for source in changes}

        self.assertTrue(changes_by_scenario)
        for source in sorted(sources):
            with self.subTest(source=source):
                self.assertIn(source.lower(), narrative)
        for scenario, changes in changes_by_scenario.items():
            with self.subTest(scenario=scenario):
                self.assertEqual(readme_changes.get(scenario.rsplit(".", 1)[-1]), sum(changes.values()))


class ReviewedDocumentationContractTest(unittest.TestCase):
    """Keep reviewed compatibility and transaction statements complete."""

    def test_rule_priority_lists_every_specificity_score(self):
        guide = (_PROJECT_ROOT / "docs" / "configuration.md").read_text()
        priority = guide.split("### Rule Priority", 1)[1].split("### RE2 Pattern Syntax", 1)[0]
        scopes = {
            7: (
                "Exact module type + parent module type + device type + platform",
                "Regex pattern + parent module type + device type + platform",
            ),
            6: (
                "Exact module type + parent module type + device type",
                "Regex pattern + parent module type + device type",
            ),
            5: ("Exact module type + parent module type + platform", "Regex pattern + parent module type + platform"),
            4: ("Exact module type + parent module type", "Regex pattern + parent module type"),
            3: ("Exact module type + device type + platform", "Regex pattern + device type + platform"),
            2: ("Exact module type + device type", "Regex pattern + device type"),
            1: ("Exact module type + platform", "Regex pattern + platform"),
            0: ("Exact module type only", "Regex pattern only"),
        }

        for score, (exact_scope, regex_scope) in scopes.items():
            with self.subTest(score=score):
                self.assertIn(
                    f"| {score} | {exact_scope} | {regex_scope} |",
                    priority,
                )

    def test_transaction_adr_states_unrelated_failure_behavior(self):
        adr = (_PROJECT_ROOT / "docs" / "adr" / "0005-execute-each-family-in-its-own-transaction.md").read_text()

        self.assertIn(
            "An unrelated integrity or infrastructure failure rolls back its own family and propagates to the operation boundary.",
            adr,
        )

    def test_re2_upgrade_guide_separates_errors_from_warnings(self):
        guide = (_PROJECT_ROOT / "docs" / "installation.md").read_text()
        section = guide.split("## Run Database Migrations", 1)[1].split("## Restart NetBox", 1)[0]
        migration = " ".join(section.split())

        self.assertIn("The migration stops", migration)
        self.assertIn("The migration warns and continues", migration)
        self.assertIn(r"`\d`, `\s`, and `\w`", migration)
        self.assertIn("case-insensitive matching outside a negated character class", migration)
        self.assertIn("Django records the warning-only migration as applied", migration)
        self.assertIn("Do not rerun the completed migration", migration)
        self.assertIn("If the migration stops", migration)
        self.assertIn("run the migration again", migration)

    def test_configuration_names_both_pattern_matching_contexts(self):
        guide = (_PROJECT_ROOT / "docs" / "configuration.md").read_text()
        pattern_guidance = guide.split("### Rule Fields", 1)[1].split("### RE2 Pattern Syntax", 1)[0]

        self.assertIn("module type model name", pattern_guidance)
        self.assertIn("parent interface's current name", pattern_guidance)

    def test_pattern_help_text_names_both_matching_contexts(self):
        """The field is a module-type matcher for module rules and a name filter for device rules."""
        from netbox_interface_name_rules.models import InterfaceNameRule

        help_text = InterfaceNameRule._meta.get_field("module_type_pattern").help_text

        self.assertIn("module type model name", help_text)
        self.assertIn("interface name", help_text)
        self.assertIn("Applies to Device Interfaces", help_text)

    def test_readme_badge_matches_the_supported_netbox_floor(self):
        readme = (_PROJECT_ROOT / "README.md").read_text()

        self.assertIn("NetBox-%E2%89%A54.3.0-blue", readme)
        self.assertNotIn("NetBox-%E2%89%A54.2.0-blue", readme)


def _patterns_in(node):
    """Yield every module_type_pattern value nested anywhere in a loaded YAML document."""
    if isinstance(node, dict):
        if isinstance(node.get("module_type_pattern"), str):
            yield node["module_type_pattern"]
        for value in node.values():
            yield from _patterns_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _patterns_in(value)


def _shipped_patterns():
    """Return every module-type pattern the plugin ships or documents, by source."""
    found = []
    for path in sorted((_PROJECT_ROOT / "contrib").glob("*.yaml")):
        found.extend((path.name, pattern) for pattern in _patterns_in(yaml.safe_load(path.read_text())))
    for path in sorted((_PROJECT_ROOT / "docs").glob("*.md")):
        for raw in re.findall(r"^\s*-?\s*module_type_pattern:\s*(.+)$", path.read_text(), re.MULTILINE):
            value = yaml.safe_load(raw)
            if isinstance(value, str):
                found.append((path.name, value))
    return found


class ShippedPatternRe2AuditTest(unittest.TestCase):
    """Every pattern the plugin ships or documents must survive the RE2 upgrade audit."""

    def test_no_shipped_pattern_blocks_the_upgrade(self):
        patterns = _shipped_patterns()

        self.assertTrue(patterns)
        for source, pattern in patterns:
            with self.subTest(source=source, pattern=pattern):
                re.compile(pattern)
                re2.compile(pattern)
                self.assertFalse(_RE2_AUDIT._uses_different_re2_semantics(pattern))
