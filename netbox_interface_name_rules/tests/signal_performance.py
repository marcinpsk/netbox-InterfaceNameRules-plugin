# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Record the automatic naming signal-path performance baseline.

This module is intentionally outside Django's default ``test*.py`` discovery. Run it by its full
module label when a before/after implementation comparison is needed. The runner uses the real
NetBox models, PostgreSQL connection, Django signals, and committed callbacks.
"""

import contextlib
import gc
import hashlib
import json
import math
import os
import platform
import re
import statistics
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import django
from dcim.choices import InterfaceTypeChoices
from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    InterfaceTemplate,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    Site,
    VirtualChassis,
)
from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase

from netbox_interface_name_rules import __version__ as plugin_version
from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.engine import supports_channelization, supports_vc_position_token
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.signals import _apply_rules_deferred, _apply_rules_for_device_deferred
from performance.artifact import SCHEMA_VERSION, validate_artifact

_OUTPUT_VARIABLE = "INTERFACE_FAMILY_PERFORMANCE_OUTPUT"
_SAMPLE_VARIABLE = "INTERFACE_FAMILY_PERFORMANCE_SAMPLES"
_WARMUP_VARIABLE = "INTERFACE_FAMILY_PERFORMANCE_WARMUPS"
_PLUGIN_REVISION_VARIABLE = "INTERFACE_FAMILY_PERFORMANCE_SOURCE_REVISION"
_NETBOX_REVISION_VARIABLE = "NETBOX_PERFORMANCE_SOURCE_REVISION"
_KIND_VARIABLE = "INTERFACE_FAMILY_PERFORMANCE_KIND"
_DEFAULT_KIND = "existing_implementation"
_DEFAULT_SAMPLES = 15
_DEFAULT_WARMUPS = 3

_CHANNEL_TYPE = getattr(InterfaceTypeChoices, "TYPE_CHANNEL", "channel")
_PARENT_TYPE = InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS
_PLAIN_TYPE = InterfaceTypeChoices.TYPE_10GE_SFP_PLUS

_SPACE_RE = re.compile(r"\s+")
_STRING_LITERAL_RE = re.compile(r"(?i)(?:e|u&)?'(?:''|[^'])*'")
_DOLLAR_LITERAL_RE = re.compile(
    r"\$\$.*?\$\$|\$(?P<tag>[A-Za-z_][A-Za-z_0-9]*)\$.*?\$(?P=tag)\$",
    re.DOTALL,
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z_0-9$])[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?(?![A-Za-z_0-9$])", re.I)
_SAVEPOINT_RE = re.compile(r"s\d+_x\d+")
_DJANGO_CURSOR_RE = re.compile(r"_django_curs_\d+_(sync|async)_\d+")
_AUTO_EXPLAIN_PLAN_RE = re.compile(r"\bplan:\s*(?P<document>[\[{].*)", re.DOTALL)

_PLAN_TIME_KEYS = {
    "Actual Startup Time",
    "Actual Total Time",
    "Execution Time",
    "Planning Time",
}
_PLAN_PRIVATE_KEYS = {"Query Parameters", "Query Text"}
_PLAN_EXPRESSION_KEYS = {
    "Conflict Filter",
    "Filter",
    "Function Call",
    "Hash Cond",
    "Hash Key",
    "Index Cond",
    "Join Filter",
    "Merge Cond",
    "Output",
    "Recheck Cond",
    "Sort Key",
    "TID Cond",
}
_WORK_COUNTER_KEYS = (
    "Shared Hit Blocks",
    "Shared Read Blocks",
    "Shared Dirtied Blocks",
    "Shared Written Blocks",
    "Local Hit Blocks",
    "Local Read Blocks",
    "Local Dirtied Blocks",
    "Local Written Blocks",
    "Temp Read Blocks",
    "Temp Written Blocks",
    "WAL Records",
    "WAL FPI",
    "WAL Bytes",
)
_PLAN_IDENTITY_KEYS = {
    "Alias",
    "Async Capable",
    "Cache Key",
    "Cache Mode",
    "Command",
    "Conflict Arbiter Indexes",
    "Conflict Filter",
    "Conflict Resolution",
    "CTE Name",
    "Custom Plan Provider",
    "Disabled",
    "Filter",
    "Function Call",
    "Function Name",
    "Group Key",
    "Group Keys",
    "Grouping Sets",
    "Hash Cond",
    "Hash Key",
    "Hash Keys",
    "Index Cond",
    "Index Name",
    "Inner Unique",
    "Join Filter",
    "Join Type",
    "Merge Cond",
    "Node Type",
    "One-Time Filter",
    "Operation",
    "Output",
    "Parallel Aware",
    "Parent Relationship",
    "Partial Mode",
    "Plan",
    "Plan Rows",
    "Plan Width",
    "Plans",
    "Presorted Key",
    "Recheck Cond",
    "Relation Name",
    "Relations",
    "Remote SQL",
    "Repeatable Seed",
    "Sampling Method",
    "Sampling Parameters",
    "Scan Direction",
    "Schema",
    "SetOp Command",
    "Single Copy",
    "Sort Key",
    "Startup Cost",
    "Strategy",
    "Subplan Name",
    "Table Function Call",
    "Target Tables",
    "TID Cond",
    "Total Cost",
    "Workers Planned",
}
_PLANNER_SETTINGS = (
    "block_size",
    "cpu_index_tuple_cost",
    "cpu_operator_cost",
    "cpu_tuple_cost",
    "default_statistics_target",
    "effective_cache_size",
    "effective_io_concurrency",
    "jit",
    "max_parallel_workers_per_gather",
    "plan_cache_mode",
    "random_page_cost",
    "seq_page_cost",
    "shared_buffers",
    "work_mem",
)
_PROFILE_MODELS = (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    InterfaceTemplate,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    Site,
    VirtualChassis,
    InterfaceNameRule,
)


@dataclass(frozen=True)
class _PreparedScenario:
    """One prepared fixture and the operation measured against it."""

    operation: Callable[[], None]
    verify: Callable[[], dict[str, Any]]
    cleanup: Callable[[], None]
    fixture: dict[str, int]
    work_units: int


@dataclass(frozen=True)
class _Scenario:
    """A stable scenario definition used by both measurement passes."""

    name: str
    description: str
    layer: str
    prepare: Callable[[], _PreparedScenario]


def _positive_integer_from_environment(name: str, default: int, *, allow_zero: bool = False) -> int:
    """Read an integer run setting and reject values that cannot produce useful evidence."""
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise AssertionError(f"{name} must be an integer, got {raw!r}.") from error
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise AssertionError(f"{name} must be at least {minimum}, got {value}.")
    return value


def _output_path() -> Path:
    """Return the requested artifact path or fail before the expensive measurements start."""
    raw = os.environ.get(_OUTPUT_VARIABLE)
    if not raw:
        raise AssertionError(f"Set {_OUTPUT_VARIABLE} to an absolute .json artifact path.")
    output = Path(raw)
    if not output.is_absolute() or output.suffix != ".json":
        raise AssertionError(f"{_OUTPUT_VARIABLE} must be an absolute path ending in .json.")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _normalize_sql(sql: str) -> str:
    """Remove values and formatting differences from a SQL statement."""
    normalized = _DOLLAR_LITERAL_RE.sub("'?'", str(sql))
    normalized = _STRING_LITERAL_RE.sub("'?'", normalized)
    normalized = _SAVEPOINT_RE.sub("s?_x?", normalized)
    normalized = _DJANGO_CURSOR_RE.sub(r"_django_curs_?_\1_?", normalized)
    normalized = _NUMBER_RE.sub("?", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _fingerprint(value: str) -> str:
    """Return a stable identifier for normalized evidence."""
    return hashlib.sha256(value.encode()).hexdigest()


def _percentile(samples: list[int], percentile: float) -> float:
    """Return an interpolated percentile without a benchmark dependency."""
    ordered = sorted(samples)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summary(samples: list[int]) -> dict[str, Any]:
    """Return raw nanoseconds plus median and p95 values in milliseconds."""
    return {
        "samples_ns": samples,
        "median_ms": statistics.median(samples) / 1_000_000,
        "p95_ms": _percentile(samples, 0.95) / 1_000_000,
    }


def _optional_summary(samples: list[int]) -> dict[str, Any]:
    """Summarize optional warm-up samples while allowing a zero-warm-up smoke run."""
    if not samples:
        return {"samples_ns": []}
    return _summary(samples)


def _sanitize_expression(value: Any) -> Any:
    """Remove literal values from one plan expression or expression list."""
    if isinstance(value, str):
        return _normalize_sql(value)
    if isinstance(value, list):
        return [_sanitize_expression(child) for child in value]
    return value


def _sanitize_plan(value: Any) -> Any:
    """Remove query values and node timing from an auto_explain document."""
    if isinstance(value, dict):
        sanitized = {}
        for key, child in value.items():
            if key in _PLAN_PRIVATE_KEYS or key in _PLAN_TIME_KEYS or key.endswith(" I/O Time"):
                continue
            if key in _PLAN_EXPRESSION_KEYS or key.endswith((" Cond", " Filter", " Key")):
                sanitized[key] = _sanitize_expression(child)
            else:
                sanitized[key] = _sanitize_plan(child)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_plan(child) for child in value]
    return value


def _plan_envelope(document: Any) -> dict[str, Any]:
    """Return the auto_explain envelope from either accepted JSON shape."""
    if isinstance(document, list) and len(document) == 1 and isinstance(document[0], dict):
        return document[0]
    if isinstance(document, dict):
        return document
    raise AssertionError(f"auto_explain returned an unexpected JSON document: {type(document).__name__}.")


def _parse_notice(message: str) -> tuple[str, dict[str, Any]] | None:
    """Parse one JSON auto_explain notice and discard all original query values."""
    match = _AUTO_EXPLAIN_PLAN_RE.search(message)
    if not match:
        return None
    envelope = _plan_envelope(json.loads(match.group("document")))
    normalized_sql = _normalize_sql(str(envelope.get("Query Text", "unknown statement")))
    return normalized_sql, _sanitize_plan(envelope)


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every plan node from a sanitized EXPLAIN envelope."""
    root = plan.get("Plan")
    if not isinstance(root, dict):
        return []
    pending = [root]
    nodes = []
    while pending:
        node = pending.pop()
        nodes.append(node)
        pending.extend(child for child in node.get("Plans", ()) if isinstance(child, dict))
    return nodes


