# NetBox Interface Name Rules Plugin

[![PyPI](https://img.shields.io/pypi/v/netbox-interface-name-rules)](https://pypi.org/project/netbox-interface-name-rules/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/netbox-interface-name-rules)](https://pypi.org/project/netbox-interface-name-rules/)
[![CI](https://img.shields.io/github/actions/workflow/status/marcinpsk/netbox-InterfaceNameRules-plugin/test.yaml?branch=main&label=tests)](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/actions/workflows/test.yaml)
[![Coverage](https://img.shields.io/endpoint?url=https://marcinpsk.github.io/netbox-InterfaceNameRules-plugin/coverage/badge.json)](https://marcinpsk.github.io/netbox-InterfaceNameRules-plugin/coverage/)
[![License](https://img.shields.io/github/license/marcinpsk/netbox-InterfaceNameRules-plugin)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/netbox-interface-name-rules)](https://pypi.org/project/netbox-interface-name-rules/)
[![NetBox](https://img.shields.io/badge/NetBox-%E2%89%A54.3.0-blue)](https://github.com/netbox-community/netbox)
[![Contributors](https://img.shields.io/github/contributors/marcinpsk/netbox-InterfaceNameRules-plugin)](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/graphs/contributors)
[![REUSE status](https://api.reuse.software/badge/github.com/marcinpsk/netbox-InterfaceNameRules-plugin)](https://api.reuse.software/info/github.com/marcinpsk/netbox-InterfaceNameRules-plugin)

Automatic interface renaming when modules are installed into NetBox device bays.

## What it does

When a module (transceiver, line card, converter) is installed into a module bay,
NetBox creates interfaces using position-based naming from the module type template.
This often produces incorrect names — e.g., `Interface 1` instead of `et-0/0/4`.

This plugin hooks into Django's `post_save` signal on the `Module` model to
automatically apply renaming rules based on configurable templates.

## Features

- **Signal-driven** — rules fire automatically on module install, no manual step needed
- **Template variables** — `{slot}`, `{bay_position}`, `{bay_position_num}`, `{base}`, `{channel}`, etc.
- **Arithmetic expressions** — `{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}`
- **Breakout support** — create multiple channel interfaces from a single port (e.g., QSFP+ 4x10G)
- **Scoping** — rules can be scoped to specific device types, parent module types, or be universal
- **Bulk import/export** — YAML-based rule management via the UI or API

## Supported scenarios

| Scenario | Example |
|----------|---------|
| Converter offset | GLC-T in CVR-X2-SFP → `GigabitEthernet3/10` |
| Breakout channels | QSFP-4X10G-LR → `et-0/0/4:0` through `et-0/0/4:3` |
| Platform naming | QSFP-100G-LR4 on ACX7024 → `et-0/0/{bay_position}` |
| UfiSpace breakout | QSFP-100G on S9610 → `swp{bay_position_num}s{channel}` |

## Installation

```bash
pip install netbox-interface-name-rules
```

Add to `configuration.py`:
```python
PLUGINS = ["netbox_interface_name_rules"]
```

## Configuration

Rules are managed through the NetBox UI under **Plugins → Interface Name Rules**, or via the REST API at `/api/plugins/interface-name-rules/rules/`.

See the [full configuration guide](https://marcinpsk.github.io/netbox-InterfaceNameRules-plugin/configuration/) for all rule fields, priority scoring, and template variable reference.

## Screenshots
<p align="center">
  <img src="https://raw.githubusercontent.com/marcinpsk/netbox-InterfaceNameRules-plugin/main/docs/icon.svg" alt="NetBox Interface Name Rules" width="30"/>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/marcinpsk/netbox-InterfaceNameRules-plugin/main/docs/screenshots/01-rule-list.png" alt="Rule list" width="700"/>
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/marcinpsk/netbox-InterfaceNameRules-plugin/main/docs/screenshots/11-apply-rule-preview.png" alt="Apply-rules preview" width="700"/>
</p>

## Compatibility

- NetBox ≥ 4.3.0 (CI tests 4.3, 4.5, 4.7, plus NetBox `main` and `feature` as non-blocking early warnings)
- Python ≥ 3.12

## License

Apache 2.0

## Documentation

- [Full documentation](https://marcinpsk.github.io/netbox-InterfaceNameRules-plugin/) — installation, configuration, examples
- [DeepWiki](https://deepwiki.com/marcinpsk/netbox-InterfaceNameRules-plugin) — AI-generated codebase overview

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to submit code or interface name rules.

Community-contributed rules for various vendors are in the [`contrib/`](contrib/) directory.

## Testing

Inside the devcontainer:

```bash
netbox-test                 # run this plugin's tests (shared test_netbox DB)
netbox-test-isolated <app> [flags]   # run on a per-session ISOLATED test DB
```

`netbox-test-isolated` is concurrency-safe: Django otherwise names the test
database `test_<DB_NAME>` (`test_netbox`), so two `manage.py test` runs in the
same devcontainer collide on one DB and corrupt each other's migrations (a
crashed run can even leave an `idle in transaction` connection holding locks that
wedges every later run). The helper points the test DB at a unique name —
derived from the first app argument, or `TEST_DB_NAME=...` — via the
[`isolated_test_settings`](.devcontainer/config/isolated_test_settings.py) shim
(`--settings=isolated_test_settings` with `.devcontainer/config` on `PYTHONPATH`).
Plain `netbox-test` keeps using the shared default (`test_netbox`);
`netbox-test-isolated` instead derives a per-app DB name by default — `test_<app>`
from the first app argument (this plugin when none is given) — unless you set
`TEST_DB_NAME` explicitly. Example — test a sibling plugin without disturbing a
parallel run:

```bash
netbox-test-isolated netbox_nso_plugin --keepdb
```
