#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
#
# Load Interface Name Rules from contrib/ YAML files into the devcontainer NetBox.
# Run via: python manage.py shell < /path/to/load-sample-data.py

import os

import yaml

CONTRIB_DIR = os.path.join(
    os.path.dirname(
        os.path.abspath(
            globals().get(
                "__file__", "/workspaces/netbox-InterfaceNameRules-plugin/.devcontainer/scripts/load-sample-data.py"
            )
        )
    ),
    "..",
    "..",
    "contrib",
)
# Fallback for when piped via `manage.py shell` (where __file__ resolves incorrectly)
if not os.path.isdir(CONTRIB_DIR):
    CONTRIB_DIR = "/workspaces/netbox-InterfaceNameRules-plugin/contrib"


def load_yaml(filename):
    path = os.path.join(CONTRIB_DIR, filename)
    if not os.path.exists(path):
        print(f"  ⚠️  File not found: {path} — skipping")
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [r for r in (data or []) if isinstance(r, dict)]


def ok(label):
    print(f"  ✓ {label}")


def skip(label, reason):
    print(f"  · {label} — {reason}")


def load_interface_name_rules_file(filename):
    """Load InterfaceNameRules from a single YAML file."""
    from dcim.models import DeviceType, ModuleType, Platform
    from netbox_interface_name_rules.models import InterfaceNameRule

    rows = load_yaml(filename)
    created = updated = skipped = 0
    for row in rows:
        applies_to_device_interfaces = bool(row.get("applies_to_device_interfaces", False))
        module_type_is_regex = bool(row.get("module_type_is_regex", False))
        module_type_pattern = row.get("module_type_pattern", "")
        module_type_name = row.get("module_type", "")
        parent_module_type_name = row.get("parent_module_type")
        device_type_name = row.get("device_type")
        platform_name = row.get("platform")
        name_template = row.get("name_template", "")
        channel_count = int(row.get("channel_count", 0))
        channel_start = int(row.get("channel_start", 0))
        description = row.get("description", "")
        label = module_type_name or module_type_pattern or device_type_name or "(device-level)"

        if not name_template:
            skip(label, "missing name_template")
            skipped += 1
            continue

        module_type = None
        if not applies_to_device_interfaces and not module_type_is_regex:
            if not module_type_name:
                skip(label, "module_type required when not regex and not device-level")
                skipped += 1
                continue
            try:
                qs = ModuleType.objects.filter(model=module_type_name)
                mfr_name = row.get("manufacturer")
                if mfr_name:
                    qs = qs.filter(manufacturer__name=mfr_name)
                module_type = qs.first()
                if module_type is None:
                    raise ModuleType.DoesNotExist
            except ModuleType.DoesNotExist:
                skip(module_type_name, f"ModuleType {module_type_name!r} not found")
                skipped += 1
                continue

        parent_module_type = None
        if parent_module_type_name:
            try:
                parent_module_type = ModuleType.objects.get(model=parent_module_type_name)
            except (ModuleType.DoesNotExist, ModuleType.MultipleObjectsReturned) as exc:
                skip(label, f"parent ModuleType {parent_module_type_name!r}: {exc}")
                skipped += 1
                continue

        device_type = None
        if device_type_name:
            try:
                device_type = DeviceType.objects.get(model=device_type_name)
            except (DeviceType.DoesNotExist, DeviceType.MultipleObjectsReturned) as exc:
                skip(label, f"DeviceType {device_type_name!r}: {exc}")
                skipped += 1
                continue

        platform = None
        if platform_name:
            try:
                platform = Platform.objects.get(name=platform_name)
            except (Platform.DoesNotExist, Platform.MultipleObjectsReturned):
                try:
                    platform = Platform.objects.get(slug=platform_name)
                except (Platform.DoesNotExist, Platform.MultipleObjectsReturned) as exc:
                    skip(label, f"Platform {platform_name!r}: {exc}")
                    skipped += 1
                    continue

        if applies_to_device_interfaces:
            # Device-level rules are keyed by (pattern, device_type, platform)
            lookup = {
                "applies_to_device_interfaces": True,
                "module_type_pattern": module_type_pattern,
                "device_type": device_type,
                "platform": platform,
            }
            defaults_extra = {"module_type": None, "module_type_is_regex": False, "parent_module_type": None}
        else:
            lookup = {
                "module_type": module_type,
                "module_type_pattern": module_type_pattern if module_type_is_regex else "",
                "module_type_is_regex": module_type_is_regex,
                "parent_module_type": parent_module_type,
                "device_type": device_type,
                "platform": platform,
                "applies_to_device_interfaces": False,
            }
            defaults_extra = {}

        defaults = {
            "name_template": name_template,
            "channel_count": channel_count,
            "channel_start": channel_start,
            "description": description,
            **defaults_extra,
        }
        try:
            obj, was_created = InterfaceNameRule.objects.update_or_create(**lookup, defaults=defaults)
            if was_created:
                ok(f"{label} → {name_template!r}")
                created += 1
            else:
                updated += 1
        except Exception as e:
            skip(label, str(e))
            skipped += 1
    return created, updated, skipped


