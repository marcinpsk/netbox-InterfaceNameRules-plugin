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
plugin starts executing stored patterns with RE2. It stops when RE2 cannot
compile a pattern or when a Python shorthand can change its Unicode behavior.
The latter check includes `\d`, `\s`, `\w`, word boundaries, case-insensitive
matching, and POSIX character classes. The migration lists the affected
Interface Name Rule IDs and does not modify any rule. Rewrite those patterns
with explicit [RE2 syntax](https://github.com/google/re2/wiki/syntax), then run
the migration again. For example, use `[0-9]` for ASCII digits or `\p{L}` for
Unicode letters.

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
