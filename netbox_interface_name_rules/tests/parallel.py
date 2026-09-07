# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Isolation helpers for parallel pytest workers."""

import re
from urllib.parse import urlsplit, urlunsplit

# A stock Redis server serves databases 0 to 15. The devcontainer's own rqworker holds the first
# two, so the rest divides into slots of one task and one cache database.
REDIS_DATABASE_COUNT = 16
RESERVED_REDIS_DATABASES = 2

_REDIS_SLOT_COUNT = (REDIS_DATABASE_COUNT - RESERVED_REDIS_DATABASES) // 2
# Slot 0 belongs to a serial run, so the worker ceiling is one below the slot count.
MAX_PARALLEL_WORKERS = _REDIS_SLOT_COUNT - 1

_POSTGRES_NAME_LIMIT = 63
_WORKER_ID_PATTERN = re.compile(r"gw(?P<number>\d+)")


def _worker_number(worker_id: str) -> int:
    """Return the ordinal of *worker_id*."""
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


def isolated_cache_location(location: str, host: str, database: int) -> str:
    """Return *location* pointed at *host* and *database*, keeping scheme, credentials and port."""
    parsed = urlsplit(location)
    netloc = f"{parsed.username}:{parsed.password}@" if parsed.username or parsed.password else ""
    netloc += host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, f"/{database}", parsed.query, parsed.fragment))


def isolated_redis_databases(worker_id: str | None) -> tuple[int, int]:
    """Return the private task and cache Redis databases for one pytest worker.

    A serial run takes slot 0, so ``pytest -n 0`` never shares queues or cache entries with an
    xdist worker running against the same Redis host.
    """
    slot = 0 if worker_id is None else _worker_number(worker_id) + 1
    tasks = RESERVED_REDIS_DATABASES + slot
    return tasks, tasks + _REDIS_SLOT_COUNT
