# Template Variables

## Available Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `{bay_position}` | Raw bay position string | `0`, `swp1` |
| `{bay_position_num}` | Numeric suffix of bay position | `0`, `1` |
| `{slot}` | Top-level slot/module bay position | `3` |
| `{parent_bay_position}` | Parent module's bay position | `2` |
| `{sfp_slot}` | Sub-bay index within parent module | `1` |
| `{base}` | Original interface name from NetBox | `Interface 0` |
| `{channel}` | Breakout channel number (requires `channel_count`) | `0`, `1`, `2` |
| `{vc_position}` | Virtual Chassis member position (`device.vc_position`); only available when the device is a VC member | `1`, `2` |

## Arithmetic Expressions

Any brace-enclosed expression containing arithmetic operators is evaluated safely:

```
{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}
```

Supported operators: `+`, `-`, `*`, `//` (floor division), parentheses. Float division (`/`) is **not** supported — use `//` for integer division.

## Virtual Chassis Support

When a device belongs to a Virtual Chassis, the `{vc_position}` variable is injected automatically and reflects the device's chassis position (`device.vc_position`). Templates that use `{vc_position}` on non-VC devices will fail gracefully (the rename is skipped).

### Virtual Chassis Linecard

```yaml
name_template: "Gi{vc_position}/{bay_position_num}"
# Member at VC position 1, bay "linecard2" → Gi1/2
# Member at VC position 2, bay "linecard3" → Gi2/3
```

Note: when a device is added to or removed from a Virtual Chassis (or its position changes), existing module-attached interfaces are **not** automatically renamed. Use the [Apply Rules](../configuration/interface-name-rules.md) UI to batch-rename.

## Examples

### Simple Rename

```yaml
name_template: "et-0/0/{bay_position}"
# Bay position 4 → et-0/0/4
```

### Breakout Channels

```yaml
name_template: "xe-0/0/{bay_position}:{channel}"
channel_count: 4
channel_start: 0
# Bay position 2 → xe-0/0/2:0, xe-0/0/2:1, xe-0/0/2:2, xe-0/0/2:3
```

### Converter Offset

```yaml
name_template: "GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}"
# Slot 3, parent bay 2, SFP slot 1 → GigabitEthernet3/11
```

### UfiSpace SONiC

```yaml
name_template: "swp{bay_position_num}"
# Bay position swp5 → swp5
```