def _plan_identity_shape(value: Any) -> Any:
    """Retain only structural and planner fields that define the chosen plan."""
    if isinstance(value, dict):
        return {key: _plan_identity_shape(child) for key, child in value.items() if key in _PLAN_IDENTITY_KEYS}
    if isinstance(value, list):
        return [_plan_identity_shape(child) for child in value]
    return value


def _root_work(plan: dict[str, Any]) -> dict[str, float | int]:
    """Extract additive work measures from the root plan node."""
    root = plan.get("Plan", {})
    loops = int(root.get("Actual Loops", 0) or 0)
    rows = int(root.get("Actual Rows", 0) or 0)
    work: dict[str, float | int] = {
        "planner_total_cost": float(root.get("Total Cost", 0) or 0),
        "planner_rows": int(root.get("Plan Rows", 0) or 0),
        "actual_loops": loops,
        "actual_rows_times_loops": rows * loops,
    }
    for key in _WORK_COUNTER_KEYS:
        work[key.lower().replace(" ", "_")] = int(root.get(key, 0) or 0)
    return work


def _add_work(total: dict[str, float | int], work: dict[str, float | int]) -> None:
    """Add one plan execution's work to an aggregate."""
    for key, value in work.items():
        total[key] = total.get(key, 0) + value


def _group_plans(notices: list[str]) -> list[dict[str, Any]]:
    """Group identical sanitized plans and retain one full representative plan."""
    grouped: dict[str, dict[str, Any]] = {}
    for message in notices:
        parsed = _parse_notice(message)
        if parsed is None:
            continue
        normalized_sql, plan = parsed
        serialized = json.dumps(_plan_identity_shape(plan), sort_keys=True, separators=(",", ":"))
        identity = _fingerprint(f"{normalized_sql}\0{serialized}")
        entry = grouped.setdefault(
            identity,
            {
                "fingerprint": identity,
                "statement_fingerprint": _fingerprint(normalized_sql),
                "normalized_sql": normalized_sql,
                "calls": 0,
                "aggregate": {},
                "indexes": set(),
                "node_types": Counter(),
                "plan": plan,
            },
        )
        entry["calls"] += 1
        _add_work(entry["aggregate"], _root_work(plan))
        for node in _plan_nodes(plan):
            if index_name := node.get("Index Name"):
                entry["indexes"].add(index_name)
            if node_type := node.get("Node Type"):
                entry["node_types"][node_type] += 1
    plans = []
    for entry in grouped.values():
        entry["indexes"] = sorted(entry["indexes"])
        entry["node_types"] = dict(sorted(entry["node_types"].items()))
        plans.append(entry)
    return sorted(plans, key=lambda item: item["fingerprint"])


