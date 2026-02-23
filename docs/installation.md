# Installation

## Requirements

- NetBox ≥ 4.4.0
- Python ≥ 3.12

## Install from PyPI

```bash
pip install netbox-interface-name-rules
```

## Enable the Plugin

Add to your NetBox `configuration.py`:

```python
PLUGINS = ['netbox_interface_name_rules']
```

## Run Database Migrations

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
