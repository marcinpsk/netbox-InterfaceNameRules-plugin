# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Root pytest configuration for shared-host parallelism.

pytest loads this file before pytest-xdist resolves the `-n auto` in the addopts, and it loads
`netbox_interface_name_rules/tests/conftest.py` only during collection, after the workers start. The
worker cap therefore has to live here to reach an invocation that names no test path.
"""

import pytest

from netbox_interface_name_rules.tests.parallel import MAX_PARALLEL_WORKERS


def pytest_xdist_auto_num_workers(config):
    """Cap the worker count that pytest-xdist detects for ``-n auto``."""
    from xdist.plugin import pytest_xdist_auto_num_workers as detected_num_workers

    return min(detected_num_workers(config), MAX_PARALLEL_WORKERS)


def _requested_worker_count(config):
    """Use xdist's resolved specs so the refusal counts workers the way xdist does."""
    from xdist.workermanage import parse_tx_spec_config

    return len(parse_tx_spec_config(config))


def pytest_configure(config):
    """Refuse a worker count that cannot get private databases, before any database is created."""
    # The two conditions xdist itself starts workers on, and the list it reads them from.
    if config.getoption("dist", "no") == "no" or config.getoption("collectonly", False):
        return
    if not config.getoption("tx", []):
        return
    requested = _requested_worker_count(config)
    if requested > MAX_PARALLEL_WORKERS:
        raise pytest.UsageError(
            f"{requested} workers exceed the isolation limit: at most {MAX_PARALLEL_WORKERS} "
            "pytest workers get private PostgreSQL and Redis databases."
        )
