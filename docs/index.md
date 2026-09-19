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
- **Arithmetic expressions** — `{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}`
- **Breakout support** — create multiple channel interfaces from a single port
- **Bounded regex pattern matching** — match module types with RE2 patterns (e.g., `QSFP-DD-400G-.*`) to cover entire product families with a single rule; exact FK match takes priority over regex
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

## What a rule cannot do

A rule renames interfaces that NetBox has already created. The plugin runs on `post_save` of
`dcim.Module`, because NetBox creates module interfaces with `bulk_create`, which fires no
`pre_save` on the interfaces themselves.

A rename therefore runs after the insert, never before it. Two consequences:

- **A rule cannot resolve a name collision.** If a device type gives two bays the same position,
  installing the second module fails on the `dcim_interface_unique_device_name` constraint at
  insert time, before any rule runs. Fix that in the device type, by composing the parent into
  the bay position, rather than with a rename rule.
- **A rule cannot stop NetBox from creating an interface.** A breakout rule does create the
  channel interfaces of a family, but only after NetBox has inserted the one it starts from.

Composed bay positions also mean most transceivers already arrive with the right name, so a
rename rule is for the names that position alone cannot produce, such as the converter offset
above.

## Quick Start

```bash
pip install netbox-interface-name-rules
```

See [Installation](installation.md) for full setup instructions, then
[Examples](examples.md) for real-world rules for Juniper, SONiC, Linux, and more.

## Additional Resources

- [DeepWiki](https://deepwiki.com/marcinpsk/netbox-InterfaceNameRules-plugin) — AI-generated codebase overview
