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
and the parent keeps its own name. A breakout rule (non-zero `channel_count`) whose count
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
Blank leaves the parent the name NetBox gave it. `{base}` is the base interface's current name, for
the parent and for every channel alike.

The complete family is checked before anything is written: one occupied name — the parent's or any
channel's — skips the whole family with a warning. On NetBox releases that cannot model channels
(4.6 and older) a `channelized` rule is skipped and logged; it is never applied as a flat breakout
instead.

### Converting an installed flat family

Applying a rule never converts the flat family an earlier apply installed. A `channelized` rule with
a `parent_name_template` set instead offers each such family for conversion on **Apply Rules →
Preview & Apply**, where the operator confirms it per family; a blank parent template offers
nothing, because a flat family's ch-0 interface is the base and has nowhere else to go.

Every family is preflighted by performing the conversion inside a transaction that is rolled back,
so the verdict carries NetBox's own reason for refusing it — a cabled sibling, an occupied parent
name, a missing sibling — and a refused family is never half converted.

Converting keeps the physical row: the ch-0 interface keeps its interface ID, cable, type, module
link and `mark_connected`, and becomes the parent. Its IP addresses, FHRP group assignments,
untagged/tagged VLANs, 802.1Q mode, MTU, description and tags move to a newly created channel 1
interface that takes over its name; custom field values are copied to it. The remaining siblings are
retyped in place, keeping their own interface IDs. Automation keyed on the ch-0 interface ID
addresses the parent afterwards, not the channel that carries its name.

On NetBox 4.6 and older no family is offered and conversion reports that this release cannot model
channels.

A family installed by a rule that uses `{base}` carries the raw name it was named with, which on a
module type using the `{vc_position}` template token may predate a chassis position change (see
above). That base is recovered from the family's own names, so the family is still offered. Two
cases deliberately are not: a `{base}` used inside an arithmetic expression (`p{1 + {base}}`), which
cannot be evaluated symbolically, and an ambiguous recovery — one template matching two bases on the
module, or two templates recovering the same one. Conversion rewrites rows an operator owns, so
those families are left unoffered rather than converted on a guess.

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
