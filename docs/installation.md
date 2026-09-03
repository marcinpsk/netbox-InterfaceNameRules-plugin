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

The migration audits every existing nonempty **Module Type Pattern** before the
plugin starts executing stored patterns with RE2. The migration stops when RE2
cannot compile a pattern or could match text that Python did not match. Blocking
differences include `\D`, `\S`, and `\W` outside negated character classes;
`\d`, `\s`, and `\w` inside negated character classes; word boundaries; POSIX
character classes; and Python counted repeats that RE2 would treat as literal
text, such as `{,3}` or `{01}`. Case-insensitive matching with a negated
character class also stops the migration.

The migration warns and continues when RE2 can only remove matches. This warning
includes `\d`, `\s`, and `\w` outside negated character classes; `\D`, `\S`, and
`\W` inside negated character classes; and case-insensitive matching outside a
negated character class. The migration lists the affected Interface Name Rule
IDs and does not modify any rule. Rewrite those patterns with explicit
[RE2 syntax](https://github.com/google/re2/wiki/syntax), then run the migration
again. For example, use `[0-9]` for ASCII digits, `\p{L}` for Unicode letters,
and `{0,3}` for a repeat with an omitted Python lower bound.

```bash
cd /opt/netbox/netbox
python manage.py migrate
```

## Restart NetBox

```bash
systemctl restart netbox netbox-rq
```

## Verify

Navigate to **Plugins → Interface Name Rules** in the NetBox UI.
