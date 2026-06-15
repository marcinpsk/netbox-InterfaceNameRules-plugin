# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
#
# Isolated test-database settings shim.
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
