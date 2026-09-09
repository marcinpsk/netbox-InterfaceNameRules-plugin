# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Root pytest configuration for shared-host parallelism."""

MAX_PARALLEL_WORKERS = 8


def pytest_xdist_auto_num_workers(config):
    """Cap the worker count that pytest-xdist detects for ``-n auto``."""
    from xdist.plugin import pytest_xdist_auto_num_workers as detected_num_workers

    return min(detected_num_workers(config), MAX_PARALLEL_WORKERS)