print("🗂  Loading Interface Name Rules sample data from contrib/")
print()

# Ensure SONiC platform exists so platform-scoped ufispace rules can load
try:
    from dcim.models import Platform

    sonic, created = Platform.objects.get_or_create(
        slug="sonic",
        defaults={"name": "SONiC", "description": "Software for Open Networking in the Cloud"},
    )
    if created:
        print("✓ Created Platform: SONiC (slug=sonic)")
    else:
        print("· Platform SONiC already exists")
except Exception as e:
    print(f"⚠ Could not create SONiC platform: {e}")
print()

# ─── Test devices ─────────────────────────────────────────────────────────────
print("🖥  Creating test devices…")
print()

try:
    from dcim.models import (
        Device,
        DeviceRole,
        DeviceType,
        InterfaceTemplate,
        Manufacturer,
        ModuleBay,
        ModuleBayTemplate,
        ModuleType,
        Site,
        VirtualChassis,
    )

    _site, _ = Site.objects.get_or_create(name="Test Site", defaults={"slug": "test-site"})
    _mfr, _ = Manufacturer.objects.get_or_create(name="Test Manufacturer", defaults={"slug": "test-manufacturer"})
    _role, _ = DeviceRole.objects.get_or_create(name="Test Role", defaults={"slug": "test-role", "color": "9e9e9e"})
except Exception as _e:
    print(f"⚠ Could not set up shared test infrastructure: {_e}")
    import traceback

    traceback.print_exc()
    raise SystemExit(1)

# ── TEST-100BAY device (100 module-bay device for Apply Rules preview tests) ──
try:
    _dt100, _created = DeviceType.objects.get_or_create(
        manufacturer=_mfr,
        model="TEST-100BAY",
        defaults={"slug": "test-100bay", "u_height": 2},
    )
    if _created:
        for i in range(1, 101):
            ModuleBayTemplate.objects.get_or_create(device_type=_dt100, name=f"bay{i}", defaults={"position": str(i)})
        print("  ✓ Created DeviceType TEST-100BAY with 100 module bay templates")

    _mt1port, _ = ModuleType.objects.get_or_create(
        manufacturer=_mfr,
        model="TEST-1PORT",
        defaults={},
    )

    _dev100, _created = Device.objects.get_or_create(
        name="test-100ports",
        defaults={"site": _site, "device_type": _dt100, "role": _role, "status": "active"},
    )
    if _created:
        print("  ✓ Created device test-100ports")
        # Install TEST-1PORT module into every bay so rule 95 can rename its interfaces
        from dcim.models import Module

        for bay in ModuleBay.objects.filter(device=_dev100).order_by("name"):
            Module.objects.get_or_create(
                device=_dev100,
                module_bay=bay,
                defaults={"module_type": _mt1port, "status": "active"},
            )
        print("  ✓ Installed TEST-1PORT modules into all 100 bays")
    else:
        print("  · test-100ports already exists")
