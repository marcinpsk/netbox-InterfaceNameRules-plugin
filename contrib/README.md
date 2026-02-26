# Contributing Interface Name Rules

This directory contains example interface name rules organized by vendor.
These YAML files can be imported directly into NetBox via **Interface Name Rules > Import**.

## Files

| File | Description |
|------|-------------|
| `cisco.yaml` | Cisco IOS-XR platforms (8201-SYS, etc.) |
| `juniper.yaml` | Juniper Junos platforms (ACX7024, ACX7100-32C, etc.) — ge/xe/et naming with breakout |
| `ufispace.yaml` | UfiSpace SONiC platforms (platform-scoped, requires "SONiC" Platform object) |
| `ufispace-device-type.yaml` | UfiSpace SONiC platforms (device-type-scoped, no Platform required) |
| `linux.yaml` | Linux servers — traditional eth0/eth1, systemd predictable ens{slot}f{N}, Mellanox breakout |
| `converters.yaml` | Media converter offset rules (CVR-X2-SFP, etc.) |

## Adding rules

1. Create or edit the vendor YAML file
2. Follow the existing format — each rule needs **`name_template`** and **either**:
   - `module_type` — exact match to the NetBox module type's **`model`** field, **or**
   - `module_type_pattern` + `module_type_is_regex: true` — regex applied via `re.fullmatch()` against the module type's **`model`** field
3. Optionally narrow the rule with scoping fields (all optional):
   - `device_type` — restrict to devices of this device type (FK; matched by `model` field)
   - `platform` — restrict to devices running this platform (FK; matched by platform `name` as shown in NetBox UI, e.g. `platform: SONiC`)
   - `parent_module_type` — restrict to modules installed inside another module of this type (FK; matched by `model` field; see `contrib/converters.yaml` for a real-world example)
4. For breakout/channel interfaces, set `channel_count` (integer) to enable `{channel}` indexing:
   - `channel_count` — number of breakout channels; enables `{channel}` in `name_template` (e.g., `channel_count: 4`)
   - `channel_start` — starting channel index (default: 0)
5. Run `yamllint` on your file before submitting

### Examples

```yaml
# Exact match
- module_type: SFP-10G-LR
  device_type: ACX7024
  name_template: "xe-0/0/{bay_position}"

# Regex pattern (covers all QSFP-100G-* variants)
- module_type_pattern: "QSFP-100G-.*"
  module_type_is_regex: true
  device_type: ACX7024
  name_template: "et-0/0/{bay_position}"

# Regex catch-all for a device (ufispace.yaml style)
- module_type_pattern: ".*"
  module_type_is_regex: true
  device_type: S9610-36D
  name_template: "swp{bay_position_num}"
```

```yaml
# Parent module scoping (SFP inside an X2-to-SFP converter)
- module_type: SFP-1G-T
  parent_module_type: CVR-X2-SFP
  name_template: "GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}"
```

See `contrib/ufispace.yaml` for a real-world example of the pattern mode and `contrib/converters.yaml` for parent module scoping.

## Template variables

| Variable | Description |
|----------|-------------|
| `{bay_position}` | Raw bay position string |
| `{bay_position_num}` | Numeric suffix of bay position |
| `{slot}` | Top-level slot/module bay position |
| `{parent_bay_position}` | Parent module's bay position |
| `{sfp_slot}` | Sub-bay index within parent module |
| `{base}` | Base interface name from NetBox position resolution |
| `{vc_position}` | Virtual Chassis member position (`device.vc_position`); only injected when the device is a VC member — templates using this variable will produce no rename on non-VC devices |
| `{channel}` | Breakout channel number (requires `channel_count`; range starts at `channel_start`, default `0`) |

`channel_count` is a rule configuration field (not a template variable) that enables breakout mode.
Set it to the number of sub-interfaces to create per module, e.g.:

```yaml
- module_type_pattern: "QSFP-DD-400G-.*"
  module_type_is_regex: true
  channel_count: 4
  channel_start: 0
  name_template: "{base}:{channel}"
```

With `channel_count: 4` and `{base}` resolving to `et-0/0/1`, this creates
`et-0/0/1:0`, `et-0/0/1:1`, `et-0/0/1:2`, and `et-0/0/1:3`.

Arithmetic expressions are supported inside `name_template`.
Variables like `{parent_bay_position}` and `{sfp_slot}` are substituted first,
then any brace group that resolves to a pure arithmetic expression is evaluated
safely via Python `ast` (only `+`, `-`, `*`, `//` and parentheses are allowed).
**Float division `/` is not supported and will raise an error — use integer
division `//` instead (e.g. `{slot // 2}`).**

```yaml
# Converter offset naming — arithmetic in name_template
- module_type_pattern: "SFP-.*"
  module_type_is_regex: true
  parent_module_type: CVR-X2-SFP
  name_template: "swp{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}"
```

For example, with `parent_bay_position=1` and `sfp_slot=0` the expression
evaluates to `8 + (1 - 1) * 2 + 0 = 8`, producing `swp8`.
