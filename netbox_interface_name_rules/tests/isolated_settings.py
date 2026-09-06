# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox test settings that require caller-selected database and Redis targets.

The devcontainer runs a live rqworker and a shared cache on the first two Redis databases, and other
sessions run their own suites against the same server. Every worker therefore takes a private pair
of Redis databases and a private PostgreSQL database instead of the defaults.
"""

import os

from netbox_interface_name_rules.tests.parallel import isolated_redis_databases

_worker_id = os.environ.get("PYTEST_XDIST_WORKER")
_tasks_database, _cache_database = isolated_redis_databases(_worker_id)

_redis_host = os.environ.get("TEST_REDIS_HOST", "").strip()
if not _redis_host:
    raise ValueError("TEST_REDIS_HOST must name the Redis server the tests may use.")

os.environ["REDIS_HOST"] = _redis_host
os.environ["REDIS_CACHE_HOST"] = _redis_host
os.environ["REDIS_DATABASE"] = str(_tasks_database)
os.environ["REDIS_CACHE_DATABASE"] = str(_cache_database)
os.environ.setdefault("NETBOX_CONFIGURATION", "netbox_interface_name_rules.tests.netbox_configuration")

from netbox.settings import *  # noqa: E402, F403

_database_name = os.environ.get("TEST_DB_NAME", "")
if not _database_name.startswith("test_"):
    raise ValueError("TEST_DB_NAME must be set and must start with 'test_'.")

# The worker suffix is applied in the conftest fixture, after xdist resolves the worker identity.
DATABASES["default"].setdefault("TEST", {})["NAME"] = _database_name  # noqa: F405