except Exception as _e:
    print(f"⚠ Could not create TEST-100BAY device: {_e}")
    import traceback

    traceback.print_exc()

# ── VC-SWITCH device type + VC-LINECARD / VC-SFP module types ─────────────────
try:
    _dt_vc, _created = DeviceType.objects.get_or_create(
        manufacturer=_mfr,
        model="VC-SWITCH",
        defaults={"slug": "vc-switch", "u_height": 1},
    )
    if _created:
        for i in range(1, 5):
            InterfaceTemplate.objects.get_or_create(
                device_type=_dt_vc,
                name=f"Gi0/{i}",
                defaults={"type": "1000base-t"},
            )
        ModuleBayTemplate.objects.get_or_create(
            device_type=_dt_vc,
            name="linecard0",
            defaults={"position": "0"},
        )
        print("  ✓ Created DeviceType VC-SWITCH (4 iface templates + 1 module bay)")

    _mt_vc_lc, _created_lc = ModuleType.objects.get_or_create(
        manufacturer=_mfr,
        model="VC-LINECARD",
        defaults={},
    )
    if _created_lc:
        # Single interface template named "0" — matches bay position, engine renames to Gi{vc_position}/0
        InterfaceTemplate.objects.get_or_create(
            module_type=_mt_vc_lc,
            name="0",
            defaults={"type": "1000base-t"},
        )

    # Add sfp0 sub-bay to VC-LINECARD (idempotent — even if module type already existed)
    ModuleBayTemplate.objects.get_or_create(
        module_type=_mt_vc_lc,
        name="sfp0",
        defaults={"position": "0"},
    )

    # ── VC-SFP module type (for nested SFP testing) ─────────────────────────────
    _mt_vc_sfp, _created_sfp = ModuleType.objects.get_or_create(
        manufacturer=_mfr,
        model="VC-SFP",
        defaults={},
    )
    if _created_sfp:
        InterfaceTemplate.objects.get_or_create(
            module_type=_mt_vc_sfp,
            name="0",
            defaults={"type": "1000base-x-sfp"},
        )
        print("  ✓ Created ModuleType VC-SFP (1 interface template)")
    else:
        print("  · ModuleType VC-SFP already exists")
except Exception as _e:
    print(f"⚠ Could not create VC-SWITCH/VC-LINECARD/VC-SFP types: {_e}")
    import traceback

    traceback.print_exc()

# ── test-vc-stack (e2e test VC) ────────────────────────────────────────────────
try:
    _vc1, _created = Device.objects.get_or_create(
        name="vc-stack-1",
        defaults={"site": _site, "device_type": _dt_vc, "role": _role, "status": "active"},
    )
    if _created:
        print("  ✓ Created device vc-stack-1")
    else:
        print("  · vc-stack-1 already exists")

    _vc2, _created = Device.objects.get_or_create(
        name="vc-stack-2",
        defaults={"site": _site, "device_type": _dt_vc, "role": _role, "status": "active"},
    )
    if _created:
        print("  ✓ Created device vc-stack-2")
    else:
        print("  · vc-stack-2 already exists")

    _stack, _created = VirtualChassis.objects.get_or_create(
        name="test-vc-stack",
        defaults={"master": _vc1},
    )
    if _created:
        print("  ✓ Created VirtualChassis test-vc-stack")
    else:
        print("  · VirtualChassis test-vc-stack already exists")

    # Assign VC membership (idempotent)
    for _dev, _pos in [(_vc1, 1), (_vc2, 2)]:
        if _dev.virtual_chassis != _stack or _dev.vc_position != _pos:
            _dev.virtual_chassis = _stack
            _dev.vc_position = _pos
            _dev.save()
            print(f"  ✓ Assigned {_dev.name} to stack at position {_pos}")

    # Ensure master is set
    if _stack.master != _vc1:
        _stack.master = _vc1
        _stack.save()
