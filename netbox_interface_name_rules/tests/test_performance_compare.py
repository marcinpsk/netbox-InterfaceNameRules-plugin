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
