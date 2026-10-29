#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Load the demo devices and the Interface Name Rules of contrib/ into NetBox.

The devcontainer and the documentation build use this script. Put the NetBox source on the path:

    PYTHONPATH=/opt/netbox/netbox python scripts/load_demo_data.py

Each object is created once, so a second run changes nothing. The script loads each rule that it can,
and exits with status 1 when it skipped a rule.
"""

import os
import sys
from pathlib import Path

import django
import yaml

CONTRIB_DIR = Path(__file__).resolve().parents[1] / "contrib"

#: The device that Apply Rules renames: its modules are installed before the rules exist.
APPLY_DEMO_DEVICE = "acx7024-1"
APPLY_DEMO_BAYS = range(4)
APPLY_DEMO_MODULE_TYPE = "QSFP-100G-LR4"

#: Vendor virtual chassis: (device type, its interface templates, [(member, VC position)], chassis name).
VENDOR_STACKS = (
    (
        "JNP-EX-VC",
        [(f"ge-0/0/{i}", "1000base-t") for i in range(4)] + [(f"xe-0/1/{i}", "10gbase-x-xfp") for i in range(2)],
        [("jnp-vc-1", 0), ("jnp-vc-2", 1)],
        "juniper-vc-stack",
    ),
    (
        "CISCO-C9K",
        [(f"GigabitEthernet1/0/{i}", "1000base-t") for i in range(1, 5)],
        [("cisco-sw-1", 1), ("cisco-sw-2", 2)],
        "cisco-stack",
    ),
    (
        "ARISTA-EOS",
        [(f"Ethernet1/{i}", "10gbase-x-sfpp") for i in range(1, 5)],
        [("arista-sw-1", 1), ("arista-sw-2", 2)],
        "arista-stack",
    ),
)


def ok(label):
    """Report a created or changed object."""
    print(f"  ✓ {label}")


def skip(label, reason):
    """Report a skipped rule."""
    print(f"  · {label}: {reason}")


def contrib_rules():
    """Return (file name, rule rows) for each rule file of contrib/."""
    files = []
    for path in sorted(CONTRIB_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        files.append((path.name, [row for row in data if isinstance(row, dict)]))
    return files


def shared_objects():
    """Return the site, the manufacturer and the role of the demo devices, and create the SONiC platform."""
    from dcim.models import DeviceRole, Manufacturer, Platform, Site

    site, _ = Site.objects.get_or_create(name="Test Site", defaults={"slug": "test-site"})
    manufacturer, _ = Manufacturer.objects.get_or_create(
        name="Test Manufacturer", defaults={"slug": "test-manufacturer"}
    )
    role, _ = DeviceRole.objects.get_or_create(name="Test Role", defaults={"slug": "test-role", "color": "9e9e9e"})
    # The platform-scoped rules of contrib/ufispace.yaml need it.
    Platform.objects.get_or_create(
        slug="sonic", defaults={"name": "SONiC", "description": "Software for Open Networking in the Cloud"}
    )
    return site, manufacturer, role


def device(name, site, device_type, role):
    """Return the device *name* and whether this call created it."""
    from dcim.models import Device

    obj, created = Device.objects.get_or_create(
        name=name, defaults={"site": site, "device_type": device_type, "role": role, "status": "active"}
    )
    if created:
        ok(f"Created device {name}")
    return obj, created


def join_chassis(chassis_name, members):
    """Put each (device, position) into the chassis; the first member is the master."""
    from dcim.models import VirtualChassis

    master = members[0][0]
    chassis, created = VirtualChassis.objects.get_or_create(name=chassis_name, defaults={"master": master})
    if created:
        ok(f"Created VirtualChassis {chassis_name}")
    for member, position in members:
        if member.virtual_chassis != chassis or member.vc_position != position:
            member.virtual_chassis = chassis
            member.vc_position = position
            member.save()
            ok(f"Assigned {member.name} to {chassis_name} at position {position}")
    if chassis.master != master:
        chassis.master = master
        chassis.save()


def load_100_bay_device(site, manufacturer, role):
    """Load a device with 100 installed modules, for the batch limit of Apply Rules."""
    from dcim.models import DeviceType, Module, ModuleBay, ModuleBayTemplate, ModuleType

    device_type, created = DeviceType.objects.get_or_create(
        manufacturer=manufacturer, model="TEST-100BAY", defaults={"slug": "test-100bay", "u_height": 2}
    )
    if created:
        for i in range(1, 101):
            ModuleBayTemplate.objects.create(device_type=device_type, name=f"bay{i}", position=str(i))
    module_type, _ = ModuleType.objects.get_or_create(manufacturer=manufacturer, model="TEST-1PORT")
    obj, created = device("test-100ports", site, device_type, role)
    if created:
        for bay in ModuleBay.objects.filter(device=obj).order_by("name"):
            Module.objects.create(device=obj, module_bay=bay, module_type=module_type, status="active")
        ok("Installed TEST-1PORT modules into all 100 bays")


def load_demo_vc_stacks(site, manufacturer, role):
    """Load the VC-SWITCH type, its line card and SFP module types, and two VC-SWITCH stacks."""
    from dcim.models import DeviceType, InterfaceTemplate, Module, ModuleBay, ModuleBayTemplate, ModuleType

    switch, created = DeviceType.objects.get_or_create(
        manufacturer=manufacturer, model="VC-SWITCH", defaults={"slug": "vc-switch", "u_height": 1}
    )
    if created:
        for i in range(1, 5):
            InterfaceTemplate.objects.create(device_type=switch, name=f"Gi0/{i}", type="1000base-t")
        ModuleBayTemplate.objects.create(device_type=switch, name="linecard0", position="0")
    linecard, created = ModuleType.objects.get_or_create(manufacturer=manufacturer, model="VC-LINECARD")
    if created:
        # The port takes the bay position as its name; the rules rename it to Gi{vc_position}/0.
        InterfaceTemplate.objects.create(module_type=linecard, name="0", type="1000base-t")
    ModuleBayTemplate.objects.get_or_create(module_type=linecard, name="sfp0", defaults={"position": "0"})
    sfp, created = ModuleType.objects.get_or_create(manufacturer=manufacturer, model="VC-SFP")
    if created:
        InterfaceTemplate.objects.create(module_type=sfp, name="0", type="1000base-x-sfp")

    # The e2e tests change test-vc-stack; demo-vc-stack stays for manual checks.
    for chassis_name, prefix in (("test-vc-stack", "vc-stack"), ("demo-vc-stack", "demo-vc")):
        join_chassis(chassis_name, [(device(f"{prefix}-{n}", site, switch, role)[0], n) for n in (1, 2)])

    # Remove the modules other than VC-SFP that manual tests left in the sfp0 bays of the demo stack.
    for name in ("demo-vc-1", "demo-vc-2"):
        for bay in ModuleBay.objects.filter(device__name=name, name="sfp0"):
            for module in Module.objects.filter(module_bay=bay).exclude(module_type=sfp):
                module.delete()
                ok(f"Removed stale {module.module_type.model} from {name} sfp0")


def load_vendor_stacks(site, manufacturer, role):
    """Load a Juniper, a Cisco and an Arista style stack for the device-interface rules."""
    from dcim.models import DeviceType, InterfaceTemplate

    for model, interfaces, members, chassis_name in VENDOR_STACKS:
        device_type, created = DeviceType.objects.get_or_create(
            manufacturer=manufacturer, model=model, defaults={"slug": model.lower(), "u_height": 1}
        )
        if created:
            for name, kind in interfaces:
                InterfaceTemplate.objects.create(device_type=device_type, name=name, type=kind)
            ok(f"Created DeviceType {model}")
        join_chassis(chassis_name, [(device(name, site, device_type, role)[0], n) for name, n in members])


def load_apply_demo_device(site, manufacturer, role):
    """Load an ACX7024 whose QSFP28 modules keep the names that NetBox gave them, for Apply Rules."""
    from dcim.models import (
        DeviceType,
        InterfaceTemplate,
        Manufacturer,
        Module,
        ModuleBay,
        ModuleBayTemplate,
        ModuleType,
    )

    juniper, _ = Manufacturer.objects.get_or_create(name="Juniper", defaults={"slug": "juniper"})
    acx, created = DeviceType.objects.get_or_create(
        manufacturer=juniper, model="ACX7024", defaults={"slug": "acx7024", "u_height": 1}
    )
    if created:
        for i in range(24):
            ModuleBayTemplate.objects.create(device_type=acx, name=f"Transceiver 0/0/{i}", position=str(i))
        ok("Created DeviceType ACX7024 with 24 transceiver bays")
    module_type = ModuleType.objects.filter(model=APPLY_DEMO_MODULE_TYPE).first()
    if module_type is None:
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model=APPLY_DEMO_MODULE_TYPE)
        # NetBox replaces {module} with the bay position.
        InterfaceTemplate.objects.create(module_type=module_type, name="{module}", type="100gbase-x-qsfp28")
    obj, created = device(APPLY_DEMO_DEVICE, site, acx, role)
    if created:
        for position in APPLY_DEMO_BAYS:
            bay = ModuleBay.objects.get(device=obj, position=str(position))
            Module.objects.create(device=obj, module_bay=bay, module_type=module_type, status="active")
        ok(f"Installed {len(APPLY_DEMO_BAYS)} {APPLY_DEMO_MODULE_TYPE} modules into {APPLY_DEMO_DEVICE}")


def load_rule_scopes(manufacturer):
    """Create each device type and exact module type that a contrib rule names, when no object has that model."""
    from dcim.models import DeviceType, ModuleType

    for _filename, rows in contrib_rules():
        for row in rows:
            model = row.get("device_type")
            if model and not DeviceType.objects.filter(model=model).exists():
                DeviceType.objects.create(manufacturer=manufacturer, model=model, slug=model.lower())
                ok(f"Created DeviceType {model}")
            for model in (row.get("module_type"), row.get("parent_module_type")):
                if model and not ModuleType.objects.filter(model=model).exists():
                    ModuleType.objects.create(manufacturer=manufacturer, model=model)
                    ok(f"Created ModuleType {model}")


def resolve(cls, label, **lookup):
    """Return the one object of the model *cls* that *lookup* names, or raise LookupError that names *label*."""
    try:
        return cls.objects.get(**lookup)
    except (cls.DoesNotExist, cls.MultipleObjectsReturned) as exc:
        raise LookupError(f"{label}: {exc}") from exc


def rule_lookup(row):
    """Return the unique lookup and the extra defaults of the rule in *row*."""
    from dcim.models import DeviceType, ModuleType, Platform

    device_type = platform = None
    if row.get("device_type"):
        device_type = resolve(DeviceType, "device type", model=row["device_type"])
    if row.get("platform"):
        platforms = Platform.objects.filter(name=row["platform"]) or Platform.objects.filter(slug=row["platform"])
        platform = resolve(Platform, "platform", pk__in=platforms.values("pk"))
    if row.get("applies_to_device_interfaces"):
        lookup = {
            "applies_to_device_interfaces": True,
            "module_type_pattern": row.get("module_type_pattern", ""),
            "device_type": device_type,
            "platform": platform,
        }
        return lookup, {"module_type": None, "module_type_is_regex": False, "parent_module_type": None}
    is_regex = bool(row.get("module_type_is_regex"))
    module_type = parent_module_type = None
    if not is_regex:
        types = ModuleType.objects.filter(model=row.get("module_type", ""))
        if row.get("manufacturer"):
            types = types.filter(manufacturer__name=row["manufacturer"])
        module_type = resolve(ModuleType, "module type", pk__in=types.values("pk"))
    if row.get("parent_module_type"):
        parent_module_type = resolve(ModuleType, "parent module type", model=row["parent_module_type"])
    lookup = {
        "module_type": module_type,
        "module_type_pattern": row.get("module_type_pattern", "") if is_regex else "",
        "module_type_is_regex": is_regex,
        "parent_module_type": parent_module_type,
        "device_type": device_type,
        "platform": platform,
        "applies_to_device_interfaces": False,
    }
    return lookup, {}


def load_rules():
    """Load each rule of contrib/ and return the number of rules that were skipped."""
    from netbox_interface_name_rules.models import InterfaceNameRule

    skipped = 0
    for filename, rows in contrib_rules():
        print(f"📋 Loading {filename}…")
        for row in rows:
            label = row.get("module_type") or row.get("module_type_pattern") or row.get("device_type") or "(device)"
            if not row.get("name_template"):
                skip(label, "missing name_template")
                skipped += 1
                continue
            try:
                lookup, extra = rule_lookup(row)
            except LookupError as exc:
                skip(label, str(exc))
                skipped += 1
                continue
            defaults = {
                "name_template": row["name_template"],
                "channel_count": int(row.get("channel_count", 0)),
                "channel_start": int(row.get("channel_start", 0)),
                "description": row.get("description", ""),
                **extra,
            }
            _rule, created = InterfaceNameRule.objects.update_or_create(**lookup, defaults=defaults)
            if created:
                ok(f"{label} → {row['name_template']!r}")
    return skipped


def reapply_device_rules():
    """Rename the VC members again: they joined their chassis before the device-interface rules existed."""
    from dcim.models import Device

    from netbox_interface_name_rules.engine import apply_device_interface_rules

    members = Device.objects.filter(virtual_chassis__isnull=False).select_related(
        "virtual_chassis", "device_type", "platform"
    )
    for member in members:
        if renamed := apply_device_interface_rules(member):
            ok(f"Renamed {renamed} interface(s) on {member.name} (position {member.vc_position})")


def main():
    """Load the demo data and return the exit status."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "netbox.settings")
    django.setup()
    from django.core.management import call_command

    print("🖥  Creating demo devices…")
    site, manufacturer, role = shared_objects()
    load_100_bay_device(site, manufacturer, role)
    load_demo_vc_stacks(site, manufacturer, role)
    load_vendor_stacks(site, manufacturer, role)
    load_apply_demo_device(site, manufacturer, role)
    load_rule_scopes(manufacturer)
    # Fix the member_count counter of each VirtualChassis.
    call_command("calculate_cached_counts", verbosity=0)
    skipped = load_rules()
    print("🔄 Re-applying the device-interface rules to the VC members…")
    reapply_device_rules()
    if skipped:
        print(f"⚠️  {skipped} rule(s) skipped.")
        return 1
    print("✅ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