except Exception as _e:
    print(f"⚠ Could not create test-vc-stack: {_e}")
    import traceback

    traceback.print_exc()

# ── Demo Virtual Chassis stack (permanent, for manual verification) ────────────
# These are NOT touched by e2e tests, giving the user a stable VC to experiment with.
try:
    _demo_vc1, _created = Device.objects.get_or_create(
        name="demo-vc-1",
        defaults={"site": _site, "device_type": _dt_vc, "role": _role, "status": "active"},
    )
    if _created:
        print("  ✓ Created device demo-vc-1")
    else:
        print("  · demo-vc-1 already exists")

    _demo_vc2, _created = Device.objects.get_or_create(
        name="demo-vc-2",
        defaults={"site": _site, "device_type": _dt_vc, "role": _role, "status": "active"},
    )
    if _created:
        print("  ✓ Created device demo-vc-2")
    else:
        print("  · demo-vc-2 already exists")

    _demo_stack, _created = VirtualChassis.objects.get_or_create(
        name="demo-vc-stack",
        defaults={"master": _demo_vc1},
    )
    if _created:
        print("  ✓ Created VirtualChassis demo-vc-stack")
    else:
        print("  · VirtualChassis demo-vc-stack already exists")

    for _dev, _pos in [(_demo_vc1, 1), (_demo_vc2, 2)]:
        if _dev.virtual_chassis != _demo_stack or _dev.vc_position != _pos:
            _dev.virtual_chassis = _demo_stack
            _dev.vc_position = _pos
            _dev.save()
            print(f"  ✓ Assigned {_dev.name} to demo-vc-stack at position {_pos}")

    if _demo_stack.master != _demo_vc1:
        _demo_stack.master = _demo_vc1
        _demo_stack.save()

    # Clean up any non-VC-SFP modules from sfp0 bays on demo devices (stale from manual testing)
    from dcim.models import Module, ModuleBay

    for _demo_dev in [_demo_vc1, _demo_vc2]:
        for _sub_bay in ModuleBay.objects.filter(device=_demo_dev, name="sfp0"):
            _stale_mods = Module.objects.filter(device=_demo_dev, module_bay=_sub_bay).exclude(
                module_type__model="VC-SFP"
            )
            for _m in _stale_mods:
                _m.delete()
                print(f"  ✓ Removed stale {_m.module_type.model} from {_demo_dev.name} sfp0 (cleanup)")
except Exception as _e:
    print(f"⚠ Could not create demo-vc-stack: {_e}")
    import traceback

    traceback.print_exc()

