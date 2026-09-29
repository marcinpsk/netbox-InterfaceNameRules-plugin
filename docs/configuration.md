# Configuration

## Creating Rules

Navigate to **Plugins → Interface Name Rules → Add** or use the REST API.

### Rule Fields

| Field | Required | Description |
|-------|----------|-------------|
| Module Type | Conditional | The module type that triggers this rule (required when Regex Mode is off and Applies to Device Interfaces is disabled) |
| Applies to Device Interfaces | No | Rename device-level interfaces when the device joins or changes position in a Virtual Chassis. Module Type and Parent Module Type must be empty; Module Type Pattern can filter interface names |
| Module Type Pattern | Conditional | RE2 pattern matched against the complete module type model name, or a device interface's current name when Applies to Device Interfaces is enabled |
| Regex Mode | No | When enabled, match by pattern instead of exact module type FK |
| Parent Module Type | No | Restrict to modules inside this parent (e.g., converter). Must be empty on a device-interface rule |
| Device Type | No | Restrict to devices of this type |
| Platform | No | Restrict to devices running this software platform/OS |
| Name Template | Yes | Interface name pattern with template variables |
| Parent Name Template | No | Name template for the channelized parent, with the same variables as Name Template except `{channel}`. Blank keeps the parent's current name |
| Breakout Mode | No | Flat renames the base to the first channel and creates sibling interfaces. Channelized turns the base into a parent with one subinterface per channel (requires NetBox channel support) |
| Channel Count | No | Number of breakout channels (0 = no breakout) |
| Channel Start | No | Starting channel number (0 for most platforms) |

### Rule Priority

When multiple rules could match, exact module type rules form the first tier.
Regex rules form the fallback tier. Within each tier, the rule with the highest
scope score wins. Parent module type has weight 4, device type has weight 2, and
platform has weight 1.

| Score | Exact tier | Regex tier |
|------:|------------|------------|
| 7 | Exact module type + parent module type + device type + platform | Regex pattern + parent module type + device type + platform |
| 6 | Exact module type + parent module type + device type | Regex pattern + parent module type + device type |
| 5 | Exact module type + parent module type + platform | Regex pattern + parent module type + platform |
| 4 | Exact module type + parent module type | Regex pattern + parent module type |
| 3 | Exact module type + device type + platform | Regex pattern + device type + platform |
| 2 | Exact module type + device type | Regex pattern + device type |
| 1 | Exact module type + platform | Regex pattern + platform |
| 0 | Exact module type only | Regex pattern only |

For module rules, the regex pattern matches the complete installed module type
model name. For device-interface rules, it matches each device
interface's current name, including a standalone interface such as `mgmt0`. A channel
subinterface is never matched on its own; it follows the parent whose family a rule wins.
When multiple regex patterns match at the same score, the longest pattern wins.

### RE2 Pattern Syntax