class _StatementRecorder:
    """Django execute wrapper that records statement shapes without parameter values."""

    def __init__(self) -> None:
        self._statements: dict[str, dict[str, Any]] = {}

    def __call__(self, execute, sql, params, many, context):
        """Record one completed statement and return its normal result."""
        result = execute(sql, params, many, context)
        normalized = _normalize_sql(str(sql))
        identity = _fingerprint(normalized)
        entry = self._statements.setdefault(
            identity,
            {
                "fingerprint": identity,
                "normalized_sql": normalized,
                "calls": 0,
                "rows_affected": 0,
            },
        )
        entry["calls"] += 1
        row_count = context["cursor"].rowcount
        if isinstance(row_count, int) and row_count > 0:
            entry["rows_affected"] += row_count
        return result

    def result(self) -> list[dict[str, Any]]:
        """Return statements in stable fingerprint order."""
        return sorted(self._statements.values(), key=lambda item: item["fingerprint"])


@contextlib.contextmanager
def _auto_explain_notices():
    """Capture JSON plans from the active PostgreSQL session or fail explicitly."""
    if connection.vendor != "postgresql":
        raise AssertionError(f"Performance profiling requires PostgreSQL, got {connection.vendor!r}.")
    connection.ensure_connection()
    raw_connection = connection.connection
    if not hasattr(raw_connection, "add_notice_handler"):
        raise AssertionError("Performance profiling requires psycopg 3 notice handlers.")

    notices: list[str] = []

    def handle_notice(diagnostic) -> None:
        notices.append(str(diagnostic.message_primary))

    configured = False
    raw_connection.add_notice_handler(handle_notice)
    try:
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("LOAD 'auto_explain'")
                cursor.execute("SET client_min_messages = notice")
                cursor.execute("SET auto_explain.log_min_duration = 0")
                cursor.execute("SET auto_explain.log_analyze = on")
                cursor.execute("SET auto_explain.log_timing = off")
                cursor.execute("SET auto_explain.log_buffers = on")
                cursor.execute("SET auto_explain.log_wal = on")
                cursor.execute("SET auto_explain.log_nested_statements = on")
                cursor.execute("SET auto_explain.log_triggers = on")
                cursor.execute("SET auto_explain.log_verbose = off")
                cursor.execute("SET auto_explain.log_settings = off")
                cursor.execute("SET auto_explain.log_format = json")
                cursor.execute("SET auto_explain.log_level = notice")
                cursor.execute("SET auto_explain.log_parameter_max_length = 0")
                cursor.execute("SET auto_explain.sample_rate = 1")
        except DatabaseError as error:
            raise AssertionError("PostgreSQL auto_explain profiling is unavailable.") from error
        configured = True
        yield notices
    finally:
        if configured:
            with contextlib.suppress(DatabaseError), connection.cursor() as cursor:
                cursor.execute("SET auto_explain.log_min_duration = -1")
        raw_connection.remove_notice_handler(handle_notice)


