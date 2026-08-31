# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Test documentation statements that define plugin behavior."""

import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConversionDocumentationTest(unittest.TestCase):
    """Keep the conversion preflight description consistent with its implementation."""

    def test_missing_members_are_not_described_as_a_rollback_rejection(self):
        guide = (_PROJECT_ROOT / "docs" / "template-variables.md").read_text()
        section = guide.split("### Converting an installed flat family", 1)[1].split("### Converter Offset", 1)[0]

        self.assertIn("missing members are rejected locally before a conversion transaction starts", section.lower())
        self.assertNotIn("a missing sibling", section)
