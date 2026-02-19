# Copilot Instructions

## Project overview

NetBox plugin that automatically renames interfaces when modules (transceivers, line cards, converters) are installed into device module bays. It hooks into Django's `post_save` signal on `dcim.Module` to apply configurable renaming rules with template variable substitution and arithmetic expression support.

Requires NetBox ≥ 4.2.0 and Python ≥ 3.12. Licensed under Apache-2.0 (REUSE-compliant).

## Architecture

This follows the standard [NetBox plugin pattern](https://netboxlabs.com/docs/netbox/en/stable/plugins/development/):

- **`models.py`** — Single model `InterfaceNameRule` linking a module type to a name template, with optional scoping to parent module type and/or device type. Rules are matched most-specific-first.
- **`signals.py`** — `post_save` receiver on `dcim.Module`. Only fires on `created=True`. Lazily imports the engine to avoid circular imports during Django startup.
- **`engine.py`** — Core logic: rule lookup (`_find_matching_rule` with 4-level priority fallback), template variable building from module bay hierarchy, and interface renaming/breakout creation. The `evaluate_name_template` function supports `{variable}` substitution followed by safe AST-based arithmetic evaluation of remaining brace expressions.
- **`api/`** — DRF REST API using NetBox's `NetBoxModelViewSet` and `NetBoxModelSerializer`.
- **`views.py`, `urls.py`, `tables.py`, `forms.py`, `filters.py`, `navigation.py`** — Standard NetBox UI CRUD views.
- **`utils.py`** — Version detection for feature gating (e.g., `{module_path}` token support in NetBox ≥ 4.9.0).

The signal handler → engine import is intentionally lazy to ensure Django models are fully loaded before use.

## Development environment

Uses a devcontainer (`.devcontainer/`) running the `netboxcommunity/netbox` Docker image with PostgreSQL and Redis. Both plugin repos are mounted live from the host at `/workspaces/<repo-name>`.

Local development uses `uv` for dependency management. Run `direnv allow` to activate the `.envrc` which runs `uv sync` and activates the venv.

```bash
# Start NetBox dev server (inside devcontainer)
netbox-run          # foreground
netbox-run-bg       # background
netbox-stop         # stop
netbox-restart      # restart

# Reinstall plugin and restart
netbox-reload

# Run Django management commands
netbox-manage migrate
netbox-manage makemigrations netbox_interface_name_rules

# All aliases — type 'dev-help' inside the devcontainer
```

### Extra plugins

To install additional plugins in the devcontainer, copy `.devcontainer/config/extra-plugins.py.example` to `extra-plugins.py` and add entries. Supports both PyPI packages and local editable installs.

## Linting

Uses Ruff (configured in `pyproject.toml`) with pre-commit hooks. Shellcheck validates shell scripts.

```bash
ruff check .          # lint
ruff format .         # format
ruff check --fix .    # lint + auto-fix
```

Key Ruff settings: line-length 120, ignores E501/F403/F405 globally, ignores F401 in `__init__.py`.

## Testing

```bash
# Run all plugin tests (inside devcontainer)
netbox-test

# Or manually from /opt/netbox/netbox:
python manage.py test netbox_interface_name_rules

# Run a single test
python manage.py test netbox_interface_name_rules.tests.TestClassName.test_method_name
```

## REUSE/SPDX compliance

All source files must include SPDX headers. Files that don't support comments (JSON, .gitignore) are covered by glob patterns in `REUSE.toml`.

```
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
```

The `reuse-lint` pre-commit hook validates compliance on every commit.

## Key conventions

- All views, forms, serializers, and tables inherit from NetBox's base classes (`NetBoxModel`, `NetBoxModelViewSet`, `NetBoxModelForm`, etc.) — always use these, not raw Django/DRF equivalents.
- Template variables use Python `str.format()` syntax: `{slot}`, `{bay_position}`, `{bay_position_num}`, `{parent_bay_position}`, `{sfp_slot}`, `{base}`, `{channel}`.
- Arithmetic inside braces is evaluated via `ast.parse` with a strict allowlist of AST node types — never use `eval()` directly on user input.
- The `tags` field on `InterfaceNameRule` uses `related_name="+"` to avoid reverse accessor clashes with other plugins.
- Rule matching priority: (module_type + parent + device) → (module_type + parent) → (module_type + device) → (module_type only).
- `contrib/interface_name_rules.yaml` contains example rules for bulk import — keep it updated when adding new rule patterns.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/) format, enforced by pre-commit hook.
