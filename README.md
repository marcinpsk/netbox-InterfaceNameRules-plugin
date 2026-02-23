# NetBox Interface Name Rules Plugin

[![PyPI](https://img.shields.io/pypi/v/netbox-interface-name-rules)](https://pypi.org/project/netbox-interface-name-rules/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/netbox-interface-name-rules)](https://pypi.org/project/netbox-interface-name-rules/)
[![CI](https://img.shields.io/github/actions/workflow/status/marcinpsk/netbox-InterfaceNameRules-plugin/test.yaml?branch=main&label=tests)](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/actions/workflows/test.yaml)
[![License](https://img.shields.io/github/license/marcinpsk/netbox-InterfaceNameRules-plugin)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/netbox-interface-name-rules)](https://pypi.org/project/netbox-interface-name-rules/)
[![NetBox](https://img.shields.io/badge/NetBox-%E2%89%A54.2.0-blue)](https://github.com/netbox-community/netbox)
[![Contributors](https://img.shields.io/github/contributors/marcinpsk/netbox-InterfaceNameRules-plugin)](https://github.com/marcinpsk/netbox-InterfaceNameRules-plugin/graphs/contributors)

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
PLUGINS = ['netbox_interface_name_rules']
```

## Compatibility

- NetBox ≥ 4.2.0
- Python ≥ 3.12

## License

Apache 2.0

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to submit code or interface name rules.

Community-contributed rules for various vendors are in the [`contrib/`](contrib/) directory.
