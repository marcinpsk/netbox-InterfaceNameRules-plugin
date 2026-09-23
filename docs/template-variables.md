# Template Variables

## Available Variables

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
| `{base}` | The raw template name of the interface the rule renames. | `et-0/0/1` |
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
| `{base}` | The raw template name of the interface the rule renames. | `et-0/0/1` |
| `{vc_position}` | Virtual Chassis member position. Available only on a member device. | `2` |

### Device interface names

Device-interface rules use these variables in the Name Template field.

| Variable | Description | Example |
|----------|-------------|---------|
| `{base}` | Current interface name before the rule applies. | `et-0/0/1` |
| `{vc_position}` | Virtual Chassis member position. Available only on a member device. | `2` |
| `{port}` | Segment after the last slash in the current interface name. Uses the full name when no slash is present. | `1` |
<!-- END GENERATED TEMPLATE VARIABLE REFERENCE -->

### Device interface rule filter

The **Module Type Pattern** field in device interface rules acts as a **regex filter on interface names** (not a module type selector). Only interfaces whose current name matches the pattern are renamed.

### What `{base}` starts from

In a module rule, `{base}` is the raw template name of the interface the rule renames: the name
the module type's interface template resolves to now. It does not change when the rule runs again,
so a rule that uses `{base}` derives the same names on install and on every reapply. A flat
breakout family, an installed channelized family and a plain interface rename all read it this way.
The parent and every channel of a channelized family receive the parent's base value.

NetBox does not record which template created an interface. For a rule that uses `{base}`, the
plugin finds the template by the interface's name: the raw name itself, a name NetBox gave the
interface at an earlier virtual-chassis position, or the name the rule gives the raw name at any
virtual-chassis position. A `{vc_position}` inside an arithmetic expression matches only the current
position. An interface that no template claims, or that more than one template claims, keeps its name. The plugin logs the reason. A module type without interface templates has
no raw names, so there `{base}` is the interface's current name.

In a device interface rule, `{base}` is the interface's current name.

## Arithmetic Expressions

Any brace-enclosed expression containing arithmetic operators is evaluated safely:

```
{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}
```

Supported operators: `+`, `-`, `*`, `//` (floor division), parentheses. Float division (`/`) is **not** supported — use `//` for integer division.

### Use the `_num` variables in arithmetic

A device type may compose the parent into a bay position, the way the Catalyst 4900M and the
MX304 do, so that a nested bay reads `TenGigabitEthernet3/2/1` rather than `1`. Positions are
therefore path-shaped strings in the general case, and arithmetic on one fails:

```
Unsafe expression in name template: 8 + (TenGigabitEthernet3/2 - 1) * 2 + 1
```

Every position has a `_num` counterpart holding the number its digits spell, so a padded run
such as `02` reads `2`, which is what arithmetic accepts. Use those wherever a template does
arithmetic. They are also correct for a plain numeric position, so `_num` is the safe default;
the raw variable still gives the position as it is stored.

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
  module_type_pattern: "GigabitEthernet[0-9]+/[0-9]+/[0-9]+"
  name_template: "GigabitEthernet{vc_position}/0/{port}"

# Juniper EX Virtual Chassis — 0-based member IDs
- applies_to_device_interfaces: true
  device_type: "JNP-EX-VC"
  module_type_pattern: "ge-[0-9]+/[0-9]+/[0-9]+"
  name_template: "ge-{vc_position}/0/{port}"

# Arista EOS slot/port
- applies_to_device_interfaces: true
  device_type: "ARISTA-EOS"
  module_type_pattern: "Ethernet[0-9]+/[0-9]+"
  name_template: "Ethernet{vc_position}/{port}"
