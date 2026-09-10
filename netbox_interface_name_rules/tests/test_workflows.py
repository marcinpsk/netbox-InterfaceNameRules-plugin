# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Every workflow that runs pytest must install the plugins `addopts` makes mandatory.

pytest fails during argument parsing when `addopts` names an option no installed plugin registers,
so a missing distribution breaks the job before a single test runs.
"""

import pathlib
import re
import tempfile
import tomllib

from django.test import SimpleTestCase

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOWS = _PROJECT_ROOT / ".github" / "workflows"

# The option each pytest plugin registers. `--no-cov` needs pytest-cov for the same reason `--cov`
# does: an unregistered option is a parse error, whether it turns coverage on or off.
_OPTION_OWNERS = {
    "--cov": "pytest-cov",
    "--no-cov": "pytest-cov",
    "-n": "pytest-xdist",
    "--dist": "pytest-xdist",
}


def _configured_addopts():
    """Return the `addopts` string pytest applies to every invocation in this repository."""
    with (_PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]["addopts"]


def _required_distributions(*command_lines):
    """Return the pytest plugins the options in *command_lines* require."""
    text = " ".join(command_lines)
    # An option ends at whitespace, at `=`, or at end of input; `--cov-report` is not `--cov`.
    return {
        owner for option, owner in _OPTION_OWNERS.items() if re.search(rf"(?<!\S){re.escape(option)}(?=[\s=]|$)", text)
    }


def _workflows_running_pytest(directory=None):
    """Map each workflow in *directory* that invokes pytest to its full text."""
    found = {}
    for path in sorted((directory or _WORKFLOWS).glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        prefixes = [
            match[1]
            for segment in re.split(r"&&|\|\||;|\||\n", text)
            if (match := re.search(r"^(.*?)\bpytest(?=\s|$)", segment))
        ]
        if any(not re.search(r"\bpip(?:3)?\s+install\b", prefix) for prefix in prefixes):
            found[path.name] = text
    return found


class RequiredDistributionTest(SimpleTestCase):
    """The option scan reads every spelling pytest accepts, including `--option=value`."""

    def test_a_long_option_with_a_value_is_recognised(self):
        """`addopts` uses `--cov=package`, so missing this form would disarm the whole check."""
        self.assertEqual(_required_distributions("--cov=netbox_interface_name_rules"), {"pytest-cov"})
        self.assertEqual(_required_distributions("--dist=loadscope"), {"pytest-xdist"})

    def test_a_separated_value_is_recognised(self):
        self.assertEqual(_required_distributions("--dist loadscope"), {"pytest-xdist"})
        self.assertEqual(_required_distributions("-n auto"), {"pytest-xdist"})

    def test_a_longer_option_that_merely_starts_the_same_is_not_a_match(self):
        """`--cov-report` alone registers nothing; only `--cov` and `--no-cov` do."""
        self.assertEqual(_required_distributions("--cov-report=term-missing"), set())

    def test_the_configured_addopts_require_both_plugins(self):
        self.assertEqual(_required_distributions(_configured_addopts()), {"pytest-cov", "pytest-xdist"})


class WorkflowPytestPluginTest(SimpleTestCase):
    """A workflow that runs pytest installs every plugin the run needs."""

    def test_every_pytest_workflow_installs_the_plugins_addopts_requires(self):
        workflows = _workflows_running_pytest()

        self.assertTrue(workflows, "no workflow was detected as running pytest")
        for name, text in workflows.items():
            pytest_lines = re.findall(r"^.*\bpytest\b.*$", text, re.MULTILINE)
            required = _required_distributions(_configured_addopts(), *pytest_lines)
            for distribution in sorted(required):
                with self.subTest(workflow=name, distribution=distribution):
                    self.assertRegex(
                        text,
                        rf"install[^\n]*\b{re.escape(distribution)}\b",
                        f"{name} runs pytest but never installs {distribution}",
                    )


class WorkflowDetectionTest(SimpleTestCase):
    def test_command_prefixes_are_detected_and_installations_are_excluded(self):
        commands = {
            "python": "python -m pytest tests",
            "poetry": "poetry run pytest tests",
            "xvfb": "xvfb-run pytest tests",
            "plain": "pytest tests",
            "selection": "pytest -k install",
            "chained": "pip install -e . && pytest",
            "or_chain": "pip install -e . || pytest",
            "semicolon_chain": "pip install -e .; pytest",
            "pipeline": "pip install -e . | pytest",
            "pip": "pip install pytest-cov",
            "uv": "uv pip install pytest-xdist",
            "pip_pytest": "pip install pytest",
        }
        with tempfile.TemporaryDirectory() as directory:
            workflows = pathlib.Path(directory)
            for name, command in commands.items():
                (workflows / f"{name}.yml").write_text(f"steps:\n  - run: |\n      {command}\n", encoding="utf-8")
            found = _workflows_running_pytest(workflows)
        for name, command in commands.items():
            with self.subTest(command=command):
                self.assertEqual(f"{name}.yml" in found, name not in {"pip", "uv", "pip_pytest"})
