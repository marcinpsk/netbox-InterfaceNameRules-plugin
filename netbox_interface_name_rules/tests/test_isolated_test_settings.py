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

# Read the settings module named on the command line, so the shim can be compared with the
# NetBox settings it wraps.
_PROBE = """
import importlib
import json
import sys

module = importlib.import_module(sys.argv[1])
print(json.dumps({
    "test_db_name": module.DATABASES["default"].get("TEST", {}).get("NAME"),
    "queue_databases": sorted({params["DB"] for params in module.RQ_QUEUES.values()}),
    "default_queue_database": module.RQ_QUEUES["default"]["DB"],
}))
"""


class IsolatedTestSettingsTest(TestCase):
    """Assemble the shim the way Django does and read back what it isolated."""

    # The suite's own settings module reaches the child through these, and Django would load it
    # lazily instead of the module under test.
    _SUITE_VARIABLES = (
        "DJANGO_SETTINGS_MODULE",
        "PYTEST_XDIST_WORKER",
        "REDIS_HOST",
        "REDIS_CACHE_HOST",
        "REDIS_DATABASE",
        "REDIS_CACHE_DATABASE",
    )

    def _load(self, module, environment):
        """Import *module* in a clean interpreter and return the settings it produced."""
        env = {**os.environ, **environment}
        for key in self._SUITE_VARIABLES:
            env.pop(key, None)
        for key, value in environment.items():
            if value is None:
                env.pop(key, None)
        # Hand the child this interpreter's own import path. NetBox lives in a different place in
        # the devcontainer than in CI, and neither location may be assumed here.
        env["PYTHONPATH"] = os.pathsep.join([str(CONFIG_DIR), *(entry for entry in sys.path if entry)])
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE, module],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        # Report the child's own error: a CalledProcessError would hide why the import failed.
        assert completed.returncode == 0, f"{module} failed to import:\n{completed.stderr[-2000:]}"
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_it_isolates_the_task_queue_redis_database(self):
        settings = self._load("isolated_test_settings", {"TEST_DB_NAME": "inr_probe", "TEST_REDIS_DB": "9"})

        self.assertEqual(settings["test_db_name"], "inr_probe")
        self.assertEqual(settings["queue_databases"], [9])

    def test_it_leaves_the_task_queue_alone_without_the_variable(self):
        environment = {"TEST_DB_NAME": "inr_probe", "TEST_REDIS_DB": None}
        baseline = self._load("netbox.settings", environment)

        settings = self._load("isolated_test_settings", environment)

        self.assertEqual(settings["default_queue_database"], baseline["default_queue_database"])
        self.assertEqual(settings["queue_databases"], baseline["queue_databases"])