# ── Vendor demo VCs (Juniper/Cisco/Arista) ────────────────────────────────────
try:
    # ── Juniper EX-style VC (demo: 2 members, positions 0 and 1) ────────────────
    # Models Juniper EX4300 Virtual Chassis with ge-0/0/N and xe-0/1/N port naming.
    # In a Juniper VC, member IDs are 0-based. Interfaces are named ge-{member}/0/{port}.
    _dt_jnp, _created = DeviceType.objects.get_or_create(
        manufacturer=_mfr,
        model="JNP-EX-VC",
        defaults={"slug": "jnp-ex-vc", "u_height": 1},
    )
    if _created:
        for i in range(4):
            InterfaceTemplate.objects.get_or_create(
                device_type=_dt_jnp,
                name=f"ge-0/0/{i}",
                defaults={"type": "1000base-t"},
            )
        for i in range(2):
            InterfaceTemplate.objects.get_or_create(
                device_type=_dt_jnp,
                name=f"xe-0/1/{i}",
                defaults={"type": "10gbase-x-xfp"},
            )
        print("  ✓ Created DeviceType JNP-EX-VC (4×ge-0/0/N + 2×xe-0/1/N templates)")

    for _name, _pos in [("jnp-vc-1", 0), ("jnp-vc-2", 1)]:
        _dev, _c = Device.objects.get_or_create(
            name=_name,
            defaults={"site": _site, "device_type": _dt_jnp, "role": _role, "status": "active"},
        )
        if _c:
            print(f"  ✓ Created device {_name}")

    _jnp1 = Device.objects.get(name="jnp-vc-1")
    _jnp2 = Device.objects.get(name="jnp-vc-2")
    _jnp_stack, _created = VirtualChassis.objects.get_or_create(
        name="juniper-vc-stack",
        defaults={"master": _jnp1},
    )
    if _created:
        print("  ✓ Created VirtualChassis juniper-vc-stack")
    for _dev, _pos in [(_jnp1, 0), (_jnp2, 1)]:
        if _dev.virtual_chassis != _jnp_stack or _dev.vc_position != _pos:
            _dev.virtual_chassis = _jnp_stack
            _dev.vc_position = _pos
            _dev.save()
            print(f"  ✓ Assigned {_dev.name} to juniper-vc-stack at position {_pos}")
    if _jnp_stack.master != _jnp1:
        _jnp_stack.master = _jnp1
        _jnp_stack.save()

    # ── Cisco Catalyst-style stack (demo: 2 members, positions 1 and 2) ─────────
    # Models Cisco Catalyst 9300 IOS-XE stack. Member IDs are 1-based.
    # Interfaces are named GigabitEthernet{member}/0/{port}.
    _dt_cisco, _created = DeviceType.objects.get_or_create(
        manufacturer=_mfr,
        model="CISCO-C9K",
        defaults={"slug": "cisco-c9k", "u_height": 1},
    )
    if _created:
        for i in range(1, 5):
            InterfaceTemplate.objects.get_or_create(
                device_type=_dt_cisco,
                name=f"GigabitEthernet1/0/{i}",
                defaults={"type": "1000base-t"},
            )
        print("  ✓ Created DeviceType CISCO-C9K (4×GigabitEthernet1/0/N templates)")

    for _name, _pos in [("cisco-sw-1", 1), ("cisco-sw-2", 2)]:
        _dev, _c = Device.objects.get_or_create(
            name=_name,
            defaults={"site": _site, "device_type": _dt_cisco, "role": _role, "status": "active"},
        )
        if _c:
            print(f"  ✓ Created device {_name}")

    _csw1 = Device.objects.get(name="cisco-sw-1")
    _csw2 = Device.objects.get(name="cisco-sw-2")
    _cisco_stack, _created = VirtualChassis.objects.get_or_create(
        name="cisco-stack",
        defaults={"master": _csw1},
    )
    if _created:
        print("  ✓ Created VirtualChassis cisco-stack")
    for _dev, _pos in [(_csw1, 1), (_csw2, 2)]:
        if _dev.virtual_chassis != _cisco_stack or _dev.vc_position != _pos:
            _dev.virtual_chassis = _cisco_stack
            _dev.vc_position = _pos
            _dev.save()
            print(f"  ✓ Assigned {_dev.name} to cisco-stack at position {_pos}")
    if _cisco_stack.master != _csw1:
        _cisco_stack.master = _csw1
        _cisco_stack.save()

    # ── Arista EOS-style stack (demo: 2 members, positions 1 and 2) ─────────────
    # Models Arista modular leaf switch naming: Ethernet{slot}/{port}.
    # In a multi-chassis setup, slot maps to the VC member position.
    _dt_arista, _created = DeviceType.objects.get_or_create(
        manufacturer=_mfr,
        model="ARISTA-EOS",
        defaults={"slug": "arista-eos", "u_height": 1},
    )
    if _created:
        for i in range(1, 5):
            InterfaceTemplate.objects.get_or_create(
                device_type=_dt_arista,
                name=f"Ethernet1/{i}",
                defaults={"type": "10gbase-x-sfpp"},
            )
        print("  ✓ Created DeviceType ARISTA-EOS (4×Ethernet1/N templates)")

    for _name, _pos in [("arista-sw-1", 1), ("arista-sw-2", 2)]:
        _dev, _c = Device.objects.get_or_create(
            name=_name,
            defaults={"site": _site, "device_type": _dt_arista, "role": _role, "status": "active"},
        )
        if _c:
            print(f"  ✓ Created device {_name}")

    _asw1 = Device.objects.get(name="arista-sw-1")
    _asw2 = Device.objects.get(name="arista-sw-2")
    _arista_stack, _created = VirtualChassis.objects.get_or_create(
        name="arista-stack",
        defaults={"master": _asw1},
    )
    if _created:
        print("  ✓ Created VirtualChassis arista-stack")
    for _dev, _pos in [(_asw1, 1), (_asw2, 2)]:
        if _dev.virtual_chassis != _arista_stack or _dev.vc_position != _pos:
            _dev.virtual_chassis = _arista_stack
            _dev.vc_position = _pos
            _dev.save()
            print(f"  ✓ Assigned {_dev.name} to arista-stack at position {_pos}")
    if _arista_stack.master != _asw1:
        _arista_stack.master = _asw1
        _arista_stack.save()

