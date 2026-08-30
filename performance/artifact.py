# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Define and validate the performance artifact boundary."""

from collections.abc import Mapping
from math import isfinite
from typing import Any

SCHEMA_VERSION = 1
DATABASE_METRICS = (
    ("statement_calls", "SQL calls"),
    ("planner_total_cost", "Planner cost"),
    ("shared_hit_blocks", "Shared hits"),
    ("shared_read_blocks", "Shared reads"),
    ("wal_bytes", "WAL bytes"),
)


def _invalid(source: str, path: str, expectation: str) -> ValueError:
    """Return a consistent artifact validation error."""
    return ValueError(f"{source}: {path} {expectation}")


def _mapping(value: Any, source: str, path: str) -> Mapping[str, Any]:
    """Require one mapping value."""
    if not isinstance(value, Mapping):
        raise _invalid(source, path, "must be an object")
    return value


def _list(value: Any, source: str, path: str) -> list[Any]:
    """Require one list value."""
    if not isinstance(value, list):
        raise _invalid(source, path, "must be a list")
    return value


def _required(mapping: Mapping[str, Any], key: str, source: str, path: str) -> Any:
    """Return a required field from one mapping."""
    if key not in mapping:
        raise _invalid(source, f"{path}.{key}", "is required")
    return mapping[key]


def _string(value: Any, source: str, path: str) -> str:
    """Require one non-empty string value."""
    if not isinstance(value, str) or not value:
        raise _invalid(source, path, "must be a non-empty string")
    return value


def _finite_number(value: Any, source: str, path: str) -> int | float:
    """Require one non-negative finite JSON number value."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value) or value < 0:
        raise _invalid(source, path, "must be a non-negative finite number")
    return value


def _non_negative_integer(value: Any, source: str, path: str) -> int:
    """Require one non-negative integer value."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(source, path, "must be a non-negative integer")
    return value


def _validate_host_load(environment: Mapping[str, Any], source: str) -> None:
    """Validate optional host-load evidence."""
    host_load = environment.get("host_load")
    if host_load is None:
        return
    host_load = _mapping(host_load, source, "environment.host_load")
    for phase in ("started", "finished"):
        load = _mapping(
            _required(host_load, phase, source, "environment.host_load"),
            source,
            f"environment.host_load.{phase}",
        )
        _finite_number(
            _required(load, "one_minute", source, f"environment.host_load.{phase}"),
            source,
            f"environment.host_load.{phase}.one_minute",
        )


def _validate_environment(artifact: Mapping[str, Any], source: str) -> None:
    """Validate the environment fields read by comparisons."""
    environment = _mapping(_required(artifact, "environment", source, "artifact"), source, "environment")
    for field in (
        "plugin_revision",
        "netbox_revision",
        "netbox_version",
        "cpu_model",
        "operating_system_release",
    ):
        _string(_required(environment, field, source, "environment"), source, f"environment.{field}")
    postgresql_path = "environment.postgresql"
    postgresql = _mapping(_required(environment, "postgresql", source, "environment"), source, postgresql_path)
    _string(
        _required(postgresql, "server_version", source, postgresql_path),
        source,
        f"{postgresql_path}.server_version",
    )
    _mapping(
        _required(postgresql, "planner_settings", source, postgresql_path),
        source,
        f"{postgresql_path}.planner_settings",
    )
    _validate_host_load(environment, source)


def _validate_machine_time(value: Any, source: str, path: str) -> None:
    """Validate optional wall-clock and process-time evidence."""
    if value is None:
        return
    machine_time = _mapping(value, source, path)
    wall = _mapping(_required(machine_time, "wall", source, path), source, f"{path}.wall")
    process_cpu = _mapping(
        _required(machine_time, "process_cpu", source, path),
        source,
        f"{path}.process_cpu",
    )
    for field in ("median_ms", "p95_ms"):
        _finite_number(_required(wall, field, source, f"{path}.wall"), source, f"{path}.wall.{field}")
    _finite_number(
        _required(process_cpu, "median_ms", source, f"{path}.process_cpu"),
        source,
        f"{path}.process_cpu.median_ms",
    )


def _validate_scenarios(artifact: Mapping[str, Any], source: str) -> None:
    """Validate scenario identities and the database evidence used by comparisons."""
    scenarios = _list(_required(artifact, "scenarios", source, "artifact"), source, "scenarios")
    names: set[str] = set()
    for index, value in enumerate(scenarios):
        path = f"scenarios[{index}]"
        scenario = _mapping(value, source, path)
        name = _string(_required(scenario, "name", source, path), source, f"{path}.name")
        if name in names:
            raise _invalid(source, f"{path}.name", f"duplicates {name!r}")
        names.add(name)
        _string(_required(scenario, "layer", source, path), source, f"{path}.layer")

        database = _mapping(_required(scenario, "database", source, path), source, f"{path}.database")
        totals = _mapping(
            _required(database, "totals", source, f"{path}.database"),
            source,
            f"{path}.database.totals",
        )
        for field, _label in DATABASE_METRICS:
            if field == "statement_calls":
                _non_negative_integer(
                    _required(totals, field, source, f"{path}.database.totals"),
                    source,
                    f"{path}.database.totals.{field}",
                )
            elif field in totals:
                _finite_number(totals[field], source, f"{path}.database.totals.{field}")
        statements = _list(
            _required(database, "statements", source, f"{path}.database"),
            source,
            f"{path}.database.statements",
        )
        for statement_index, statement_value in enumerate(statements):
            statement_path = f"{path}.database.statements[{statement_index}]"
            statement = _mapping(statement_value, source, statement_path)
            _string(
                _required(statement, "normalized_sql", source, statement_path),
                source,
                f"{statement_path}.normalized_sql",
            )
            _non_negative_integer(
                _required(statement, "calls", source, statement_path),
                source,
                f"{statement_path}.calls",
            )

        _validate_machine_time(_required(scenario, "machine_time", source, path), source, f"{path}.machine_time")


def validate_artifact(value: Any, source: str = "performance artifact") -> None:
    """Reject a performance artifact that this code cannot interpret."""
    artifact = _mapping(value, source, "artifact")
    version = _required(artifact, "schema_version", source, "artifact")
    if version != SCHEMA_VERSION:
        raise _invalid(source, "artifact.schema_version", f"must be {SCHEMA_VERSION}, got {version!r}")
    _string(_required(artifact, "baseline_kind", source, "artifact"), source, "artifact.baseline_kind")
    _string(_required(artifact, "generated_at", source, "artifact"), source, "artifact.generated_at")

    configuration = _mapping(
        _required(artifact, "configuration", source, "artifact"),
        source,
        "configuration",
    )
    for field in ("samples", "warmups"):
        _non_negative_integer(
            _required(configuration, field, source, "configuration"),
            source,
            f"configuration.{field}",
        )

    _validate_environment(artifact, source)
    _validate_scenarios(artifact, source)
