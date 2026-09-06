# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for parallel test worker isolation."""

import os
import re
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from netbox_interface_name_rules.tests.parallel import (
    MAX_PARALLEL_WORKERS,
    RESERVED_REDIS_DATABASES,
    isolated_redis_databases,
    isolated_test_database_name,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _root_conftest():
    """Load the root conftest by path: it is not importable as a package module."""
    spec = spec_from_file_location("interface_name_rules_root_conftest", _PROJECT_ROOT / "conftest.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_empty_pytest(*arguments, timeout=180):
    """Run pytest without collecting this plugin's suite, so only the worker rules apply."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "COV_"))}
    environment["TEST_DB_NAME"] = "test_worker_pool_contract"
    environment["TEST_REDIS_HOST"] = os.environ.get("TEST_REDIS_HOST", "redis")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *arguments,
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "--ignore=netbox_interface_name_rules",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=_PROJECT_ROOT,
        check=False,
        timeout=timeout,
    )


def test_a_worker_gets_one_database_and_two_redis_databases():
    assert isolated_test_database_name("test_inr", "gw3") == "test_inr_gw3"
    assert isolated_redis_databases("gw3") == (
        RESERVED_REDIS_DATABASES + 3,
        RESERVED_REDIS_DATABASES + 3 + MAX_PARALLEL_WORKERS,
    )


def test_the_live_rqworker_databases_are_never_handed_out():
    """The devcontainer's own rqworker and cache hold the first two databases."""
    handed_out = set()
    for worker in [None, *(f"gw{number}" for number in range(MAX_PARALLEL_WORKERS))]:
        handed_out.update(isolated_redis_databases(worker))

    assert handed_out.isdisjoint(range(RESERVED_REDIS_DATABASES))
    assert max(handed_out) < 16


def test_every_worker_pair_is_distinct():
    """Two workers must never share a task or a cache database."""
    pairs = [isolated_redis_databases(f"gw{number}") for number in range(MAX_PARALLEL_WORKERS)]
    assigned = [database for pair in pairs for database in pair]

    assert len(set(assigned)) == len(assigned)


def test_a_serial_run_also_avoids_the_live_databases():
    assert isolated_test_database_name("test_inr", None) == "test_inr"
    assert isolated_redis_databases(None) == (RESERVED_REDIS_DATABASES, RESERVED_REDIS_DATABASES + MAX_PARALLEL_WORKERS)


def test_database_name_stays_within_the_postgresql_limit():
    database_name = isolated_test_database_name(f"test_{'x' * 70}", "gw6")

    assert len(database_name) == 63
    assert database_name.endswith("_gw6")


def test_a_worker_above_the_ceiling_is_rejected():
    with pytest.raises(ValueError, match=f"At most {MAX_PARALLEL_WORKERS} pytest workers"):
        isolated_redis_databases(f"gw{MAX_PARALLEL_WORKERS}")


def test_an_unrecognised_worker_id_is_rejected():
    with pytest.raises(ValueError, match="Unsupported pytest worker ID"):
        isolated_redis_databases("worker-3")


@pytest.mark.parametrize(("detected", "expected"), [("2", 2), ("32", MAX_PARALLEL_WORKERS)])
def test_auto_worker_count_never_exceeds_the_ceiling(monkeypatch, detected, expected):
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", detected)

    assert _root_conftest().pytest_xdist_auto_num_workers(None) == expected


def test_a_bare_run_caps_the_auto_worker_pool():
    """The cap must reach an invocation that names no test path, which the addopts `-n auto` targets."""
    result = _run_empty_pytest("-n", "auto", "-v")

    # `--ignore` leaves nothing to collect, so pytest exits 5.
    assert result.returncode in (0, 5), f"exit {result.returncode}\n{result.stdout[-3000:]}"
    created = re.search(r"created: (\d+)/\d+ workers", result.stdout)
    assert created is not None, result.stdout[-3000:]
    assert 0 < int(created.group(1)) <= MAX_PARALLEL_WORKERS


def test_an_explicit_worker_count_above_the_ceiling_is_refused():
    """`-n 16` never reaches the auto hook, so without the refusal it would start unisolated workers."""
    result = _run_empty_pytest("-n", str(MAX_PARALLEL_WORKERS + 1))

    # 4 is pytest's usage-error status: it must refuse rather than run and collide later.
    assert result.returncode == 4, f"exit {result.returncode}\n{(result.stdout + result.stderr)[-3000:]}"
    assert f"at most {MAX_PARALLEL_WORKERS} pytest workers" in result.stdout + result.stderr


def test_collecting_without_running_is_left_alone():
    """xdist starts no worker for `--collect-only`, so the ceiling has nothing to refuse."""
    result = _run_empty_pytest(
        "-o", "addopts=", "--collect-only", "--tx", f"{MAX_PARALLEL_WORKERS + 1}*popen", "--dist", "load"
    )

    assert result.returncode == 5, f"exit {result.returncode}\n{(result.stdout + result.stderr)[-3000:]}"


@pytest.mark.django_db
def test_the_running_worker_uses_its_private_targets(settings):
    """Apply the worker identity to the real Django settings, not just to the helpers."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    tasks_database, cache_database = isolated_redis_databases(worker_id)

    assert settings.DATABASES["default"]["TEST"]["NAME"] == isolated_test_database_name(
        os.environ["TEST_DB_NAME"], worker_id
    )
    assert settings.RQ_QUEUES["default"]["DB"] == tasks_database
    assert settings.CACHES["default"]["LOCATION"].endswith(f"/{cache_database}")


def test_only_this_plugin_loads_under_the_test_settings(settings):
    """A co-installed plugin must not reach a run of this suite."""
    assert settings.PLUGINS == ["netbox_interface_name_rules"]
