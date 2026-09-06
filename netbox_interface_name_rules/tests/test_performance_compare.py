# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the performance artifact comparison script."""

import fnmatch
import importlib.util
import re
import tomllib
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMPARE_PATH = _PROJECT_ROOT / "performance" / "compare.py"
_spec = importlib.util.spec_from_file_location("performance_compare", _COMPARE_PATH)
compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare)


def _unwrapped(text):
    """Return *text* with hard line wrapping collapsed, so assertions match across line breaks."""
    return re.sub(r"\s+", " ", text)


def _artifact(planner_settings, load=None):
    """Return the smallest artifact the environment table reads."""
    return {
        "schema_version": 1,
        "baseline_kind": "test",
        "generated_at": "2026-08-30T00:00:00+00:00",
        "environment": {
            "host_load": load,
            "plugin_revision": "abc123",
            "netbox_revision": "def456",
            "netbox_version": "4.7",
            "cpu_model": "Test CPU",
            "operating_system_release": "test-release",
            "postgresql": {"server_version": "18.3", "planner_settings": planner_settings},
        },
        "configuration": {"samples": 15, "warmups": 3},
    }


def _timed_artifact(machine_time):
    """Return an artifact whose single scenario carries *machine_time*."""
    artifact = _artifact({"work_mem": "4MB"})
    artifact["scenarios"] = [
        {
            "name": "module.direct_callback.plain_rename",
            "layer": "direct_callback",
            "database": {
                "statements": [{"normalized_sql": 'SELECT * FROM "dcim_interface"', "calls": 31}],
                "totals": {"statement_calls": 31},
            },
            "machine_time": machine_time,
        }
    ]
    return artifact


_MACHINE_TIME = {"wall": {"median_ms": 2.0, "p95_ms": 3.0}, "process_cpu": {"median_ms": 1.0}}


class PerformancePackageTest(unittest.TestCase):
    """The repository-only performance harness does not ship as a generic package."""

    def test_package_discovery_excludes_the_performance_tools(self):
        configuration = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text())
        patterns = configuration["tool"]["setuptools"]["packages"]["find"]["include"]

        self.assertFalse(any(fnmatch.fnmatchcase("performance", pattern) for pattern in patterns))

    def test_pytest_adds_the_repository_checkout_to_pythonpath(self):
        configuration = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text())

        self.assertIn(".", configuration["tool"]["pytest"]["ini_options"]["pythonpath"])

    def test_recorded_direct_callbacks_reach_zero_shared_reads(self):
        comparison = (_PROJECT_ROOT / "performance" / "comparisons" / "family-package-vs-existing.md").read_text()
        expected_scenarios = {
            "module.direct_callback.no_matching_rule",
            "module.direct_callback.plain_rename",
            "module.direct_callback.structural_creation",
            "module.direct_callback.existing_family",
            "module.direct_callback.reconciliation",
            "vc.direct_callback.reapply_1",
            "vc.direct_callback.reapply_8",
        }
        rows = [
            [cell.strip().strip("`") for cell in line.strip("|\n").split("|")]
            for line in comparison.splitlines()
            if line.startswith("|")
        ]
        direct_reads = [row for row in rows if ".direct_callback." in row[0] and row[1] == "Shared reads"]

        self.assertEqual(len(direct_reads), 7)
        self.assertEqual({row[0] for row in direct_reads}, expected_scenarios)
        self.assertEqual({row[3] for row in direct_reads}, {"0"})

    def test_readme_does_not_equate_shared_reads_with_all_disk_io(self):
        readme = (_PROJECT_ROOT / "performance" / "README.md").read_text()

        self.assertNotIn("never goes to disk", readme)

    def test_readme_does_not_infer_planner_cost_from_statement_counts(self):
        readme = (_PROJECT_ROOT / "performance" / "README.md").read_text()

        self.assertNotIn("less planner work", readme)
        self.assertIn(
            "so this report does not compare the planner work of those reads",
            _unwrapped(readme),
        )

    def test_readme_shared_read_claim_matches_the_comparison(self):
        """Read the recorded shared reads rather than pinning what one pair of runs happened to show."""
        comparison = (_PROJECT_ROOT / "performance" / "comparisons" / "family-package-vs-existing.md").read_text()
        before_reads = after_reads = 0
        for line in comparison.splitlines():
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if len(cells) == 6 and cells[1] == "Shared reads" and ".direct_callback." in cells[0]:
                before_reads += int(cells[2])
                after_reads += int(cells[3])
        readme = _unwrapped((_PROJECT_ROOT / "performance" / "README.md").read_text())

        self.assertNotIn("No shared-buffer reads were observed in any direct-callback scenario.", readme)
        scoped_claim = "No shared-buffer reads were observed in any direct-callback scenario after the refactor"
        if after_reads:
            self.assertNotIn(scoped_claim, readme)
        else:
            self.assertIn(scoped_claim, readme)
        before_claim = "the before run recorded none either"
        if before_reads:
            self.assertNotIn(before_claim, readme)
        else:
            self.assertIn(before_claim, readme)

    def test_comparison_separates_deterministic_counts_from_cache_metrics(self):
        comparison = (_PROJECT_ROOT / "performance" / "comparisons" / "family-package-vs-existing.md").read_text()
        introduction = comparison.split("## Environment", 1)[0]

        self.assertIn(compare._COMPARISON_INTRO, introduction)
        self.assertIn("SQL statement counts are deterministic for equivalent inputs", introduction)
        self.assertIn(
            "Cache metrics depend on PostgreSQL buffer-cache state and concurrent activity",
            introduction,
        )
        self.assertNotIn("Database work is deterministic", introduction)

    def test_comparison_machine_time_note_matches_the_load_it_reports(self):
        comparison = (_PROJECT_ROOT / "performance" / "comparisons" / "family-package-vs-existing.md").read_text()
        environment = comparison.split("## Environment", 1)[1].split("## Database work", 1)[0]
        machine_time = comparison.split("## Machine time", 1)[1].split("## Statement-count regressions", 1)[0]
        load_row = next(row for row in environment.splitlines() if row.startswith("| host load"))
        samples = [float(value) for value in re.findall(r"\d+\.\d+", load_row)]

        self.assertEqual(len(samples), 4)
        quiet = max(samples) < compare._COMPARABLE_ONE_MINUTE_LOAD
        expected = compare._MACHINE_TIME_COMPARABLE_NOTE if quiet else compare._MACHINE_TIME_UNPROVEN_NOTE

        self.assertIn(expected, machine_time)


