# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Checks for the isolated test-settings shim the devcontainer runs tests with."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / ".devcontainer/config"

_PROBE = """
import json
import isolated_test_settings as shim

print(json.dumps({
    "test_db_name": shim.DATABASES["default"].get("TEST", {}).get("NAME"),
    "queue_databases": sorted({params["DB"] for params in shim.RQ_QUEUES.values()}),
    "default_queue_database": shim.RQ_QUEUES["default"]["DB"],
}))
"""


class IsolatedTestSettingsTest(TestCase):
    """Assemble the shim the way Django does and read back what it isolated."""

    def _load(self, environment):
        """Import the shim in a clean interpreter and return the settings it produced."""
        env = {**os.environ, **environment}
        for key, value in environment.items():
            if value is None:
                env.pop(key, None)
        env["PYTHONPATH"] = os.pathsep.join([str(CONFIG_DIR), "/opt/netbox/netbox"])
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_it_isolates_the_task_queue_redis_database(self):
        settings = self._load({"TEST_DB_NAME": "inr_probe", "TEST_REDIS_DB": "9"})

        self.assertEqual(settings["test_db_name"], "inr_probe")
        self.assertEqual(settings["queue_databases"], [9])

    def test_it_leaves_the_task_queue_alone_without_the_variable(self):
        settings = self._load({"TEST_DB_NAME": "inr_probe", "TEST_REDIS_DB": None})

        self.assertEqual(settings["default_queue_database"], 0)
