# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Test documentation statements that define plugin behavior."""

import re
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