class ArtifactValidationTest(unittest.TestCase):
    """The comparison refuses an artifact it cannot interpret completely."""

    def test_the_current_artifact_shape_is_accepted(self):
        compare.validate_artifact(_timed_artifact(_MACHINE_TIME), "before artifact")

    def test_an_artifact_with_no_scenarios_is_rejected(self):
        """An empty run would otherwise render header-only tables and report no regressions."""
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["scenarios"] = []

        with self.assertRaisesRegex(ValueError, "scenarios"):
            compare.validate_artifact(artifact, "before artifact")

    def test_a_newer_schema_version_is_rejected(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["schema_version"] = 2

        with self.assertRaisesRegex(ValueError, "schema_version"):
            compare.validate_artifact(artifact, "before artifact")

    def test_schema_version_requires_an_integer(self):
        for version in (True, 1.0):
            artifact = _timed_artifact(_MACHINE_TIME)
            artifact["schema_version"] = version

            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "schema_version"):
                compare.validate_artifact(artifact, "before artifact")

    def test_a_missing_required_environment_field_is_rejected(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        del artifact["environment"]["plugin_revision"]

        with self.assertRaisesRegex(ValueError, "plugin_revision"):
            compare.validate_artifact(artifact, "before artifact")

    def test_a_non_numeric_database_metric_is_rejected(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["scenarios"][0]["database"]["totals"]["planner_total_cost"] = "not a number"

        with self.assertRaisesRegex(ValueError, "planner_total_cost"):
            compare.validate_artifact(artifact, "before artifact")

    def test_a_negative_database_metric_is_rejected(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["scenarios"][0]["database"]["totals"]["planner_total_cost"] = -1.0

        with self.assertRaisesRegex(ValueError, "non-negative"):
            compare.validate_artifact(artifact, "before artifact")

    def test_a_fractional_statement_call_count_is_rejected(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["scenarios"][0]["database"]["statements"] = [{"normalized_sql": "SELECT * FROM example", "calls": 1.5}]

        with self.assertRaisesRegex(ValueError, "calls"):
            compare.validate_artifact(artifact, "before artifact")

    def test_statement_call_total_must_match_the_statement_entries(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["scenarios"][0]["database"]["totals"]["statement_calls"] = 30

        with self.assertRaisesRegex(ValueError, "statement_calls"):
            compare.validate_artifact(artifact, "before artifact")

    def test_whitespace_only_normalized_sql_is_rejected(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["scenarios"][0]["database"]["statements"][0]["normalized_sql"] = " "

        with self.assertRaisesRegex(ValueError, "normalized_sql"):
            compare.validate_artifact(artifact, "before artifact")


class TimeTableTest(unittest.TestCase):
    """A comparison must not invent machine time that a run did not measure."""

    def test_unmeasured_machine_time_is_reported_not_crashed(self):
        rows = compare._time_table(
            {"module.direct_callback.plain_rename": _timed_artifact(None)["scenarios"][0]},
            {"module.direct_callback.plain_rename": _timed_artifact(_MACHINE_TIME)["scenarios"][0]},
        )

        self.assertEqual(
            rows[-3:],
            [
                "| `module.direct_callback.plain_rename` | Wall median (ms) | not measured | 2 | n/a | n/a |",
                "| `module.direct_callback.plain_rename` | Wall p95 (ms) | not measured | 3 | n/a | n/a |",
                "| `module.direct_callback.plain_rename` | CPU median (ms) | not measured | 1 | n/a | n/a |",
            ],
        )

    def test_measured_machine_time_is_still_compared(self):
        rows = compare._time_table(
            {"module.direct_callback.plain_rename": _timed_artifact(_MACHINE_TIME)["scenarios"][0]},
            {"module.direct_callback.plain_rename": _timed_artifact(_MACHINE_TIME)["scenarios"][0]},
        )

        self.assertIn("Wall median (ms)", "".join(rows))

    def test_an_after_only_scenario_reports_its_missing_baseline(self):
        scenario = _timed_artifact(_MACHINE_TIME)["scenarios"][0]

        rows = compare._time_table({}, {scenario["name"]: scenario})

        self.assertIn(
            "| `module.direct_callback.plain_rename` | Wall median (ms) | missing baseline | 2 | n/a | n/a |",
            rows,
        )


class DatabaseTableTest(unittest.TestCase):
    """A comparison must show every database metric present on either side."""

    def test_a_metric_missing_from_one_artifact_is_reported(self):
        before_scenario = _timed_artifact(None)["scenarios"][0]
        after_scenario = _timed_artifact(None)["scenarios"][0]
        before_scenario["database"]["totals"]["planner_total_cost"] = 12.5

        rows, _regressions = compare._database_table(
            {before_scenario["name"]: before_scenario},
            {after_scenario["name"]: after_scenario},
        )

        self.assertIn(
            "| `module.direct_callback.plain_rename` | Planner cost | 12.50 | n/a | n/a | n/a |",
            rows,
        )

    def test_an_after_only_scenario_reports_its_missing_baseline(self):
        scenario = _timed_artifact(None)["scenarios"][0]

        rows, _regressions = compare._database_table({}, {scenario["name"]: scenario})

        self.assertIn(
            "| `module.direct_callback.plain_rename` | SQL calls | missing baseline | 31 | n/a | n/a |",
            rows,
        )

    def test_a_statement_increase_is_reported_as_a_regression(self):
        before_scenario = _timed_artifact(None)["scenarios"][0]
        after_scenario = _timed_artifact(None)["scenarios"][0]
        after_scenario["database"]["totals"]["statement_calls"] = 32

        _rows, regressions = compare._database_table(
            {before_scenario["name"]: before_scenario},
            {after_scenario["name"]: after_scenario},
        )

        self.assertEqual(regressions, [(before_scenario["name"], "SQL calls", 31, 32)])


class StatementAttributionTest(unittest.TestCase):
    """Statement attribution distinguishes tables from transaction control."""

    def test_transaction_control_is_not_labeled_as_a_table(self):
        before = {"database": {"statements": []}}
        after = {
            "database": {
                "statements": [
                    {"normalized_sql": 'SELECT * FROM "dcim_interface"', "calls": 1},
                    {"normalized_sql": 'SAVEPOINT "s1_x1"', "calls": 1},
                ]
            }
        }

        rows = compare._attribution("scenario", before, after)

        self.assertEqual(
            rows,
            [
                "| `scenario` | `dcim_interface` | 0 | 1 | +1 |",
                "| `scenario` | `transaction: SAVEPOINT` | 0 | 1 | +1 |",
            ],
        )


class EnvironmentTableTest(unittest.TestCase):
    """Exercise the environment table the comparison writes."""

    def _planner_row(self, before, after):
        rows = compare._environment_table(before, after)
        return next(row for row in rows if row.startswith("| planner settings |"))

    def test_identical_planner_settings_fill_both_columns(self):
        settings = {"work_mem": "4MB"}

        row = self._planner_row(_artifact(settings), _artifact(dict(settings)))

        self.assertEqual(row, "| planner settings | `identical` | `identical` |")

    def test_changed_planner_settings_fill_both_columns(self):
        row = self._planner_row(_artifact({"work_mem": "4MB"}), _artifact({"work_mem": "64MB"}))

        self.assertEqual(row, "| planner settings | `CHANGED` | `CHANGED` |")

    def _load_row(self, before, after):
        rows = compare._environment_table(before, after)
        return next(row for row in rows if row.startswith("| host load"))

    def test_recorded_host_load_is_reported_for_both_runs(self):
        settings = {"work_mem": "4MB"}
        quiet = {"started": {"one_minute": 0.5}, "finished": {"one_minute": 0.9}}
        busy = {"started": {"one_minute": 31.0}, "finished": {"one_minute": 44.25}}

        row = self._load_row(_artifact(settings, busy), _artifact(settings, quiet))

        self.assertEqual(row, "| host load (1 min, start to end) | `31.00 to 44.25` | `0.50 to 0.90` |")

    def test_unrecorded_host_load_is_named_rather_than_left_blank(self):
        settings = {"work_mem": "4MB"}
        quiet = {"started": {"one_minute": 0.5}, "finished": {"one_minute": 0.9}}

        row = self._load_row(_artifact(settings, None), _artifact(settings, quiet))

        self.assertEqual(row, "| host load (1 min, start to end) | `not recorded` | `0.50 to 0.90` |")


class MachineTimeNoteTest(unittest.TestCase):
    """The machine-time note follows the 1-minute load both runs recorded."""

    def _note(self, before_load, after_load):
        settings = {"work_mem": "4MB"}
        return compare._machine_time_note(_artifact(settings, before_load), _artifact(settings, after_load))

    def test_runs_under_the_ceiling_are_called_comparable(self):
        before = {"started": {"one_minute": 1.10}, "finished": {"one_minute": 1.18}}
        after = {"started": {"one_minute": 1.23}, "finished": {"one_minute": 0.99}}

        note = self._note(before, after)

        self.assertEqual(note, compare._MACHINE_TIME_COMPARABLE_NOTE)
        self.assertIn("1-minute load stayed below 2.00", _unwrapped(note))

    def test_one_busy_sample_withholds_the_claim(self):
        quiet = {"started": {"one_minute": 0.5}, "finished": {"one_minute": 0.9}}
        busy = {"started": {"one_minute": 0.5}, "finished": {"one_minute": 7.25}}

        self.assertEqual(self._note(quiet, busy), compare._MACHINE_TIME_UNPROVEN_NOTE)
        self.assertEqual(self._note(busy, quiet), compare._MACHINE_TIME_UNPROVEN_NOTE)

    def test_the_ceiling_itself_is_too_busy(self):
        quiet = {"started": {"one_minute": 0.5}, "finished": {"one_minute": 0.9}}
        at_ceiling = {"started": {"one_minute": 0.5}, "finished": {"one_minute": 2.0}}

        self.assertEqual(self._note(quiet, at_ceiling), compare._MACHINE_TIME_UNPROVEN_NOTE)

    def test_unrecorded_load_withholds_the_claim(self):
        quiet = {"started": {"one_minute": 0.5}, "finished": {"one_minute": 0.9}}

        self.assertEqual(self._note(None, quiet), compare._MACHINE_TIME_UNPROVEN_NOTE)
        self.assertEqual(self._note(quiet, None), compare._MACHINE_TIME_UNPROVEN_NOTE)
