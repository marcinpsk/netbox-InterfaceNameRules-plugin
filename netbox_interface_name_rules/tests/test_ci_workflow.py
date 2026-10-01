# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the coverage wiring of the CI test workflow."""

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
