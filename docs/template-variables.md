# Template Variables

## Available Variables

### Module Interface Rules

These variables are available in rules that rename **module-installed interfaces** (triggered by `post_save` on `dcim.Module`):

| Variable | Description | Example |
|----------|-------------|---------|
| `{bay_position}` | Raw bay position string | `0`, `swp1` |
| `{bay_position_num}` | Numeric suffix of bay position | `0`, `1` |
| `{slot}` | Top-level slot/module bay position | `3` |
| `{parent_bay_position}` | Parent module's bay position | `2` |
| `{sfp_slot}` | Sub-bay index within parent module | `1` |
| `{base}` | Original interface name from NetBox template | `Interface 0` |
| `{channel}` | Breakout channel number (requires `channel_count`) | `0`, `1`, `2` |
| `{vc_position}` | Virtual Chassis member position (`device.vc_position`) | `1`, `2` |

### Device Interface Rules

These variables are available in rules with **Applies to Device Interfaces** enabled (triggered by `post_save` on `dcim.Device` — VC position change):

| Variable | Description | Example |
|----------|-------------|---------|
| `{vc_position}` | Virtual Chassis member position (`device.vc_position`) | `1`, `2` |
| `{base}` | Current full interface name (before renaming) | `Gi0/1`, `ge-0/0/3` |
| `{port}` | Segment after the last `/` in the interface name; full name if no `/` | `1` (from `Gi0/1`), `3` (from `ge-0/0/3`) |

The **Module Type Pattern** field in device interface rules acts as a **regex filter on interface names** (not a module type selector). Only interfaces whose current name matches the pattern are renamed.

## Arithmetic Expressions

Any brace-enclosed expression containing arithmetic operators is evaluated safely:

```
{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}
```

Supported operators: `+`, `-`, `*`, `//` (floor division), parentheses. Float division (`/`) is **not** supported — use `//` for integer division.

## Virtual Chassis Support

### Module Interface Rules (linecard/SFP)

When a module is installed on a device that belongs to a Virtual Chassis, the `{vc_position}` variable is injected automatically and reflects the device's chassis position (`device.vc_position`). Templates that use `{vc_position}` on non-VC devices will fail gracefully (the rename is skipped).

```yaml
name_template: "Gi{vc_position}/{bay_position_num}"
# Member at VC position 1, bay "linecard2" → Gi1/2
# Member at VC position 2, bay "linecard3" → Gi2/3
```

### Device Interface Rules (VC port renaming)

When a device **joins** a Virtual Chassis or **changes position**, the plugin fires `apply_device_interface_rules()` for that device. This renames native device-type interfaces (those not created by a module, i.e. `module=None`) using the `{vc_position}`, `{base}`, and `{port}` variables.

Enable **Applies to Device Interfaces** on the rule and set the **Module Type Pattern** as an interface-name filter (regex).

```yaml
# Cisco Catalyst 9000 stack — member 2 gets GigabitEthernet2/0/1..4
- applies_to_device_interfaces: true
  device_type: "CISCO-C9K"
  module_type_pattern: "GigabitEthernet\\d+/\\d+/\\d+"
  name_template: "GigabitEthernet{vc_position}/0/{port}"

# Juniper EX Virtual Chassis — 0-based member IDs
- applies_to_device_interfaces: true
  device_type: "JNP-EX-VC"
  module_type_pattern: "ge-\\d+/\\d+/\\d+"
  name_template: "ge-{vc_position}/0/{port}"

# Arista EOS slot/port
- applies_to_device_interfaces: true
  device_type: "ARISTA-EOS"
  module_type_pattern: "Ethernet\\d+/\\d+"
  name_template: "Ethernet{vc_position}/{port}"
```

**Re-apply on load**: If devices were added to the VC *before* rules were loaded (e.g. during initial provisioning), trigger a manual re-apply by iterating all VC member devices and calling `apply_device_interface_rules(device)` after loading rules.

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