The plugin compiles and executes every stored rule pattern with
[RE2](https://github.com/google/re2/wiki/syntax). RE2 guarantees bounded memory
use and linear matching time. It does not support Python-only features that
require backtracking, including lookaround, backreferences, and atomic groups.
Use `\z` instead of Python's `\Z` end-of-text escape. The `\d`, `\s`, and `\w`
classes match ASCII characters. Use an RE2 Unicode property such as `\p{L}` when
the rule must match Unicode letters.

## NetBox Module Interface Templates

The plugin works alongside NetBox's own module interface template naming.
Two NetBox token styles affect how and when the plugin renames interfaces:

### `{module}` (legacy — all NetBox versions)

When a module type's interface template uses `{module}`, NetBox substitutes the
raw bay position string at install time — for example, `{module}` in bay `5`
creates an interface named `5`.

The plugin then **renames** this interface via the `post_save` signal on `Module`.
This is the primary workflow the plugin was designed for.

```
NetBox installs:  interface name = "5"   (raw bay position)
Plugin renames:   interface name = "et-0/0/5"
```

### The `potentially-deprecated` Tag

After installing a module, if the plugin's signal fires but finds the interface
is already correctly named, it automatically tags the rule `potentially-deprecated`.
This means:

- For **new installs**: the rule may no longer be needed (NetBox generates the name)
- For **retroactive applies**: the rule is still useful for modules installed before the rule existed

The tag is informational only — the rule remains active.

### Moving a module

NetBox 4.7 can move an installed module to another module bay or to another
device. After the move, the plugin renames the interfaces of the moved module
and of every module nested in it. The new names come from the rule that matches
each module at its new position. That rule can be a different rule, because the
device type, the platform and the parent module type select the rule.

NetBox renames a moved module's interfaces only while they carry their raw
template name. To find the interfaces that the plugin renamed earlier, the plugin
rebuilds their earlier names from the state before the move: the old module bay,
the old device and its virtual-chassis position, and the rule that matched there.
It reads that state before the save, because in the same save NetBox can change
the position and the name of the module bays that the moved module holds.
When the same transaction first changes the virtual-chassis position of the old
device, the plugin rebuilds the earlier names from the position before that
change, also after a move to another device. NetBox 4.7 renames the raw
interface names of a moved module for the position that the new device has at
the move. When the same transaction then changes the position of that device,
the plugin recognises those names at the position of the move. NetBox can also
keep those names when the module moves back to its bay after a position change
or a bay edit, and the plugin recognises them there too.

The plugin renames an interface only when exactly one interface template claims
it. A template claims an interface through its raw template name before or after
the move, or through the name that the old rule or the new rule gives it. When one
template claims two interfaces, or two templates claim one interface, the plugin
renames none of them. When any interface of the module is left unclaimed, the
plugin renames nothing on that module, because the unclaimed interface can belong
to a breakout family: every interface keeps its name, and the journal entry lists
them all. A channel or a subinterface of another interface does not count. A
module type without interface templates claims nothing, so after a move its
interfaces keep their names and are listed.

When no rule matches the module at its new position, the interfaces keep their
names. The journal entry lists each interface that the old rule named. When no
rule matched the module at its old position, the plugin renames the raw template
names as an install does.

Limits:

- A move does not rename a flat breakout family. When the rule that matched a
  module before the move uses the flat breakout mode, the plugin renames nothing
  on that module, and the journal entry lists each of its interfaces. NetBox keeps
  no link from an interface to its template or its family, so the plugin cannot
  tell with certainty which interfaces form a flat family. On NetBox 4.7, use the
  channelized breakout mode to have a move rename a breakout family, or rename the
  interfaces by hand. A move into the scope of a flat breakout rule builds the
  family as an install does.
- The plugin does not repair names that module moves left wrong before this
  version. Apply Rules cannot repair them either, because it has no state from
  before the move. Rename these interfaces by hand.
- NetBox before 4.7 saves a move as a change of the module row only. After a
  move to another bay of the same device, the plugin renames the moved module's
  interfaces, and recognises the raw template names from the old bay. The
  module bays of the moved module keep the old parent bay, which no longer
  holds the moved module. The plugin renames no interface of a module installed
  in such a bay, after the move and after a later virtual-chassis change, and
  the journal entry lists each of them. After a move to another device, the
  interfaces stay on the old device. The plugin then renames no interface of
  that module, and the journal entry lists each of them.

### Editing a module bay

NetBox renames no interface when the position or the name of a module bay
changes. When you change the position of a bay that holds a module, the plugin
renames the interfaces of that module and of every module nested in it for the
new position. When you change the name of such a bay, the plugin renames them
only when a template variable reads the name: a bay whose position is a
template token, such as `{module}`, takes its position from the number at the
end of its name. Any other name change renames nothing. An edit of an empty bay
renames nothing.

The plugin finds the interfaces that it renamed earlier as it does after a
move. It reads the bay's position and name before the save, and rebuilds the
earlier names from them. The claim rules of a move apply, and a bay edit does
not rename a flat breakout family either. The journal entry goes on the module
in the bay. The plugin does not repair names that bay edits left wrong before
this version, for the same reason as after a move.

The plugin renames each module at most once per transaction, from the state
before the first change in that transaction. This includes a virtual-chassis
position change of the module's device in the same transaction: the plugin
rebuilds the earlier names from the position before that change, and the
device does not rename the module again. A module installed in the same
transaction, before or after that change, is recognised from the position at
its install. Several edits of one bay rename once, and an edit that the same transaction undoes renames nothing. When one
transaction edits a bay and a bay nested below it, or edits a bay and moves,
installs or changes the type of a module in it, each module is renamed once. A
module installed in the same transaction is named as an install names it, also
under a flat breakout rule.

### Changing a module's type

When you change the type of an installed module, the plugin renames the
interfaces of that module with the rule that matches its new type. A rule can
be scoped to a parent module type, so a module nested in the changed module can
get a different rule too. The plugin also renames the interfaces of each nested
module whose rule changes. A nested module whose rule does not change keeps its
names, and the journal entry does not list it.

The plugin finds the interfaces of a nested module that it renamed earlier as it
does after a move. It reads what named them before the save, and rebuilds the
names that the old rule gave. It reads this state only when an enabled rule has
the old type or the new type as its parent module type, because only then can a
nested module get a different rule. The claim rules of a move apply, and a type
change does not rename the flat breakout family of a nested module either. When
no rule matches a nested module after the change, its interfaces keep their
names, and the journal entry lists each interface that the old rule named. The
journal entry goes on the module whose type changed.

When one transaction changes the type of a module and then moves the module or
edits the bay that holds it, the plugin recognises the names of the nested
modules from the state before the type change.

### Journal entries after an automatic rename

The plugin renames interfaces again after a save that can make a name wrong: a
module install, a module type change, a module move to another bay or device,
an edit of the position or the name of an occupied module bay, and a device
that joins or changes position in a virtual chassis. When that rename leaves an
interface unrenamed although a rule matched it, or fails, the plugin writes one
journal entry. The entry goes on the module, or on the device for a
virtual-chassis change. The entry for a move goes on the moved module, the
entry for a bay edit goes on the module in the bay, and the entry for a type
change goes on the module whose type changed. Each also lists the interfaces of
the modules nested in that module, also of a nested module that was moved,
edited or installed in the same transaction. When the same transaction also
changes the virtual-chassis position of the device, the entry of the device
does not list the modules that these entries list. An entry lists each
interface and the reason:

- the name the rule gives is already in use on the device,
- a template variable is not available, such as `{vc_position}` on a device
  outside a virtual chassis,
- no single interface template claims the interface: two templates claim it,
  its template also claims another interface or flat family, or no template
  claims it. This applies to a rule that uses `{base}`, to a breakout rule, and
  after a move, a bay edit or a type change of the parent module. An install
  reports only an interface that still carries a raw name. A breakout rule does
  not report a subinterface that no template claims,
- a flat breakout family that the rule named at another virtual-chassis
  position lost one of its interfaces, so the plugin keeps the names of the
  rest,
- no rule matches a moved module, the module in an edited bay, or a module
  nested in a module whose type changed, after the change,
- the interface is not on the device of its module, which NetBox before 4.7
  leaves after a move to another device,
- the module bay still has the parent bay it had before its module moved,
  which NetBox before 4.7 leaves for the modules nested in a moved module,
- the rule of a moved module, of the module in an edited bay, or of a module
  nested in a module whose type changed, before the change is a flat breakout
  rule,
- another interface of the module is unclaimed after a move, a bay edit or a
  type change of the parent module, so nothing on the module is renamed,
- the rule failed, for example on a division by zero in its template.

When an error stops the rename, the entry also has one line for that error,
which names no interface. The interfaces listed before the error stay in the
entry. When the plugin cannot read the saved module or device, it writes no
entry and only the server log records the error.

A device that leaves its virtual chassis, or stays in one without a position,
renames nothing. Its journal entry lists the interfaces whose rules use
`{vc_position}`, so you can decide what to call them.

The kind is **Danger** when the rule failed and **Warning** otherwise. The author
is the user of the request that saved the change. A save outside a request, for
example from a script or the shell, writes an entry with no author. An interface
that already has its correct name, or that the rule does not match, is not listed.
The server log records the same events.

### Apply Rules and the Applicable Column

**Apply Rules** is designed for **retroactive renames**.  Interfaces installed
after a matching rule is active are renamed automatically at install time.
The web UI, the REST API and bulk import install modules inside a transaction.
A script or shell that creates a module outside a transaction gets no rename:
run Apply Rules after it, or wrap the install in `transaction.atomic()`.

The **Applicable** column shows ✓ only when at least one currently-installed
interface **would actually change name** if the rule were applied.  Rules where
all matching interfaces are already correctly named show `—`.

## Bulk Import

Export existing rules or import new ones via **Interface Name Rules → Import**.
The YAML format matches the files in the `contrib/` directory.

## REST API

Full CRUD is available at `/api/plugins/interface-name-rules/rules/`.

```bash
# List rules
curl -H "Authorization: Token $TOKEN" http://netbox/api/plugins/interface-name-rules/rules/

# Create a rule
curl -X POST -H "Authorization: Token $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"module_type": 1, "name_template": "et-0/0/{bay_position}"}' \
  http://netbox/api/plugins/interface-name-rules/rules/
```
