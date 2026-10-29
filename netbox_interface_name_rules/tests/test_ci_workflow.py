# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the coverage wiring of the CI test workflow."""

import re
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


def _workflows():
    for path in sorted((_PROJECT_ROOT / ".github" / "workflows").iterdir()):
        if path.suffix in {".yaml", ".yml"}:
            yield path.name, yaml.safe_load(path.read_text(encoding="utf-8"))


def _path_filter_matches(patterns, path):
    """Match *path* as a GitHub path filter does: the last pattern that matches decides, and ! excludes."""

    def regex(pattern):
        parts = re.split(r"(\*\*/?|\*)", pattern)
        tokens = {"**": ".*", "**/": "(?:.*/)?", "*": "[^/]*"}
        return "".join(tokens.get(part, re.escape(part)) for part in parts)

    selected = False
    for pattern in patterns:
        excluded = pattern.startswith("!")
        if re.fullmatch(regex(pattern.removeprefix("!")), path):
            selected = not excluded
    return selected


class DocsWorkflowTest(unittest.TestCase):
    """The docs build sees each change that the captured media show, and no gh-pages publish is dropped."""

    def test_no_waiting_gh_pages_publish_is_cancelled(self):
        publishers = {
            f"{name}:{job_name}": job["concurrency"]
            for name, workflow in _workflows()
            for job_name, job in workflow["jobs"].items()
            if job.get("concurrency", {}).get("group") == "publish-gh-pages"
        }

        self.assertEqual(sorted(publishers), ["coverage-badge.yaml:publish-coverage", "docs-deploy.yaml:deploy"])
        for publisher, concurrency in publishers.items():
            with self.subTest(publisher=publisher):
                # The default queue keeps one waiting job, so a new one cancels it.
                self.assertEqual(
                    concurrency, {"group": "publish-gh-pages", "cancel-in-progress": False, "queue": "max"}
                )

    def test_a_pull_request_that_changes_a_captured_view_builds_the_docs(self):
        workflow = dict(_workflows())["docs-build.yaml"]
        patterns = workflow["on"]["pull_request"]["paths"]

        for path in (
            "netbox_interface_name_rules/forms.py",
            "netbox_interface_name_rules/tables.py",
            "netbox_interface_name_rules/views.py",
            "netbox_interface_name_rules/models.py",
            "netbox_interface_name_rules/engine.py",
            "netbox_interface_name_rules/family/plans.py",
            "netbox_interface_name_rules/templates/netbox_interface_name_rules/rule_apply.html",
            "docs_media/test_recordings.py",
            "docs/examples.md",
            "contrib/juniper.yaml",
            "scripts/load_demo_data.py",
            "mkdocs.yml",
            "README.md",
            "uv.lock",
        ):
            with self.subTest(path=path):
                self.assertTrue(_path_filter_matches(patterns, path))

    def test_a_pull_request_that_changes_only_the_plugin_tests_skips_the_docs_build(self):
        workflow = dict(_workflows())["docs-build.yaml"]
        patterns = workflow["on"]["pull_request"]["paths"]

        for path in (
            "netbox_interface_name_rules/tests/test_views.py",
            "netbox_interface_name_rules/tests/query_counts.json",
        ):
            with self.subTest(path=path):
                self.assertFalse(_path_filter_matches(patterns, path))
