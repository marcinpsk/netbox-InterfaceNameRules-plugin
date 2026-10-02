# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the coverage wiring of the CI test workflow."""

import shlex
import tomllib
import unittest
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _workflow_jobs():
    workflow = yaml.safe_load((_PROJECT_ROOT / ".github" / "workflows" / "test.yaml").read_text(encoding="utf-8"))
    return workflow["jobs"]


def _steps_using(job, action):
    return [step for step in job["steps"] if step.get("uses", "").startswith(f"{action}@")]


class WorkflowActionPinsTest(unittest.TestCase):
    """Workflow jobs must use the same setup-uv action revision."""

    def test_setup_uv_pins_match_across_all_workflow_jobs(self):
        pins = {}
        for path in sorted((_PROJECT_ROOT / ".github" / "workflows").iterdir()):
            if path.suffix not in {".yaml", ".yml"}:
                continue
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
            for name, job in workflow["jobs"].items():
                if "steps" not in job:
                    continue
                for index, step in enumerate(_steps_using(job, "astral-sh/setup-uv")):
                    pins[f"{path.name}:{name}:{index}"] = step["uses"]

        self.assertTrue(pins, "no setup-uv steps found")
        self.assertEqual(len(set(pins.values())), 1, f"setup-uv pins differ across workflow jobs: {pins}")


class LockedWorkflowDependenciesTest(unittest.TestCase):
    """CI tools use the lockfile without replacing NetBox's environment."""

    def test_dependency_group_installers_consume_checked_lock_exports(self):
        installers = [
            ("test.yaml", "test-netbox", "Install NetBox and plugin", "ci-tests"),
            ("test.yaml", "coverage", "Install coverage", "ci-tests"),
            ("test.yaml", "test-netbox", "Install and configure netbox-branching", "ci-branching"),
            ("test-netbox-main.yaml", "test-netbox-main", "Install NetBox and plugin", "ci-tests"),
            ("lint-format.yaml", "format-and-lint", "Install dependencies", "lint"),
            ("lint-format.yaml", "format-and-lint", "Check the release configuration", "release"),
        ]
        for filename, job, name, group in installers:
            with self.subTest(workflow=filename, job=job, step=name):
                workflow = yaml.safe_load(
                    (_PROJECT_ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
                )
                step = next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == name)
                commands = step["run"].replace("\\\n", " ").splitlines()
                exports = [line.strip() for line in commands if line.strip().startswith("uv export ")]
                self.assertEqual(len(exports), 1)
                export, separator, install = exports[0].partition("|")
                self.assertEqual(separator, "|")
                export_args = shlex.split(export)
                install_args = shlex.split(install)
                for option in ("--system-certs", "--locked", "--no-emit-project"):
                    self.assertIn(option, export_args)
                if name == "Install NetBox and plugin":
                    self.assertEqual(export_args[export_args.index("--group") + 1], group)
                    self.assertIn("--no-default-groups", export_args)
                    self.assertNotIn("--only-group", export_args)
                    editable = next(line for line in commands if "-e ." in line)
                    self.assertIn("--no-deps", shlex.split(editable))
                else:
                    self.assertEqual(export_args[export_args.index("--only-group") + 1], group)
                self.assertEqual(export_args[export_args.index("--format") + 1], "requirements-txt")
                self.assertEqual(install_args[:3], ["uv", "pip", "install"])
                for option in ("--system-certs", "--system", "--only-binary=:all:"):
                    self.assertIn(option, install_args)
                self.assertEqual(install_args[install_args.index("-r") + 1], "/dev/stdin")
                self.assertEqual(step.get("shell"), "bash", "explicit bash enables pipefail for the export")
                self.assertNotIn("--group", install_args)

    def test_branching_matrix_selects_the_locked_dependency_group(self):
        jobs = _workflow_jobs()
        cells = jobs["test-netbox"]["strategy"]["matrix"]["include"]
        selected = [cell["netbox-branching"] for cell in cells if cell.get("netbox-branching")]
        self.assertTrue(selected)
        self.assertTrue(all(value is True for value in selected))
        pyproject = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertTrue(pyproject["dependency-groups"].get("ci-branching"))


class CoverageCombineWorkflowTest(unittest.TestCase):
    """Every coverage leg must reach the one job that enforces the gate."""

    def setUp(self):
        self.jobs = _workflow_jobs()
        self.test_job = self.jobs["test-netbox"]
        self.coverage_job = self.jobs["coverage"]

    def _combine_run(self):
        return next(step["run"] for step in self.coverage_job["steps"] if step["name"] == "Combine and check coverage")

    def test_the_combine_job_downloads_the_data_of_every_coverage_leg(self):
        legs = [cell["coverage"] for cell in self.test_job["strategy"]["matrix"]["include"] if cell.get("coverage")]
        downloads = [step["with"]["name"] for step in _steps_using(self.coverage_job, "actions/download-artifact")]

        self.assertEqual(len(legs), len(set(legs)), "two coverage legs would upload under one artifact name")
        self.assertEqual(sorted(f"coverage-data-{leg}" for leg in legs), sorted(downloads))
        self.assertEqual(self.coverage_job["needs"], "test-netbox")

    def test_every_coverage_leg_uploads_its_data(self):
        uploads = [step["with"] for step in _steps_using(self.test_job, "actions/upload-artifact")]

        self.assertEqual([upload["name"] for upload in uploads], ["coverage-data-${{ matrix.coverage }}"])
        self.assertEqual(uploads[0]["if-no-files-found"], "error")

    def test_the_combine_step_reads_every_downloaded_file(self):
        combine = self._combine_run()

        for step in _steps_using(self.coverage_job, "actions/download-artifact"):
            with self.subTest(artifact=step["with"]["name"]):
                self.assertIn(f"../{step['with']['path']}/.coverage", combine)

    def test_only_the_combine_job_uploads_to_codecov(self):
        uploads = {name: len(_steps_using(job, "codecov/codecov-action")) for name, job in self.jobs.items()}

        self.assertEqual({name: count for name, count in uploads.items() if count}, {"coverage": 1})

    def test_only_the_combined_report_enforces_the_exact_gate(self):
        pyproject = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        combine = self._combine_run()

        self.assertEqual(pyproject["tool"]["coverage"]["report"]["fail_under"], 97)
        self.assertEqual(pyproject["tool"]["coverage"]["report"]["precision"], 2)
        self.assertIn("--cov-fail-under=0", pyproject["tool"]["pytest"]["ini_options"]["addopts"].split())
        self.assertIn("coverage report\n", combine)
        self.assertNotIn("--fail-under", combine)
