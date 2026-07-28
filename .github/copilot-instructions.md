# Copilot Instructions

## Project overview

NetBox plugin that automatically renames interfaces when modules (transceivers, line cards, converters) are installed into device module bays. It hooks into Django's `post_save` signal on `dcim.Module` to apply configurable renaming rules with template variable substitution and arithmetic expression support.

Requires NetBox ≥ 4.2.0 and Python ≥ 3.12. Licensed under Apache-2.0 (REUSE-compliant).

## Architecture

This follows the standard [NetBox plugin pattern](https://netboxlabs.com/docs/netbox/en/stable/plugins/development/):

- **`models.py`** — Single model `InterfaceNameRule` linking a module type (nullable FK, required only in exact mode) to a name template, with optional scoping to parent module type, device type, and/or platform. Rules are matched most-specific-first.
- **`signals.py`** — Two signal handlers:
  - `post_save` on `dcim.Module` (primary path): fires on `created=True`, defers renaming to `on_commit` so interfaces exist in DB first. This is the **only** path that runs during normal module installation because NetBox creates interfaces via `bulk_create()`.
  - `pre_save` on `dcim.Interface` (defence-in-depth): would rename before INSERT, but **does NOT fire** during normal module installation because `bulk_create()` skips `pre_save` signals. Only fires when interfaces are created individually via `Interface.save()` (scripts, custom code, etc.).

  Lazily imports the engine to avoid circular imports during Django startup.
- **`engine.py`** — Core logic: two-tier rule lookup (`_find_matching_rule` first tries an exact FK match across 4 specificity levels, then falls back to regex matching with `re.fullmatch()` across the same 4 levels), template variable building from module bay hierarchy, and interface renaming/breakout creation. The `evaluate_name_template` function supports `{variable}` substitution followed by safe AST-based arithmetic evaluation of remaining brace expressions.
- **`api/`** — DRF REST API using NetBox's `NetBoxModelViewSet` and `NetBoxModelSerializer`.
- **`views.py`, `urls.py`, `tables.py`, `forms.py`, `filters.py`, `navigation.py`** — Standard NetBox UI CRUD views.
- **`utils.py`** — Feature detection for gating (e.g., `{module_path}` token support detected via `dcim.constants.MODULE_PATH_TOKEN` import).

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

- All views, forms, serializers, and tables inherit from NetBox's base classes (`NetBoxModel`, `NetBoxModelViewSet`, `NetBoxModelForm`, etc.) — always use these, not raw Django/DRF equivalents. Non-model forms are the exception: NetBox 4.x dropped `BootstrapMixin` and styles every form through its own widget templates (`FORM_RENDERER = TemplatesSetting`), so a plain form subclasses `django.forms.Form`, exactly as NetBox's own `ConfirmationForm`/`BulkRenameForm` do.
- Template variables use Python `str.format()` syntax: `{slot}`, `{bay_position}`, `{bay_position_num}`, `{parent_bay_position}`, `{sfp_slot}`, `{base}`, `{channel}`, `{module_path}` (gated via `utils.supports_module_path()` using import-based feature detection).
- Arithmetic inside braces is evaluated via `ast.parse` with a strict allowlist of AST node types — never use `eval()` directly on user input.
- The `tags` field on `InterfaceNameRule` uses `related_name="+"` to avoid reverse accessor clashes with other plugins.
- Rule matching uses **two tiers** within each priority level — exact FK match first, then regex (`re.fullmatch()`) fallback. Priority levels (applied in both tiers): (module_type + parent + device) → (module_type + parent) → (module_type + device) → (module_type only).
- Add new rules to the appropriate vendor-specific file under `contrib/` (`cisco.yaml`, `juniper.yaml`, `linux.yaml`, `ufispace.yaml`, `ufispace-device-type.yaml`, `converters.yaml`) — keep them updated when adding new rule patterns.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/) format, enforced by pre-commit hook.