```

**Re-apply on load**: If devices were added to the VC *before* rules were loaded (e.g. during initial provisioning), trigger a manual re-apply by iterating all VC member devices and calling `apply_device_interface_rules(device)` after loading rules.

### Two different `{vc_position}` tokens

NetBox 4.6 added a `{vc_position}` token to **component template** names (`InterfaceTemplate.name` on
a device type or module type). It is spelled the same as this plugin's rule variable, but it is a
different thing with different timing:

| | NetBox template token | Plugin rule variable |
|---|---|---|
| Where it is written | `InterfaceTemplate.name`, e.g. `xe-{vc_position:0}/0/{module}` | `name_template` / `parent_name_template` on an InterfaceNameRule |
| When it is resolved | **Once**, when NetBox instantiates the interface — the member's position, else the explicit fallback `X` in `{vc_position:X}`, else `0` | Every time the rule is applied |
| After the device moves | Never re-resolved: the interface keeps the name it was given | Re-applied on VC join and on a position change |

The two are independent — a module type can use the token without any rule, and a rule can use the
variable on a module type whose templates do not.

**Why this matters for renaming.** The plugin decides whether an interface still carries its raw
template name by comparing it against what the templates resolve to *now*. On a module type that
uses the token, that comparison drifts the moment the device joins a virtual chassis, moves inside
one, or leaves it: an interface instantiated as `xe-0/0/3` on a standalone device is still named
`xe-0/0/3` after the device joins at position 2, while the template now resolves to `xe-2/0/3`.

The plugin therefore matches such names **structurally**: each template that uses the token
contributes a matcher covering every value the token can take (any member position, the `0` default,
and the explicit `X` of a `{vc_position:X}`), so one matcher recognises the name whatever position
the interface was named at. Matching stays conservative — if a matcher could claim two interfaces,
or two templates could claim one interface, nothing is renamed and a warning names the module, the
templates and the candidates. Module types that do not use the token keep the exact matching they
always had, and so does every release older than NetBox 4.6.

Note that the drift is only reachable on a **re-apply**: at install time the interfaces are named
and the rule is applied in the same instant, so the two always agree.

**Leaving a virtual chassis does not rename anything.** The plugin deliberately schedules no
re-apply when a device is removed from a VC — a rule using `{vc_position}` cannot even be evaluated
off a chassis, so what the interfaces should be called instead is an operator decision. Re-apply the
rule manually from **Apply Rules → Preview & Apply** when you want it; the matching above finds the
interfaces whether they were named at a position or at the fallback.

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

On NetBox releases that model channelized interfaces (4.7+), a module whose templates already
create a channelized family — a physical parent with `channels` set plus its channel
subinterfaces — is renamed rather than expanded: the existing channels take the names the
template produces, `{channel}` is `channel_start + channel_id - 1`, no interfaces are created,
and the parent keeps its own name unless the rule names it explicitly through
`parent_name_template`. A breakout rule (non-zero `channel_count`) whose count
disagrees with the parent's `channels` skips that family and logs a warning instead of renaming
it into a shape it does not have. On older releases nothing changes: the base is renamed to the
first channel and the remaining channels are created as flat sibling interfaces.

For a simple rule (`channel_count: 0`, the default), a channelized family is renamed in lockstep: the parent gets
the template's name and each channel keeps its own suffix (`et-0/0/3` → `et-0/0/3:1`). A channel
whose name shares no prefix with its parent is left alone and logged — the engine does not guess
at free-form names.

### Channelized Breakout

`breakout_mode` selects the topology a breakout rule produces: `flat` (the default, and what every
rule did before the field existed) creates sibling interfaces, while `channelized` turns the base
into a parent with `channels` set and creates one channel subinterface per channel.

```yaml
name_template: "xe-0/0/{bay_position}:{channel}"
parent_name_template: "et-0/0/{bay_position}"
breakout_mode: channelized
channel_count: 4
channel_start: 0
# Bay position 5 → et-0/0/5 (parent, 4 channels) + xe-0/0/5:0 … xe-0/0/5:3
```

`parent_name_template` names the parent interface. It takes the same variables as `name_template`
minus `{channel}` — the parent is the one interface in the family without a channel number, and a
`{channel}` in it is rejected in every spelling, including inside an expression (`{channel + 1}`).
Braces must balance, so a stray `{` is refused on save instead of ending up in an interface name.
Blank leaves the parent the name NetBox gave it.

The complete family is checked before anything is written: one occupied name — the parent's or any
channel's — skips the whole family with a warning. On NetBox releases that cannot model channels
(4.6 and older) a `channelized` rule is skipped and logged; it is never applied as a flat breakout
instead.

### Converting an installed flat family

Applying a rule never converts the flat family an earlier apply installed. A `channelized` rule with
a `parent_name_template` set instead offers each such family for conversion on **Apply Rules →
Preview & Apply**, where the operator confirms it per family; a blank parent template offers
nothing, because a flat family's ch-0 interface is the base and has nowhere else to go.

The plugin rejects missing members locally before a conversion transaction starts. Its local
preflight also refuses a stale family, an occupied parent name, a sibling that already belongs to
another channel family, and a cabled sibling. Each of those verdicts is the plugin's own, and no
row is written. Only a family that passes preflight reaches NetBox's own rules. The plugin runs
that rewrite inside a transaction and rolls it back when NetBox refuses a row or a name collides,
so the verdict then carries NetBox's own reason. A preview rolls back a successful rewrite the same
way. The plugin never half converts a refused family.

Converting keeps the physical row: the ch-0 interface keeps its interface ID, cable, type, module
link and `mark_connected`, and becomes the parent. Its interface VRF, IP addresses, FHRP group assignments,
untagged/tagged VLANs, 802.1Q mode, MTU, description and tags move to a newly created channel 1
interface that takes over its name; custom field values are copied to it. The remaining siblings are
retyped in place, keeping their own interface IDs. Automation keyed on the ch-0 interface ID
addresses the parent afterward, not the channel that carries its name.

On a NetBox release that cannot model channels, each flat family is shown as unsupported without a
conversion action, and direct conversion reports the same explicit family outcome.

A family installed by a rule that uses `{base}` carries the raw name it was named with, which on a
module type using the `{vc_position}` template token may predate a chassis position change (see
above). That base is recovered from the family's own names, so the family is still offered. Two
cases deliberately are not: a `{base}` used inside an arithmetic expression (`p{1 + {base}}`), which
cannot be evaluated symbolically, and an ambiguous recovery — one template matching two bases on the
module, or two templates recovering the same one. Conversion rewrites rows an operator owns, so
those families are left unoffered rather than converted on a guess.

### Converter Offset

```yaml
name_template: "GigabitEthernet{slot_num}/{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}"
# Slot 3, parent bay 2, SFP slot 1 → GigabitEthernet3/11
```

### UfiSpace SONiC

```yaml
name_template: "swp{bay_position_num}"
# Bay position swp5 → swp5
```
