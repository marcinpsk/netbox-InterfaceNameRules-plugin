# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
#
# Isolated test settings for the performance runner.
#
# The plugin's pytest suite uses ``netbox_interface_name_rules.tests.isolated_settings``, which
# gives every worker private databases. This shim serves ``manage.py test`` runs only: the
# performance runner measures the plugin under the devcontainer's full plugin list, so narrowing
# it here would change the environment the committed baselines were taken in.
#
# Django names the test database ``test_<DB_NAME>`` (here: ``test_netbox``), so two
# ``manage.py test`` runs in the same devcontainer collide on a single test DB and
# corrupt each other's migrations (and a crashed run can leave an
# ``idle in transaction`` connection holding locks that wedges every later run).
#
# This shim imports the fully-assembled NetBox settings and, when ``TEST_DB_NAME``
# is set, points the test database at that name — so each session/plugin can run on
# its own isolated test DB inside the same Postgres/devcontainer. With no env var it
# falls back to the default ``test_netbox`` and behaves exactly like before.
#
# Use it via ``--settings=isolated_test_settings`` with this directory on PYTHONPATH
# (the ``netbox-test-isolated`` helper in load-aliases.sh wires both up for you).
import os as _os

from netbox.settings import *  # noqa: F401,F403

_name = _os.environ.get("TEST_DB_NAME")
if _name:
    DATABASES["default"].setdefault("TEST", {})["NAME"] = _name  # noqa: F405

# NetBox writes the search cache inline only when no RQ worker serves the queue, so
# ``TEST_REDIS_DB`` moves the queues off the database the devcontainer's worker holds.
_redis_db = _os.environ.get("TEST_REDIS_DB")
if _redis_db:
    for _queue in RQ_QUEUES.values():  # noqa: F405
        _queue["DB"] = int(_redis_db)