except Exception as _e:
    print(f"⚠ Could not create vendor demo VCs (Juniper/Cisco/Arista): {_e}")
    import traceback

    traceback.print_exc()

# Fix VirtualChassis member_count denormalized counter (can get out of sync from testing)
try:
    from django.core.management import call_command

    call_command("calculate_cached_counts", verbosity=0)
    print("  ✓ Refreshed cached counts (member_count)")
except Exception as _e:
    print(f"  ⚠ calculate_cached_counts failed: {_e}")

print()

if not os.path.isdir(CONTRIB_DIR):
    print(f"⚠️  contrib directory not found: {CONTRIB_DIR}")
    print("   Cannot load sample data — exiting.")
    raise SystemExit(1)

rule_files = [f for f in os.listdir(CONTRIB_DIR) if f.endswith(".yaml") and f != "README.yaml"]
total_created = total_updated = total_skipped = 0
for fname in sorted(rule_files):
    print(f"📋 Loading {fname}…")
    c, u, s = load_interface_name_rules_file(fname)
    print(f"  → {c} created, {u} updated, {s} skipped\n")
    total_created += c
    total_updated += u
    total_skipped += s

print(f"✅ Done: {total_created} created, {total_updated} updated, {total_skipped} skipped.")

# Re-apply device-level interface rules for all current VC members.
# Devices are added to VCs above, before rules are loaded — so the signal
# fires before rules exist. Trigger a manual re-apply now that rules are in place.
print()
print("🔄 Re-applying device-level interface rules for all VC members…")
try:
    from dcim.models import Device
    from netbox_interface_name_rules.engine import apply_device_interface_rules

    _vc_devices = Device.objects.filter(virtual_chassis__isnull=False).select_related(
        "virtual_chassis", "device_type", "platform"
    )
    _total_renamed = 0
    for _d in _vc_devices:
        _n = apply_device_interface_rules(_d)
        if _n:
            print(f"  ✓ Renamed {_n} interface(s) on {_d.name} (pos={_d.vc_position})")
            _total_renamed += _n
    if _total_renamed == 0:
        print("  · No interfaces renamed (rules already match current names or no matching rules)")
    else:
        print(f"  ✓ Total: {_total_renamed} interface(s) renamed across all VC members")
except Exception as _e:
    print(f"  ⚠ Re-apply failed: {_e}")
    import traceback

    traceback.print_exc()
