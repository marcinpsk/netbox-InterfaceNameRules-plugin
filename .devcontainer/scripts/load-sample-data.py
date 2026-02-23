#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
#
# Load Interface Name Rules from contrib/ YAML files into the devcontainer NetBox.
# Run via: python manage.py shell < /path/to/load-sample-data.py

import os

import yaml

CONTRIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "contrib")


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

        if not name_template:
            skip(f"{module_type_name or module_type_pattern}", "missing name_template")
            skipped += 1
            continue

        module_type = None
        if not module_type_is_regex:
            if not module_type_name:
                skip("(no module_type)", "module_type required when not regex")
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
                skip(module_type_name or module_type_pattern, f"parent ModuleType {parent_module_type_name!r}: {exc}")
                skipped += 1
                continue

        device_type = None
        if device_type_name:
            try:
                device_type = DeviceType.objects.get(model=device_type_name)
            except (DeviceType.DoesNotExist, DeviceType.MultipleObjectsReturned) as exc:
                skip(module_type_name or module_type_pattern, f"DeviceType {device_type_name!r}: {exc}")
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
                    skip(module_type_name or module_type_pattern, f"Platform {platform_name!r}: {exc}")
                    skipped += 1
                    continue

        lookup = {
            "module_type": module_type,
            "module_type_pattern": module_type_pattern if module_type_is_regex else "",
            "module_type_is_regex": module_type_is_regex,
            "parent_module_type": parent_module_type,
            "device_type": device_type,
            "platform": platform,
        }
        defaults = {
            "name_template": name_template,
            "channel_count": channel_count,
            "channel_start": channel_start,
            "description": description,
        }
        try:
            obj, was_created = InterfaceNameRule.objects.update_or_create(**lookup, defaults=defaults)
            label = module_type_name or f"regex:{module_type_pattern}"
            if was_created:
                ok(f"{label} → {name_template!r}")
                created += 1
            else:
                updated += 1
        except Exception as e:
            skip(module_type_name or module_type_pattern, str(e))
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
