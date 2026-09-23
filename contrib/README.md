# Contributing Interface Name Rules

This directory contains example interface name rules organized by vendor.
These YAML files can be imported directly into NetBox via **Interface Name Rules > Import**.

## Files

| File | Description |
|------|-------------|
| `cisco.yaml` | Cisco IOS-XR platforms (8201-SYS, etc.) |
| `juniper.yaml` | Juniper Junos platforms (ACX7024, ACX7100-32C, etc.) — ge/xe/et naming with breakout |
| `juniper-channelized.yaml` | The Juniper breakout families of `juniper.yaml` in `channelized` mode (NetBox 4.7+) — import instead of, not alongside |
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
5. On NetBox 4.7+ a breakout rule can build the channelized topology instead of flat siblings:
   - `breakout_mode: channelized` — the base becomes a physical parent with `channels` set and one
     channel subinterface is created per channel (default: `flat`, the sibling-interface behaviour)
   - `parent_name_template` — name for that parent; same variables as `name_template` minus
     `{channel}`. Blank keeps the port's current name. See `contrib/juniper-channelized.yaml`.
6. Run `yamllint` on your file before submitting

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
  name_template: "GigabitEthernet{slot_num}/{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}"
```

See `contrib/ufispace.yaml` for a real-world example of the pattern mode and `contrib/converters.yaml` for parent module scoping.

## Template variables

<!-- BEGIN GENERATED TEMPLATE VARIABLE REFERENCE -->
### Module member names

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
| `{base}` | The name the rule starts from, which is the module's raw template name when the rule first applies. | `et-0/0/1` |
| `{vc_position}` | Virtual Chassis member position. Available only on a member device. | `2` |
| `{channel}` | Breakout channel number. Available when the rule declares channels. | `0` |

### Module parent names

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
| `{base}` | The name the rule starts from, which is the module's raw template name when the rule first applies. | `et-0/0/1` |
| `{vc_position}` | Virtual Chassis member position. Available only on a member device. | `2` |

### Device interface names

Device-interface rules use these variables in the Name Template field.

| Variable | Description | Example |
|----------|-------------|---------|
| `{base}` | Current interface name before the rule applies. | `et-0/0/1` |
| `{vc_position}` | Virtual Chassis member position. Available only on a member device. | `2` |
| `{port}` | Segment after the last slash in the current interface name. Uses the full name when no slash is present. | `1` |
<!-- END GENERATED TEMPLATE VARIABLE REFERENCE -->

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
Variables like `{parent_bay_position_num}` and `{sfp_slot}` are substituted first,
then any brace group that resolves to a pure arithmetic expression is evaluated
safely via Python `ast` (only `+`, `-`, `*`, `//` and parentheses are allowed).
**Float division `/` is not supported and will raise an error — use integer
division `//` instead (e.g. `{{slot_num} // 2}`).**
A variable is substituted only as the exact token `{slot_num}`, so it keeps its
own braces inside an arithmetic expression.

```yaml
# Converter offset naming — arithmetic in name_template
- module_type_pattern: "SFP-.*"
  module_type_is_regex: true
  parent_module_type: CVR-X2-SFP
  name_template: "swp{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}"
```

For example, with `parent_bay_position_num=1` and `sfp_slot=0` the expression
evaluates to `8 + (1 - 1) * 2 + 0 = 8`, producing `swp8`.
