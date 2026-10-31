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


def _imported_names(tree):
    # "subprocess" stays a module name without an import, so a snippet without imports is still checked.
    modules, functions = {"subprocess"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            functions.update(alias.asname or alias.name for alias in node.names if alias.name in WAITING_CALLS)
    return modules, functions


def _sets_a_timeout(keyword):
    # Forwarded **keywords can carry the timeout; a literal None is no limit.
    if keyword.arg is None:
        return True
    return keyword.arg == "timeout" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)


def _missing_timeouts(tree):
    modules, functions = _imported_names(tree)
    calls = {id(node.func): node for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        module_function = (
            isinstance(node, ast.Attribute)
            and node.attr in WAITING_CALLS
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        )
        imported_function = isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in functions
        if not (module_function or imported_function):
            continue
        call = calls.get(id(node))
        # A reference that is not called directly (runner = subprocess.run) hides the calls made through it.
        if call is None or not any(_sets_a_timeout(keyword) for keyword in call.keywords):
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

    def test_a_none_timeout_is_no_timeout(self):
        tree = ast.parse("subprocess.run(['git'], timeout=None)")
        self.assertEqual(list(_missing_timeouts(tree)), [1])

    def test_a_call_through_an_imported_name_requires_a_timeout(self):
        tree = ast.parse(
            "import subprocess as sp\nfrom subprocess import run, check_output as output\n"
            "sp.run(['git'])\nrun(['git'])\noutput(['git'])\nrun(['git'], timeout=60)"
        )
        self.assertEqual(list(_missing_timeouts(tree)), [3, 4, 5])

    def test_a_waiting_function_bound_to_another_name_is_refused(self):
        tree = ast.parse(
            "from subprocess import run\nrunner = subprocess.run\nother = run\nrunner(['git'])\n"
            "subprocess.run(['git'], timeout=60)"
        )
        self.assertEqual(list(_missing_timeouts(tree)), [2, 3])

    def test_the_sources_include_the_docs_media_tests(self):
        self.assertIn(PROJECT_ROOT / "docs_media" / "test_encoding.py", list(_sources()))
