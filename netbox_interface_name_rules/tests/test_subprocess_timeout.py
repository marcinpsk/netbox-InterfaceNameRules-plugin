# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require a timeout on each subprocess call, so a hung child fails the run instead of blocking it."""

import ast
import pathlib

from django.test import SimpleTestCase

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
# Each tracked Python file is in one of these, or is conftest.py at the root.
SOURCE_ROOTS = (".devcontainer", "docs_media", "netbox_interface_name_rules", "performance", "scripts")
WAITING_CALLS = {"run", "call", "check_call", "check_output"}


def _missing_timeouts(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr in WAITING_CALLS
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
        ):
            continue
        if any(keyword.arg in {"timeout", None} for keyword in node.keywords):
            continue
        yield node.lineno


def _sources():
    yield PROJECT_ROOT / "conftest.py"
    for root in SOURCE_ROOTS:
        yield from sorted((PROJECT_ROOT / root).rglob("*.py"))


class SubprocessTimeoutTest(SimpleTestCase):
    def test_subprocess_calls_declare_a_timeout(self):
        violations = []
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            violations.extend(f"{path.relative_to(PROJECT_ROOT)}:{line}" for line in _missing_timeouts(tree))
        self.assertEqual(violations, [], "Missing timeout= at:\n" + "\n".join(violations))

    def test_a_call_that_waits_requires_a_timeout(self):
        tree = ast.parse(
            "subprocess.run(['git'])\nsubprocess.check_output(['git'])\n"
            "subprocess.check_call(['git'])\nsubprocess.call(['git'])"
        )
        self.assertEqual(list(_missing_timeouts(tree)), [1, 2, 3, 4])

    def test_a_timeout_or_forwarded_keywords_satisfy_the_rule(self):
        tree = ast.parse("subprocess.run(['git'], timeout=60)\nsubprocess.run(['git'], **options)")
        self.assertEqual(list(_missing_timeouts(tree)), [])

    def test_the_sources_include_the_docs_media_tests(self):
        self.assertIn(PROJECT_ROOT / "docs_media" / "test_encoding.py", list(_sources()))
