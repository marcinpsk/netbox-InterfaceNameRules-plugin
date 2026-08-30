# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the performance artifact comparison script."""

import importlib.util
import unittest
from pathlib import Path

_COMPARE_PATH = Path(__file__).resolve().parents[2] / "performance" / "compare.py"
_spec = importlib.util.spec_from_file_location("performance_compare", _COMPARE_PATH)
compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare)


def _artifact(planner_settings, load=None):
    """Return the smallest artifact the environment table reads."""
    return {
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
            "database": {"totals": {"statement_calls": 31}},
            "machine_time": machine_time,
        }
    ]
    return artifact


_MACHINE_TIME = {"wall": {"median_ms": 2.0, "p95_ms": 3.0}, "process_cpu": {"median_ms": 1.0}}


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
