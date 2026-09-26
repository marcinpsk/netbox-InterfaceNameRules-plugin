# Installation

## Requirements

- NetBox ≥ 4.3.0
- Python ≥ 3.12

## Install from PyPI

```bash
pip install netbox-interface-name-rules
```

## Enable the Plugin

Add to your NetBox `configuration.py`:

```python
PLUGINS = ["netbox_interface_name_rules"]
```

## Run Database Migrations

The migration audits every existing nonempty **Module Type Pattern** used by a
regex module-type rule or a device-interface rule before the plugin starts
executing stored patterns with RE2. The migration stops when RE2 cannot compile
a pattern or could match text that Python did not match. Blocking differences
include `\D`, `\S`, and `\W` outside negated character classes;
`\d`, `\s`, and `\w` inside negated character classes; word boundaries; POSIX
character classes; and Python counted repeats that RE2 would treat as literal
text, such as `{,3}` or `{01}`. Case-insensitive matching with a negated
character class also stops the migration. If the migration stops, rewrite the
listed patterns, then run the migration again.

The migration warns and continues when RE2 can only remove matches. This warning
includes `\d`, `\s`, and `\w` outside negated character classes; `\D`, `\S`, and
`\W` inside negated character classes; and case-insensitive matching outside a
negated character class. The migration lists the affected Interface Name Rule
IDs and does not modify any rule. Django records the warning-only migration as
applied. Rewrite and save those patterns with explicit
[RE2 syntax](https://github.com/google/re2/wiki/syntax). Do not rerun the
completed migration. For example, use `[0-9]` for ASCII digits, `\p{L}` for
Unicode letters, and `{0,3}` for a repeat with an omitted Python lower bound.

```bash
cd /opt/netbox/netbox
python manage.py migrate
```

The name-template audit migration reports stored rules whose templates use variables
outside their naming context, have unbalanced braces, or have a brace group that cannot
evaluate. Each warning names the rule ID, field, and reason. The migration leaves all
values unchanged and completes even
when it reports a refused rule. Correct the reported templates before saving those
rules again. See [Template Variables](template-variables.md) for each context's variables.

Three migrations change stored rules to the shape that rule validation accepts.
A rollback of these migrations does not restore the old values. If you need
them, record them before the upgrade.

- Migration `0015` turns off regex mode and sets the breakout mode to flat on
  every device-interface rule. It also sets a channelized rule with a channel
  count of 0 to flat, and clears the **Parent Name Template** of every rule that
  is not channelized. A device-interface rule never matched on regex mode, but
  the priority score of a device-interface rule reads its regex mode, so the
  migration can change the rank of a device-interface rule. When two
  device-interface rules match the same interface, a different rule can rename
  it, and the interface can get a different name. The breakout mode and
  **Parent Name Template** changes do not change the rank or the name of any
  interface. The migration does not log the rules it changes.
- Migration `0016` sets every breakout mode other than flat or channelized to
  flat. It does not log the rules it changes.
- Migration `0018` clears the **Parent Module Type** of every device-interface
  rule. A device-interface rule never matched on that field, so only the rule
  ranking changes. For each cleared rule, the migration logs the rule ID and the
  cleared module type. A rollback of migration `0018` does not restore the
  cleared values.

## Restart NetBox

```bash
systemctl restart netbox netbox-rq
```

## Verify

Navigate to **Plugins → Interface Name Rules** in the NetBox UI.
