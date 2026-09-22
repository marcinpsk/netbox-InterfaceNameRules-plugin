# Copilot Instructions

## Project overview

NetBox plugin that automatically renames interfaces when modules (transceivers, line cards, converters) are installed into device module bays. It also reapplies rules when a module type or a device's virtual-chassis position changes. Django signals defer the work until the surrounding transaction commits.

Requires NetBox ≥ 4.3.0 and Python ≥ 3.12. Licensed under Apache-2.0 (REUSE-compliant).

## Architecture

This follows the standard [NetBox plugin pattern](https://netboxlabs.com/docs/netbox/en/stable/plugins/development/):

- **`models.py`**: Defines `InterfaceNameRule`. A rule can select an exact module type or a regex pattern, add parent, device, and platform scopes, and describe flat or channelized breakout output.
- **`signals.py`**: Handles `pre_save` and `post_save` for `dcim.Module` and `dcim.Device`. It records prior state, schedules work with `transaction.on_commit()`, and catches failures at the deferred callback boundary. It intentionally does not connect to `dcim.Interface` because NetBox creates module interfaces with `bulk_create()`. It also connects the optional LibreNMS prediction signal when that plugin is installed.
- **`rule_selection.py`**: Loads and fingerprints enabled rules, separates exact and regex candidates, applies scope priority, and pins one cached snapshot across batch work.
- **`name_template.py`**: Owns the name-template language, its template-variable catalogue, and evaluation.
- **`naming.py`**: Builds template-variable values from the module-bay hierarchy.
- **`family/`**: Owns the interface-family domain model, discovery, planning, execution, structural creation, conversion, name collision checks, and NetBox capability detection.
- **`engine.py`**: Orchestrates rule application, prediction, virtual-chassis reapply, preview, and batch operations. It keeps stable entry points while delegating rule selection, naming, and family behavior to their owning modules.
- **`api/` and `graphql/`**: Expose NetBox REST and GraphQL integrations.
- **`views.py`, `urls.py`, `tables.py`, `forms.py`, `filters.py`, `navigation.py`, `jobs.py`**: Provide NetBox UI and background-job integrations.

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
- Use the public surface in `name_template.py` for the name-template language. Do not restate or reimplement its parsing rules.

<!-- BEGIN GENERATED TEMPLATE VARIABLE REFERENCE -->
### Template variables

#### Module member names

Module rules use these variables in the Name Template field.

| Variable | Description | Example |
|----------|-------------|---------|
| `{slot}` | Top-level module bay position. | `Slot 3` |
| `{slot_num}` | Numeric suffix of the top-level module bay position. | `3` |
| `{bay_position}` | Position of the bay that holds the module. | `swp1` |
| `{bay_position_num}` | Numeric suffix of the module bay position. | `1` |
| `{parent_bay_position}` | Position of the parent module's bay. | `TenGigabitEthernet3/2` |
| `{parent_bay_position_num}` | Numeric suffix of the parent module's bay position. | `2` |
| `{sfp_slot}` | Numeric sub-bay index within the parent module. | `0` |
| `{base}` | Raw template name on first apply. Current name of the family's base interface on reapply. | `et-0/0/1` |
| `{vc_position}` | Virtual Chassis member position. Available only on a member device. | `2` |
| `{channel}` | Breakout channel number. Available when the rule declares channels. | `0` |

#### Module parent names

Module rules use these variables in the Parent Name Template field.

| Variable | Description | Example |
|----------|-------------|---------|
| `{slot}` | Top-level module bay position. | `Slot 3` |
| `{slot_num}` | Numeric suffix of the top-level module bay position. | `3` |
| `{bay_position}` | Position of the bay that holds the module. | `swp1` |
| `{bay_position_num}` | Numeric suffix of the module bay position. | `1` |
| `{parent_bay_position}` | Position of the parent module's bay. | `TenGigabitEthernet3/2` |
| `{parent_bay_position_num}` | Numeric suffix of the parent module's bay position. | `2` |
| `{sfp_slot}` | Numeric sub-bay index within the parent module. | `0` |
| `{base}` | Raw template name on first apply. Current name of the family's base interface on reapply. | `et-0/0/1` |
| `{vc_position}` | Virtual Chassis member position. Available only on a member device. | `2` |

#### Device interface names

Device-interface rules use these variables in the Name Template field.

| Variable | Description | Example |
|----------|-------------|---------|
| `{base}` | Current interface name before the rule applies. | `et-0/0/1` |
| `{vc_position}` | Virtual Chassis member position. Available only on a member device. | `2` |
| `{port}` | Segment after the last slash in the current interface name. Uses the full name when no slash is present. | `1` |
<!-- END GENERATED TEMPLATE VARIABLE REFERENCE -->

- The `tags` field on `InterfaceNameRule` uses `related_name="+"` to avoid reverse accessor clashes with other plugins.
- Rule matching uses two tiers. Exact module-type rules take priority over regex rules. Within each tier, `rule_selection.py` applies the parent, device, and platform scope score, then the documented tie breakers.
- Add new rules to the appropriate vendor-specific file under `contrib/` (`cisco.yaml`, `juniper.yaml`, `linux.yaml`, `ufispace.yaml`, `ufispace-device-type.yaml`, `converters.yaml`) — keep them updated when adding new rule patterns.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/) format, enforced by pre-commit hook.
