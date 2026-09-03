# Configuration

## Creating Rules

Navigate to **Plugins → Interface Name Rules → Add** or use the REST API.

### Rule Fields

| Field | Required | Description |
|-------|----------|-------------|
| Module Type | Conditional | The module type that triggers this rule (required when Regex Mode is off) |
| Module Type Pattern | Conditional | RE2 pattern matched against the complete module type model name (required when Regex Mode is on) |
| Regex Mode | No | When enabled, match by pattern instead of exact module type FK |
| Parent Module Type | No | Restrict to modules inside this parent (e.g., converter) |
| Device Type | No | Restrict to devices of this type |
| Name Template | Yes | Interface name pattern with template variables |
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

The regex pattern matches the complete installed module type model name. When
multiple regex patterns match at the same score, the longest pattern wins.

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

### Apply Rules and the Applicable Column

**Apply Rules** is designed for **retroactive renames**.  Interfaces installed
after a matching rule is active are renamed automatically at install time.

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
