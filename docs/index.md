# NetBox Interface Name Rules

Automatic interface renaming when modules are installed into NetBox device bays.

## The Problem

When a module (transceiver, line card, converter) is installed into a module bay,
NetBox creates interfaces using position-based naming from the module type template.
This often produces incorrect names — e.g., `Interface 1` instead of `et-0/0/4`.

## The Solution

This plugin hooks into Django's `post_save` signal on the `Module` model to
automatically apply renaming rules based on configurable templates.

## Features

- **Signal-driven** — rules fire automatically on module install
- **Template variables** — `{slot}`, `{bay_position}`, `{bay_position_num}`, `{channel}`, etc.
- **Arithmetic expressions** — `{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}`
- **Breakout support** — create multiple channel interfaces from a single port
- **Regex pattern matching** — match module types by regex pattern (e.g., `QSFP-DD-400G-.*`) to cover entire product families with a single rule; exact FK match takes priority over regex
- **Scoping** — rules can target specific device types, parent module types, platforms, or be universal
- **Build Rule tester** — interactive form to preview name output and test against installed interfaces before saving
- **Apply Rules** — batch rename existing interfaces with live preview and background job support

## Supported Scenarios

| Scenario | Example |
|----------|---------|
| Simple rename | QSFP-100G-LR4 on ACX7024 → `et-0/0/{bay_position}` |
| Breakout channels | QSFP-4X10G-LR → `xe-0/0/4:0` through `xe-0/0/4:3` |
| Converter offset | GLC-T in CVR-X2-SFP → `GigabitEthernet3/10` |
| Platform naming | swp{bay_position_num} for UfiSpace SONiC devices |
| Linux server | eth{bay_position_num} or ens{slot}f{bay_position_num} |

## Quick Start

```bash
pip install netbox-interface-name-rules
```

See [Installation](installation.md) for full setup instructions, then
[Examples](examples.md) for real-world rules for Juniper, SONiC, Linux, and more.

## Additional Resources

- [DeepWiki](https://deepwiki.com/marcinpsk/netbox-InterfaceNameRules-plugin) — AI-generated codebase overview
