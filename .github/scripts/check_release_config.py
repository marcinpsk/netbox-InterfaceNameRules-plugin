#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Fail when pyproject sets a semantic-release option the tool does not define.

python-semantic-release ignores unknown keys in every `[tool.semantic_release*]`
table, so a misspelled option silently stops releasing and nothing reports it.
"""

from __future__ import annotations

import dataclasses
import sys
import tomllib
import typing
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel
from semantic_release.cli.config import RawConfig

# psr exposes no public name -> parser class map. Failing loudly on an upgrade beats
# silently checking nothing.
try:
    from semantic_release.cli.config import _known_commit_parsers
except ImportError as exc:  # pragma: no cover - upgrade guard
    raise SystemExit(
        "semantic_release no longer exports _known_commit_parsers; "
        "update .github/scripts/check_release_config.py to the new API."
    ) from exc


def _nested_model(annotation: object) -> tuple[type[BaseModel] | None, bool]:
    """Return the model *annotation* holds and whether it holds a mapping of them."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, False
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if origin in (dict, Mapping) and len(arguments) == 2:
        model, _ = _nested_model(arguments[1])
        return model, model is not None
    for argument in arguments:
        model, mapping = _nested_model(argument)
        if model is not None:
            return model, mapping
    return None, False


def _check_model(table: dict, model: type[BaseModel], path: str, unknown: list[str]) -> None:
    """Record every key of *table* that *model* does not define, then recurse."""
    for key, value in table.items():
        where = f"{path}.{key}"
        if key not in model.model_fields:
            unknown.append(where)
            continue
        if not isinstance(value, dict):
            continue
        nested, is_mapping = _nested_model(model.model_fields[key].annotation)
        if nested is None:
            continue
        if is_mapping:
            # The keys name the entries, so the values are what the model describes.
            for entry, entry_table in value.items():
                if isinstance(entry_table, dict):
                    _check_model(entry_table, nested, f"{where}.{entry}", unknown)
        else:
            _check_model(value, nested, where, unknown)


def _check_parser_options(table: dict, parser_name: str, unknown: list[str]) -> None:
    """Record every parser option the configured parser does not define."""
    parser = _known_commit_parsers.get(parser_name)
    if parser is None:
        raise SystemExit(f"Unknown commit_parser {parser_name!r}; expected one of {sorted(_known_commit_parsers)}.")
    defined = {field.name for field in dataclasses.fields(parser.parser_options)}
    unknown.extend(f"tool.semantic_release.commit_parser_options.{key}" for key in table if key not in defined)


def main() -> int:
    """Check the pyproject named on the command line, or the one in the working directory."""
    pyproject = Path(sys.argv[1] if len(sys.argv) > 1 else "pyproject.toml")
    config = tomllib.loads(pyproject.read_text())["tool"]["semantic_release"]

    unknown: list[str] = []
    parser_options = config.get("commit_parser_options", {})
    # commit_parser_options is typed Dict[str, Any], so the model cannot check inside it.
    _check_model(
        {k: v for k, v in config.items() if k != "commit_parser_options"},
        RawConfig,
        "tool.semantic_release",
        unknown,
    )
    _check_parser_options(
        parser_options,
        config.get("commit_parser", RawConfig.model_fields["commit_parser"].default),
        unknown,
    )

    if unknown:
        print(f"{pyproject}: semantic-release would ignore these keys:", file=sys.stderr)
        for key in sorted(unknown):
            print(f"  {key}", file=sys.stderr)
        return 1
    print(f"{pyproject}: every semantic-release key is defined.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