def _git_revision(path: Path) -> str | None:
    """Return the source revision for a checkout, if the checkout metadata is present."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _cpu_model() -> str:
    """Return a stable processor description without recording the host identity."""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.partition(":")[2].strip()
    return platform.processor() or "unknown"


def _planner_environment() -> dict[str, str]:
    """Read the PostgreSQL revision and planner settings used by the run."""
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        server_version = cursor.fetchone()[0]
        cursor.execute("SHOW server_version_num")
        server_version_num = cursor.fetchone()[0]
        planner = {}
        for setting in _PLANNER_SETTINGS:
            cursor.execute(f"SHOW {setting}")  # noqa: S608 - names come only from the fixed tuple above
            planner[setting] = cursor.fetchone()[0]
    return {
        "server_version": server_version,
        "server_version_num": server_version_num,
        "planner_settings": planner,
    }


def _host_load() -> dict[str, float]:
    """Return the 1, 5 and 15 minute run-queue averages, which decide whether timing is evidence."""
    one, five, fifteen = os.getloadavg()
    return {"one_minute": one, "five_minute": five, "fifteen_minute": fifteen}


def _source_environment() -> dict[str, Any]:
    """Return source and machine revisions without an environment-specific host name."""
    plugin_root = Path(__file__).resolve().parents[2]
    plugin_revision = os.environ.get(_PLUGIN_REVISION_VARIABLE) or _git_revision(plugin_root)
    netbox_revision = os.environ.get(_NETBOX_REVISION_VARIABLE)
    if not netbox_revision:
        netbox_revision = _git_revision(Path(settings.BASE_DIR).resolve().parent)
    if not plugin_revision:
        raise AssertionError(f"Set {_PLUGIN_REVISION_VARIABLE} when the plugin Git checkout is not readable.")
    if not netbox_revision:
        raise AssertionError(f"Set {_NETBOX_REVISION_VARIABLE} when NetBox does not run from a Git checkout.")
    return {
        "plugin_revision": plugin_revision,
        "plugin_version": plugin_version,
        "netbox_revision": netbox_revision,
        "netbox_version": settings.RELEASE.full_version,
        "django_version": django.get_version(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "operating_system": platform.system(),
        "operating_system_release": platform.release(),
        "machine_architecture": platform.machine(),
        "cpu_model": _cpu_model(),
    }


def _work_totals(statements: list[dict[str, Any]], plans: list[dict[str, Any]], work_units: int) -> dict[str, Any]:
    """Summarize database work and derive scale-normalized values."""
    totals: dict[str, float | int] = {"statement_calls": sum(item["calls"] for item in statements)}
    totals["planned_statement_calls"] = sum(item["calls"] for item in plans)
    for plan in plans:
        _add_work(totals, plan["aggregate"])
    return {
        **totals,
        "work_units": work_units,
        "statement_calls_per_work_unit": totals["statement_calls"] / work_units,
        "planner_total_cost_per_work_unit": totals.get("planner_total_cost", 0) / work_units,
        "actual_rows_per_work_unit": totals.get("actual_rows_times_loops", 0) / work_units,
        "shared_hit_blocks_per_work_unit": totals.get("shared_hit_blocks", 0) / work_units,
    }


def _load_sentence(artifact: dict[str, Any]) -> str:
    """Report the run-queue length this run competed with, so a reader can judge its timing."""
    load = artifact["environment"].get("host_load")
    if not load:
        return "Host load during the run was not recorded, so treat the machine time as unverified."
    return (
        f"Host load averaged {load['started']['one_minute']:.2f} over the minute before the run and "
        f"{load['finished']['one_minute']:.2f} over the minute it ended. Machine time is evidence only "
        "when both runs were taken under a comparable load."
    )


def _timing_sentence(artifact: dict[str, Any]) -> str:
    """Say whether this run measured machine time at all."""
    if artifact["configuration"]["samples"]:
        return "This run measured machine time. Statement counts are reproducible and carry the verdict."
    return (
        "This run measured database work, including SQL calls, planner cost, and shared hits. It took no "
        "machine-time samples, so every timing column reads `not measured` rather than reporting a number "
        "nobody should quote."
    )


def _markdown_summary(artifact: dict[str, Any]) -> str:
    """Render the comparison fields that a reviewer usually needs first."""
    kind = artifact.get("baseline_kind", _DEFAULT_KIND).replace("_", " ")
    lines = [
        f"# Automatic naming performance: {kind}",
        "",
        "This file is generated by `netbox_interface_name_rules.tests.signal_performance`.",
        "Machine-time values are evidence for a same-hardware before/after comparison. They are not CI limits.",
        "",
        _timing_sentence(artifact),
        "",
        _load_sentence(artifact),
        "",
        "| Scenario | Layer | SQL calls | Planner cost | Shared hits | Wall median (ms) | Wall p95 (ms) | CPU median (ms) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario in artifact["scenarios"]:
        totals = scenario["database"]["totals"]
        machine_time = scenario["machine_time"]
        if machine_time is None:
            timing = "not measured | not measured | not measured"
        else:
            wall = machine_time["wall"]
            cpu = machine_time["process_cpu"]
            timing = f"{wall['median_ms']:.3f} | {wall['p95_ms']:.3f} | {cpu['median_ms']:.3f}"
        lines.append(
            f"| `{scenario['name']}` | {scenario['layer']} | {totals['statement_calls']} | "
            f"{totals.get('planner_total_cost', 0):.3f} | {totals.get('shared_hit_blocks', 0)} | {timing} |"
        )
    lines.extend(
        [
            "",
            f"Plugin revision: `{artifact['environment']['plugin_revision']}`",
            "",
            f"NetBox revision: `{artifact['environment']['netbox_revision']}`",
            "",
            f"PostgreSQL: `{artifact['environment']['postgresql']['server_version']}`",
            "",
            f"Samples per scenario: `{artifact['configuration']['samples']}`",
            "",
        ]
    )
    return "\n".join(lines)


class SignalPathPerformanceTest(TransactionTestCase):
    """Generate retained evidence for the existing automatic naming implementation."""

    available_apps = tuple(app_config.name for app_config in apps.get_app_configs())
    maxDiff = None

    def _build_device(self, prefix: str, bay_positions=(), **device_kwargs):
        """Create a device and its module bays."""
        slug = prefix.lower()
        manufacturer = Manufacturer.objects.create(name=f"{prefix}Mfg", slug=f"{slug}-mfg")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model=f"{prefix}-Device",
            slug=f"{slug}-device",
        )
        for position in bay_positions:
            ModuleBayTemplate.objects.create(device_type=device_type, name=f"Bay {position}", position=str(position))
        role = DeviceRole.objects.create(name=f"{prefix}Role", slug=f"{slug}-role")
        site = Site.objects.create(name=f"{prefix}Site", slug=f"{slug}-site")
        device = Device.objects.create(
            name=f"{slug}-device",
            device_type=device_type,
            role=role,
            site=site,
            **device_kwargs,
        )
        return manufacturer, device

    @staticmethod
    def _plain_module_type(manufacturer: Manufacturer, model: str, name: str = "{module}") -> ModuleType:
        """Create one module type with one plain interface template."""
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=model)
        InterfaceTemplate.objects.create(module_type=module_type, name=name, type=_PLAIN_TYPE)
        return module_type

    @staticmethod
    def _channelized_module_type(manufacturer: Manufacturer, model: str) -> ModuleType:
        """Create one native parent plus four channel interface templates."""
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=model)
        parent = InterfaceTemplate.objects.create(
            module_type=module_type,
            name="{module}",
            type=_PARENT_TYPE,
            channels=4,
        )
        for channel_id in range(1, 5):
            InterfaceTemplate.objects.create(
                module_type=module_type,
                name=f"{{module}}:{channel_id}",
                type=_CHANNEL_TYPE,
                parent=parent,
                channel_id=channel_id,
            )
        return module_type

    @staticmethod
    def _fixture(module_type: ModuleType, device: Device) -> dict[str, int]:
        """Record the exact scenario sizes before the measured operation."""
        return {
            "devices": 1,
            "module_bays": ModuleBay.objects.filter(device=device).count(),
            "modules_before": Module.objects.filter(device=device).count(),
            "interfaces_before": Interface.objects.filter(device=device).count(),
            "interface_templates": InterfaceTemplate.objects.filter(module_type=module_type).count(),
            "enabled_rules": InterfaceNameRule.objects.filter(enabled=True).count(),
        }

    @staticmethod
    def _cleanup_fixture(prefix: str) -> None:
        """Delete one committed scenario fixture without changing shared reference data."""
        manufacturer_name = f"{prefix}Mfg"
        Device.objects.filter(name=f"{prefix.lower()}-device").delete()
        ModuleType.objects.filter(manufacturer__name=manufacturer_name).delete()
        DeviceType.objects.filter(manufacturer__name=manufacturer_name).delete()
        Manufacturer.objects.filter(name=manufacturer_name).delete()
        DeviceRole.objects.filter(name=f"{prefix}Role").delete()
        Site.objects.filter(name=f"{prefix}Site").delete()
        VirtualChassis.objects.filter(name=f"{prefix}-VC").delete()

    def _module_operation(self, prefix, device, module_type, direct, expected_names) -> _PreparedScenario:
        """Prepare either a complete install path or its deferred callback only."""
        bay = ModuleBay.objects.get(device=device, position="3")
        holder: dict[str, Module] = {}
        if direct:
            holder["module"] = Module.objects.create(device=device, module_bay=bay, module_type=module_type)

            def operation():
                _apply_rules_deferred(holder["module"].pk, bay.pk)

        else:

            def operation():
                with transaction.atomic():
                    holder["module"] = Module.objects.create(
                        device=device,
                        module_bay=bay,
                        module_type=module_type,
                    )

        def verify():
            names = sorted(Interface.objects.filter(module=holder["module"]).values_list("name", flat=True))
            self.assertEqual(names, expected_names)
            return {"interface_names": names, "interfaces_after": len(names)}

        return _PreparedScenario(
            operation=operation,
            verify=verify,
            cleanup=lambda: self._cleanup_fixture(prefix),
            fixture=self._fixture(module_type, device),
            work_units=len(expected_names),
        )

    def _prepare_module(self, prefix: str, kind: str, direct: bool) -> _PreparedScenario:
        """Build one module-install scenario."""
        manufacturer, device = self._build_device(prefix, ("3",))
        if kind in {"existing_family", "reconciliation"}:
            module_type = self._channelized_module_type(manufacturer, f"{prefix}-Module")
        else:
            module_type = self._plain_module_type(manufacturer, f"{prefix}-Module")

        rule_options: dict[str, Any]
        if kind == "no_matching_rule":
            other_type = ModuleType.objects.create(
                manufacturer=manufacturer,
                model=f"{prefix}-Other",
                part_number=f"{prefix}-Other",
            )
            rule_module_type = other_type
            rule_options = {"name_template": "unused-{bay_position}"}
            expected = ["3"]
        elif kind == "plain_rename":
            rule_module_type = module_type
            rule_options = {"name_template": "et-0/0/{bay_position}"}
            expected = ["et-0/0/3"]
        elif kind == "structural_creation":
            rule_module_type = module_type
            rule_options = {
                "name_template": "xe-0/0/{bay_position}:{channel}",
                "parent_name_template": "et-0/0/{bay_position}",
                "breakout_mode": BreakoutModeChoices.CHANNELIZED,
                "channel_count": 4,
                "channel_start": 0,
            }
            expected = ["et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"]
        elif kind == "existing_family":
            rule_module_type = module_type
            rule_options = {"name_template": "et-0/0/{bay_position}"}
            expected = ["et-0/0/3", "et-0/0/3:1", "et-0/0/3:2", "et-0/0/3:3", "et-0/0/3:4"]
        elif kind == "reconciliation":
            rule_module_type = module_type
            rule_options = {
                "name_template": "{base}:{channel}",
                "parent_name_template": "et-0/0/{bay_position}",
                "breakout_mode": BreakoutModeChoices.CHANNELIZED,
                "channel_count": 4,
                "channel_start": 1,
            }
            expected = ["3:1", "3:2", "3:3", "3:4", "et-0/0/3"]
        else:
            raise AssertionError(f"Unknown module scenario {kind!r}.")
        prepared = self._module_operation(prefix, device, module_type, direct, expected)
        InterfaceNameRule.objects.create(module_type=rule_module_type, **rule_options)
        return replace(prepared, fixture=self._fixture(module_type, device))

    def _prepare_vc(self, prefix: str, module_count: int, direct: bool) -> _PreparedScenario:
        """Build a VC reapply scenario with one or eight installed modules."""
        virtual_chassis = VirtualChassis.objects.create(name=f"{prefix}-VC")
        manufacturer, device = self._build_device(
            prefix,
            tuple(str(position) for position in range(1, module_count + 1)),
            virtual_chassis=virtual_chassis,
            vc_position=1,
        )
        module_type = self._plain_module_type(
            manufacturer,
            f"{prefix}-Module",
            name="xe-{vc_position:0}/0/{module}",
        )
        for position in range(1, module_count + 1):
            bay = ModuleBay.objects.get(device=device, position=str(position))
            Module.objects.create(device=device, module_bay=bay, module_type=module_type)
        InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="et-{vc_position}/0/{bay_position}",
        )

        if direct:
            Device.objects.filter(pk=device.pk).update(vc_position=2)

            def operation():
                _apply_rules_for_device_deferred(device.pk)

        else:

            def operation():
                device.vc_position = 2
                with transaction.atomic():
                    device.save()

        expected = [f"et-2/0/{position}" for position in range(1, module_count + 1)]

        def verify():
            names = sorted(Interface.objects.filter(device=device).values_list("name", flat=True))
            self.assertEqual(names, expected)
            return {"interface_names": names, "interfaces_after": len(names)}

        return _PreparedScenario(
            operation=operation,
            verify=verify,
            cleanup=lambda: self._cleanup_fixture(prefix),
            fixture=self._fixture(module_type, device),
            work_units=module_count,
        )

    def _scenarios(self) -> list[_Scenario]:
        """Return the stable scenario matrix in report order."""
        scenarios = []
        module_kinds = (
            ("no_matching_rule", "No matching rule"),
            ("plain_rename", "Plain interface rename"),
            ("structural_creation", "Four-channel structural creation"),
            ("existing_family", "Existing parent plus four channels rename"),
            ("reconciliation", "Deferred channel reconciliation"),
        )
        for kind, description in module_kinds:
            for direct in (False, True):
                layer = "direct_callback" if direct else "complete_model_save"
                name = f"module.{layer}.{kind}"
                prefix = "Perf" + _fingerprint(name)[:8]
                scenarios.append(
                    _Scenario(
                        name=name,
                        description=description,
                        layer=layer,
                        prepare=lambda prefix=prefix, kind=kind, direct=direct: self._prepare_module(
                            prefix, kind, direct
                        ),
                    )
                )
        for module_count in (1, 8):
            for direct in (False, True):
                layer = "direct_callback" if direct else "complete_model_save"
                name = f"vc.{layer}.reapply_{module_count}"
                prefix = "Perf" + _fingerprint(name)[:8]
                scenarios.append(
                    _Scenario(
                        name=name,
                        description=f"Virtual chassis reapply across {module_count} module(s)",
                        layer=layer,
                        prepare=lambda prefix=prefix, module_count=module_count, direct=direct: self._prepare_vc(
                            prefix, module_count, direct
                        ),
                    )
                )
        return scenarios

    def test_direct_vc_fixture_has_target_position(self):
        """Keep direct callback setup outside the measured operation."""
        prefix = "PerfDirectVcFixture"
        self._prepare_vc(prefix, module_count=1, direct=True)

        device = Device.objects.get(name=f"{prefix.lower()}-device")
        self.assertEqual(device.vc_position, 2)

    def test_complete_model_save_callback_runs_after_commit(self):
        """Measure the deferred callback outside the model-save transaction."""
        prepared = self._prepare_module("PerfCommittedCallback", "plain_rename", direct=False)
        atomic_states = []

        def record_atomic_state(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            atomic_states.append(connection.in_atomic_block)
            return result

        with connection.execute_wrapper(record_atomic_state):
            prepared.operation()

        self.assertIn(False, atomic_states)

    def test_direct_module_fixture_includes_the_measured_rule(self):
        """Record fixture counts after direct-callback setup is complete."""
        prepared = self._prepare_module("PerfDirectFixture", "plain_rename", direct=True)

        self.assertEqual(prepared.fixture["enabled_rules"], 1)

    def test_unmeasured_summary_names_the_recorded_database_work(self):
        """Do not describe a planner and buffer profile as statement counts alone."""
        artifact = {"configuration": {"samples": 0}}

        self.assertEqual(
            _timing_sentence(artifact),
            "This run measured database work, including SQL calls, planner cost, and shared hits. It took no "
            "machine-time samples, so every timing column reads `not measured` rather than reporting a number "
            "nobody should quote.",
        )

    def test_sql_normalization_scrubs_dollar_quoted_literals(self):
        """Remove both untagged and tagged PostgreSQL dollar-quoted values."""
        normalized = _normalize_sql("SELECT $$customer-token$$, $audit$second-token$audit$")

        self.assertEqual(normalized, "SELECT '?', '?'")

    def test_profile_closes_the_instrumented_connection(self):
        """Make the later timing pass open a session without auto_explain hooks."""
        scenario = self._scenarios()[0]

        self._profile_scenario(scenario)

        self.assertIsNone(connection.connection)

    def test_auto_explain_teardown_preserves_database_error(self):
        """Keep the original database diagnosis when profiling aborts."""
        with self.assertRaisesRegex(DatabaseError, "division by zero"), transaction.atomic():
            with _auto_explain_notices(), connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")

    def test_plan_grouping_ignores_runtime_counters(self):
        """Group one SQL plan shape even when its runtime work changes."""
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMPORARY TABLE profile_grouping_left (value integer) ON COMMIT DROP")
                cursor.execute("CREATE TEMPORARY TABLE profile_grouping_right (value integer) ON COMMIT DROP")
                cursor.execute("INSERT INTO profile_grouping_left VALUES (1)")
                cursor.execute("INSERT INTO profile_grouping_right VALUES (1)")
                cursor.execute("SET LOCAL enable_hashjoin = off")
                cursor.execute("SET LOCAL enable_mergejoin = off")

            with _auto_explain_notices() as notices, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT left_side.value FROM profile_grouping_left AS left_side "
                    "JOIN profile_grouping_right AS right_side ON left_side.value = right_side.value"
                )
                cursor.execute("INSERT INTO profile_grouping_left VALUES (2)")
                cursor.execute(
                    "SELECT left_side.value FROM profile_grouping_left AS left_side "
                    "JOIN profile_grouping_right AS right_side ON left_side.value = right_side.value"
                )

        select_plans = [
            plan
            for plan in _group_plans(notices)
            if plan["normalized_sql"].startswith("SELECT left_side.value FROM profile_grouping_left")
        ]
        self.assertEqual(len(select_plans), 1, select_plans)
        self.assertEqual(select_plans[0]["calls"], 2)
        self.assertEqual(select_plans[0]["aggregate"]["actual_rows_times_loops"], 2)

    @staticmethod
    def _analyze_tables() -> None:
        """Refresh planner statistics after each fixture is prepared."""
        tables = sorted({model._meta.db_table for model in _PROFILE_MODELS})
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f"ANALYZE {connection.ops.quote_name(table)}")

    def _warm_scenario(self, scenario: _Scenario) -> None:
        """Run and discard the operation once, so the recorded pass never pays a first-use cost.

        NetBox resolves object types, custom fields and its search backend lazily and keeps them for
        the process.  A cold pass therefore issues statements a warm one does not, and a production
        worker answers almost every caller warm.
        """
        prepared = None
        try:
            prepared = scenario.prepare()
            prepared.operation()
        finally:
            if prepared is not None:
                prepared.cleanup()

    def _profile_scenario(self, scenario: _Scenario) -> dict[str, Any]:
        """Record the operation twice and refuse a scenario whose statement counts do not agree."""
        self._warm_scenario(scenario)
        first = self._record_scenario(scenario)
        second = self._record_scenario(scenario)
        self._assert_same_statements(scenario, first, second)
        return second

    def _assert_same_statements(self, scenario: _Scenario, first: dict[str, Any], second: dict[str, Any]) -> None:
        """Fail with the differing statements, so an artifact can never record an unreproducible count."""
        counts = [
            {item["normalized_sql"]: item["calls"] for item in run["database"]["statements"]} for run in (first, second)
        ]
        differing = sorted(
            (sql for sql in set(counts[0]) | set(counts[1]) if counts[0].get(sql, 0) != counts[1].get(sql, 0)),
            key=lambda sql: abs(counts[1].get(sql, 0) - counts[0].get(sql, 0)),
            reverse=True,
        )
        if not differing:
            return
        detail = "\n".join(
            f"  {counts[0].get(sql, 0)} then {counts[1].get(sql, 0)}: {sql[:120]}" for sql in differing[:8]
        )
        self.fail(
            f"{scenario.name} issued different statements on two identical runs "
            f"({first['database']['totals']['statement_calls']} then "
            f"{second['database']['totals']['statement_calls']} calls):\n{detail}"
        )

    def _record_scenario(self, scenario: _Scenario) -> dict[str, Any]:
        """Record SQL statements and PostgreSQL work for one operation."""
        prepared = None
        try:
            prepared = scenario.prepare()
            self._analyze_tables()
            recorder = _StatementRecorder()
            with _auto_explain_notices() as notices, connection.execute_wrapper(recorder):
                prepared.operation()
            semantic = prepared.verify()
            statements = recorder.result()
            plans = _group_plans(notices)
            if not statements:
                self.fail(f"{scenario.name} did not execute any recorded SQL statements.")
            if not plans:
                self.fail(f"{scenario.name} did not produce any auto_explain plans.")
            return {
                "fixture": prepared.fixture,
                "semantic_result": semantic,
                "database": {
                    "statements": statements,
                    "plans": plans,
                    "totals": _work_totals(statements, plans, prepared.work_units),
                },
            }
        finally:
            if prepared is not None:
                prepared.cleanup()
            connection.close()

    def _time_scenario(self, scenario: _Scenario, samples: int, warmups: int) -> dict[str, Any]:
        """Measure the uninstrumented operation with fresh fixtures for every sample."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('auto_explain.log_min_duration', true)")
            if cursor.fetchone()[0] is not None:
                self.fail("The timing session has loaded auto_explain instrumentation.")
        warmup_wall_samples = []
        warmup_cpu_samples = []
        wall_samples = []
        cpu_samples = []
        for sample_index in range(warmups + samples):
            prepared = None
            try:
                prepared = scenario.prepare()
                self._analyze_tables()
                gc_enabled = gc.isenabled()
                if gc_enabled:
                    gc.disable()
                try:
                    wall_start = time.perf_counter_ns()
                    cpu_start = time.process_time_ns()
                    prepared.operation()
                    cpu_elapsed = time.process_time_ns() - cpu_start
                    wall_elapsed = time.perf_counter_ns() - wall_start
                finally:
                    if gc_enabled:
                        gc.enable()
                prepared.verify()
                if sample_index < warmups:
                    warmup_wall_samples.append(wall_elapsed)
                    warmup_cpu_samples.append(cpu_elapsed)
                else:
                    wall_samples.append(wall_elapsed)
                    cpu_samples.append(cpu_elapsed)
            finally:
                if prepared is not None:
                    prepared.cleanup()
        return {
            "warmup": {
                "wall": _optional_summary(warmup_wall_samples),
                "process_cpu": _optional_summary(warmup_cpu_samples),
            },
            "wall": _summary(wall_samples),
            "process_cpu": _summary(cpu_samples),
        }

    def test_record_existing_signal_path_performance(self):
        """Write the retained before-refactor database and machine-time evidence."""
        output = _output_path()
        samples = _positive_integer_from_environment(_SAMPLE_VARIABLE, _DEFAULT_SAMPLES, allow_zero=True)
        warmups = _positive_integer_from_environment(_WARMUP_VARIABLE, _DEFAULT_WARMUPS, allow_zero=True)
        self.assertTrue(supports_channelization(), "The baseline requires NetBox channelization support.")
        self.assertTrue(supports_vc_position_token(), "The baseline requires NetBox VC position token support.")

        load_before = _host_load()
        scenario_results = []
        for scenario in self._scenarios():
            profiled = self._profile_scenario(scenario)
            scenario_results.append(
                {
                    "name": scenario.name,
                    "description": scenario.description,
                    "layer": scenario.layer,
                    **profiled,
                    "machine_time": self._time_scenario(scenario, samples, warmups) if samples else None,
                }
            )

        artifact = {
            "schema_version": SCHEMA_VERSION,
            "baseline_kind": os.environ.get(_KIND_VARIABLE, _DEFAULT_KIND),
            "generated_at": datetime.now(UTC).isoformat(),
            "configuration": {
                "samples": samples,
                "warmups": warmups,
                "postgresql_node_timing": False,
                "machine_time_ci_gate": False,
            },
            "environment": {
                **_source_environment(),
                "postgresql": _planner_environment(),
                "host_load": {"started": load_before, "finished": _host_load()},
            },
            "scenarios": scenario_results,
        }
        validate_artifact(artifact, "generated performance artifact")
        output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
        output.with_suffix(".md").write_text(_markdown_summary(artifact))
