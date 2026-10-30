# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the coverage wiring of the CI test workflow."""

import os
import re
import shutil
import subprocess
import tempfile
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


def _run_commands(script):
    """Return each command of a run script, with its backslash continuations joined."""
    return [line.strip() for line in script.replace("\\\n", " ").splitlines() if line.strip()]


class WorkflowSourceBuildTest(unittest.TestCase):
    """No workflow step lets uv build a package from source, so no setup script runs."""

    def test_every_uv_run_and_uv_sync_refuses_a_source_build(self):
        for name, workflow in _workflows():
            for job_name, job in workflow["jobs"].items():
                for step in job.get("steps", []):
                    for command in _run_commands(step.get("run", "")):
                        if re.search(r"\buv (?:run|sync)\b", command):
                            with self.subTest(workflow=name, job=job_name, command=command):
                                self.assertIn("--no-build", command)


_NETBOX_LOCK = _PROJECT_ROOT / ".github" / "netbox-requirements.txt"


class DocsNetBoxLockTest(unittest.TestCase):
    """The docs build installs NetBox from hash-pinned wheels locked for the NetBox release and Python it uses."""

    def setUp(self):
        self.build = dict(_workflows())["docs-build.yaml"]["jobs"]["build"]["steps"]

    def test_the_lock_names_the_netbox_release_and_the_python_of_the_docs_build(self):
        [ref] = [
            s["with"]["ref"] for s in self.build if s.get("with", {}).get("repository") == "netbox-community/netbox"
        ]
        [python] = [s["with"]["python-version"] for s in self.build if "python-version" in s.get("with", {})]

        header = _NETBOX_LOCK.read_text(encoding="utf-8").splitlines()[0]

        self.assertEqual(header, f"# NetBox {ref} on Python {python}: generated by scripts/lock-netbox-requirements.sh")

    def test_netbox_installs_from_the_lock_with_hashes_and_wheels(self):
        [install] = [s["run"] for s in self.build if s.get("name") == "Install NetBox and plugin"]
        [command] = [c for c in _run_commands(install) if c.startswith("uv pip install") and "requirements.txt" in c]

        self.assertIn("-r .github/netbox-requirements.txt", command)
        self.assertIn("--require-hashes", command)
        self.assertIn("--only-binary=:all:", command)

    def test_a_change_of_the_lock_builds_the_docs(self):
        patterns = dict(_workflows())["docs-build.yaml"]["on"]["pull_request"]["paths"]

        self.assertTrue(_path_filter_matches(patterns, ".github/netbox-requirements.txt"))


# An origin with main, and gh-pages with a coverage report ($GH_PAGES yes) or only ci/gh-pages ($GH_PAGES nested).
_ORIGIN_SETUP = """
git init -q --bare origin.git
git init -q -b main work
cd work
git config user.name t
git config user.email t@example.invalid
git commit -q --allow-empty -m init
git remote add origin ../origin.git
git push -q origin main
if [ "$GH_PAGES" = yes ]; then
  git checkout -q --orphan gh-pages
  mkdir coverage
  echo report > coverage/index.html
  git add coverage
  git commit -q -m coverage
  git push -q origin gh-pages
  git checkout -q main
  rm -rf coverage
elif [ "$GH_PAGES" = nested ]; then
  git push -q origin main:refs/heads/ci/gh-pages
fi
mkdir site
"""


def _bash(script, cwd, env=None):
    """Run *script* as GitHub runs a step without a shell key: bash -e {0}."""
    return subprocess.run(  # noqa: S603 - The script is a workflow step of this repository.
        ["bash", "-e", "-c", script],  # noqa: S607 - Use bash from the test environment PATH.
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class DocsDeployCoverageTest(unittest.TestCase):
    """The deploy keeps the coverage report of gh-pages, and also deploys when gh-pages does not exist."""

    def _deploy_step(self, with_gh_pages, reachable=True, nested=False):
        workflow = dict(_workflows())["docs-deploy.yaml"]
        [script] = [
            s["run"]
            for s in workflow["jobs"]["deploy"]["steps"]
            if s.get("name") == "Keep the coverage report of gh-pages"
        ]
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {**os.environ, "GH_PAGES": "yes" if with_gh_pages else "nested" if nested else "no"}
        setup = _bash(_ORIGIN_SETUP, root, env)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        if not reachable:
            (root / "origin.git").rename(root / "moved.git")
        result = _bash(script, root / "work")
        return result, root / "work" / "site"

    def test_the_deploy_copies_the_coverage_report_of_gh_pages_into_the_site(self):
        result, site = self._deploy_step(with_gh_pages=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((site / "coverage" / "index.html").read_text(encoding="utf-8"), "report\n")

    def test_the_deploy_continues_when_gh_pages_does_not_exist(self):
        result, site = self._deploy_step(with_gh_pages=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((site / "coverage").exists())

    def test_the_deploy_stops_when_it_cannot_read_gh_pages(self):
        result, site = self._deploy_step(with_gh_pages=True, reachable=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((site / "coverage").exists())

    def test_the_deploy_continues_when_only_a_branch_ending_in_gh_pages_exists(self):
        result, site = self._deploy_step(with_gh_pages=False, nested=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((site / "coverage").exists())


# Fakes: curl writes a NetBox requirements.txt only with HTTPS-only redirects; uv fails when $FAKE_UV is "fail".
_FAKE_CURL = """#!/usr/bin/env bash
case " $* " in *" --proto =https "*) ;; *) echo "curl may follow a redirect to plain HTTP" >&2; exit 1;; esac
while [ $# -gt 0 ]; do if [ "$1" = --output ]; then out=$2; shift; fi; shift; done
printf 'Django==6.0\\npsycopg[c,pool]==3.3.5\\n' > "$out"
"""
_FAKE_UV = """#!/usr/bin/env bash
[ "$FAKE_UV" = fail ] && exit 1
echo "django==6.0"
"""


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class LockNetBoxRequirementsScriptTest(unittest.TestCase):
    """scripts/lock-netbox-requirements.sh replaces the lock only with a complete compile."""

    def _lock(self, uv):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "scripts").mkdir()
        script = root / "scripts" / "lock-netbox-requirements.sh"
        script.write_bytes((_PROJECT_ROOT / "scripts" / "lock-netbox-requirements.sh").read_bytes())
        (root / ".github").mkdir()
        (root / ".github" / "netbox-requirements.txt").write_text("old\n", encoding="utf-8")
        (root / "bin").mkdir()
        for name, body in (("curl", _FAKE_CURL), ("uv", _FAKE_UV)):
            (root / "bin" / name).write_text(body, encoding="utf-8")
            (root / "bin" / name).chmod(0o755)
        env = {**os.environ, "PATH": f"{root / 'bin'}:{os.environ['PATH']}", "FAKE_UV": uv}
        result = _bash(f'bash "{script}" v9.9.9 3.14', root, env)
        return result, (root / ".github" / "netbox-requirements.txt").read_text(encoding="utf-8")

    def test_a_compile_writes_the_header_and_the_locked_requirements(self):
        result, lock = self._lock(uv="ok")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            lock, "# NetBox v9.9.9 on Python 3.14: generated by scripts/lock-netbox-requirements.sh\ndjango==6.0\n"
        )

    def test_a_failed_compile_keeps_the_lock(self):
        result, lock = self._lock(uv="fail")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lock, "old\n")
