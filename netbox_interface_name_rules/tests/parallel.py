# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Isolation helpers for parallel pytest workers."""

import re

# A stock Redis server serves databases 0 to 15. The devcontainer's own rqworker holds the first
# two, so the worker ceiling is what the rest divides into at two databases per worker.
REDIS_DATABASE_COUNT = 16
RESERVED_REDIS_DATABASES = 2

MAX_PARALLEL_WORKERS = (REDIS_DATABASE_COUNT - RESERVED_REDIS_DATABASES) // 2

_POSTGRES_NAME_LIMIT = 63
_WORKER_ID_PATTERN = re.compile(r"gw(?P<number>\d+)")


def _worker_number(worker_id: str | None) -> int:
    """Return the ordinal of *worker_id*, or 0 for a run that starts no xdist worker."""
    if worker_id is None:
        return 0
    match = _WORKER_ID_PATTERN.fullmatch(worker_id)
    if match is None:
        raise ValueError(f"Unsupported pytest worker ID: {worker_id!r}.")
    number = int(match.group("number"))
    if number >= MAX_PARALLEL_WORKERS:
        raise ValueError(f"At most {MAX_PARALLEL_WORKERS} pytest workers are supported.")
    return number


def isolated_test_database_name(base_name: str, worker_id: str | None) -> str:
    """Return a PostgreSQL-safe test database name for one pytest worker."""
    suffix = f"_{worker_id}" if worker_id else ""
    return f"{base_name[: _POSTGRES_NAME_LIMIT - len(suffix)]}{suffix}"


def isolated_redis_databases(worker_id: str | None) -> tuple[int, int]:
    """Return the private task and cache Redis databases for one pytest worker."""
    tasks = RESERVED_REDIS_DATABASES + _worker_number(worker_id)
    return tasks, tasks + MAX_PARALLEL_WORKERS
