# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Compare two automatic naming performance artifacts and write a readable summary.

Usage: python performance/compare.py BEFORE.json AFTER.json OUT.md

Database work is deterministic, so its deltas are evidence on any machine. Machine time is only
evidence when both runs were taken on the same hardware under a comparable load; the summary says
which numbers it is reporting and leaves the judgement to the reader.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_DB_METRICS = (
    ("statement_calls", "SQL calls"),
    ("planner_total_cost", "Planner cost"),
    ("shared_hit_blocks", "Shared hits"),
    ("shared_read_blocks", "Shared reads"),
    ("wal_bytes", "WAL bytes"),
)
_TIME_METRICS = (
    ("wall", "median_ms", "Wall median (ms)"),
    ("wall", "p95_ms", "Wall p95 (ms)"),
    ("process_cpu", "median_ms", "CPU median (ms)"),
)


def _artifact_path(raw, must_exist):
    """Resolve *raw* inside this repository, refusing a path that points outside it."""
    candidate = Path(raw)
    path = (candidate if candidate.is_absolute() else _ROOT / candidate).resolve()
    if not path.is_relative_to(_ROOT):
        raise SystemExit(f"{raw} is outside the repository")
    if must_exist and not path.is_file():
        raise SystemExit(f"{raw} is not a readable artifact")
    return path


def _statement_table(sql):
    """Return the table a normalized statement reads or writes, for attribution."""
    match = re.search(r'(?:FROM|INTO|UPDATE)\s+"([a-z_]+)"', sql)
    return match.group(1) if match else sql.split()[0][:24]


def _calls_by_table(scenario):
    """Return how many statements the scenario issued against each table."""
    counts: Counter = Counter()
    for entry in scenario["database"]["statements"]:
        counts[_statement_table(entry["normalized_sql"])] += entry["calls"]
    return counts


def _attribution(name, before_scenario, after_scenario):
    """Return the per-table statement deltas behind one scenario's change."""
    before_calls, after_calls = _calls_by_table(before_scenario), _calls_by_table(after_scenario)
    rows = []
    for table in sorted(set(before_calls) | set(after_calls)):
        change = after_calls[table] - before_calls[table]
        if change:
            rows.append(f"| `{name}` | `{table}` | {before_calls[table]} | {after_calls[table]} | {change:+d} |")
    return rows


def _scenarios(artifact):
    """Return the artifact's scenarios keyed by name."""
    return {scenario["name"]: scenario for scenario in artifact["scenarios"]}


def _delta(before, after):
    """Return the absolute and percentage change, or None when there is no baseline to divide by."""
    change = after - before
    if not before:
        return change, None
    return change, change / before * 100


def _format(value):
    """Return a compact fixed-point rendering of one metric value."""
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.2f}"
    return f"{int(value)}"


def _database_table(before, after):
    """Return the per-scenario database-work comparison."""
    lines = ["| Scenario | Metric | Before | After | Change | Share |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    regressions = []
    for name, before_scenario in before.items():
        after_scenario = after.get(name)
        if after_scenario is None:
            lines.append(f"| `{name}` | — | — | missing | — | — |")
            continue
        for key, label in _DB_METRICS:
            old = before_scenario["database"]["totals"].get(key)
            new = after_scenario["database"]["totals"].get(key)
            if old is None or new is None:
                continue
            change, percent = _delta(old, new)
            share = "n/a" if percent is None else f"{percent:+.1f}%"
            lines.append(f"| `{name}` | {label} | {_format(old)} | {_format(new)} | {_format(change)} | {share} |")
            if key == "statement_calls" and change > 0:
                regressions.append((name, label, old, new))
    return lines, regressions


def _time_table(before, after):
    """Return the per-scenario machine-time comparison."""
    lines = ["| Scenario | Metric | Before | After | Change | Share |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for name, before_scenario in before.items():
        after_scenario = after.get(name)
        if after_scenario is None:
            continue
        for section, key, label in _TIME_METRICS:
            old = before_scenario["machine_time"][section][key]
            new = after_scenario["machine_time"][section][key]
            change, percent = _delta(old, new)
            share = "n/a" if percent is None else f"{percent:+.1f}%"
            lines.append(f"| `{name}` | {label} | {_format(old)} | {_format(new)} | {_format(change)} | {share} |")
    return lines


def _environment_table(before, after):
    """Return the revisions and settings each run was taken under."""
    rows = ["| Field | Before | After |", "| --- | --- | --- |"]
    for key in ("plugin_revision", "netbox_revision", "netbox_version", "cpu_model", "operating_system_release"):
        rows.append(f"| {key} | `{before['environment'].get(key)}` | `{after['environment'].get(key)}` |")
    for key in ("samples", "warmups"):
        rows.append(f"| {key} | `{before['configuration'].get(key)}` | `{after['configuration'].get(key)}` |")
    postgres_before = before["environment"].get("postgresql", {}).get("version")
    postgres_after = after["environment"].get("postgresql", {}).get("version")
    rows.append(f"| postgresql | `{postgres_before}` | `{postgres_after}` |")
    return rows


def main(argv):
    """Write the comparison of two artifacts to a Markdown file."""
    if len(argv) != 4:
        raise SystemExit(__doc__)
    before = json.loads(_artifact_path(argv[1], must_exist=True).read_text())
    after = json.loads(_artifact_path(argv[2], must_exist=True).read_text())
    destination = _artifact_path(argv[3], must_exist=False)
    before_scenarios, after_scenarios = _scenarios(before), _scenarios(after)

    database_lines, regressions = _database_table(before_scenarios, after_scenarios)
    report = [
        "# Automatic naming performance comparison",
        "",
        "Generated by `performance/compare.py` from the before and after artifacts of the "
        "interface-family refactor. Database work is deterministic and is the evidence this "
        "comparison rests on. Machine time is reported for completeness and is only evidence when "
        "both runs were taken on the same otherwise-idle hardware.",
        "",
        "## Environment",
        "",
        *_environment_table(before, after),
        "",
        "## Database work",
        "",
        *database_lines,
        "",
        "## Machine time",
        "",
        *_time_table(before_scenarios, after_scenarios),
        "",
        "## Statement-count regressions",
        "",
    ]
    if regressions:
        report.extend(f"- `{name}` {label}: {_format(old)} to {_format(new)}" for name, label, old, new in regressions)
        report += [
            "",
            "### Where those statements come from",
            "",
            "Each raised scenario is broken down by the table its statements touch, so the work this "
            "plugin drives can be told apart from the per-save bookkeeping NetBox does for the object "
            "types and custom fields the database happens to hold.",
            "",
            "| Scenario | Table | Before | After | Change |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        for name, _label, _old, _new in regressions:
            report.extend(_attribution(name, before_scenarios[name], after_scenarios[name]))
    else:
        report.append("None. No scenario issues more statements than the baseline.")
    report.append("")
    destination.write_text("\n".join(report))
    print(f"wrote {destination} ({len(regressions)} statement-count regressions)")


if __name__ == "__main__":
    main(sys.argv)
