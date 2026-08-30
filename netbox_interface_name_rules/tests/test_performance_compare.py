# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the performance artifact comparison script."""

import fnmatch
import importlib.util
import tomllib
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMPARE_PATH = _PROJECT_ROOT / "performance" / "compare.py"
_spec = importlib.util.spec_from_file_location("performance_compare", _COMPARE_PATH)
compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare)


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
            "database": {"statements": [], "totals": {"statement_calls": 31}},
            "machine_time": machine_time,
        }
    ]
    return artifact


_MACHINE_TIME = {"wall": {"median_ms": 2.0, "p95_ms": 3.0}, "process_cpu": {"median_ms": 1.0}}


class PerformancePackageTest(unittest.TestCase):
    """The shared performance contract ships with the plugin distribution."""

    def test_package_discovery_includes_the_performance_tools(self):
        configuration = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text())
        patterns = configuration["tool"]["setuptools"]["packages"]["find"]["include"]

        self.assertTrue(any(fnmatch.fnmatchcase("performance", pattern) for pattern in patterns))


class ArtifactValidationTest(unittest.TestCase):
    """The comparison refuses an artifact it cannot interpret completely."""

    def test_the_current_artifact_shape_is_accepted(self):
        compare.validate_artifact(_timed_artifact(_MACHINE_TIME), "before artifact")

    def test_a_newer_schema_version_is_rejected(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["schema_version"] = 2

        with self.assertRaisesRegex(ValueError, "schema_version"):
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

    def test_a_fractional_statement_call_count_is_rejected(self):
        artifact = _timed_artifact(_MACHINE_TIME)
        artifact["scenarios"][0]["database"]["statements"] = [{"normalized_sql": "SELECT * FROM example", "calls": 1.5}]

        with self.assertRaisesRegex(ValueError, "calls"):
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
                "| `module.direct_callback.plain_rename` | Wall median (ms) | not measured | 2 | not measured | n/a |",
                "| `module.direct_callback.plain_rename` | Wall p95 (ms) | not measured | 3 | not measured | n/a |",
                "| `module.direct_callback.plain_rename` | CPU median (ms) | not measured | 1 | not measured | n/a |",
            ],
        )

    def test_measured_machine_time_is_still_compared(self):
        rows = compare._time_table(
            {"module.direct_callback.plain_rename": _timed_artifact(_MACHINE_TIME)["scenarios"][0]},
            {"module.direct_callback.plain_rename": _timed_artifact(_MACHINE_TIME)["scenarios"][0]},
        )

        self.assertIn("Wall median (ms)", "".join(rows))


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
