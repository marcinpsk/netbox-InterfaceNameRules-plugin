# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Builders for the DCIM objects the tests need.

Every builder takes a *prefix* and derives names and slugs from it. Test classes share one database
per worker, so a class that names its objects after itself cannot collide with another class, and a
failure still names the class it came from.
"""

from dataclasses import dataclass

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Manufacturer,
    ModuleBayTemplate,
    ModuleType,
    Site,
)


def slug_for(prefix: str, suffix: str = "") -> str:
    """Return a slug built from *prefix*, safe to use as a NetBox slug."""
    cleaned = "".join(character if character.isalnum() else "-" for character in prefix).strip("-").lower()
    return f"{cleaned}-{suffix}" if suffix else cleaned


def make_manufacturer(prefix: str) -> Manufacturer:
    """Return one manufacturer named after *prefix*."""
    return Manufacturer.objects.create(name=f"{prefix} Manufacturer", slug=slug_for(prefix, "mfg"))


def make_device_type(manufacturer: Manufacturer, prefix: str, model: str | None = None) -> DeviceType:
    """Return one device type named after *prefix*."""
    model = model or f"{prefix} Device Type"
    return DeviceType.objects.create(manufacturer=manufacturer, model=model, slug=slug_for(prefix, "type"))


def make_module_type(
    manufacturer: Manufacturer,
    prefix: str,
    model: str | None = None,
    part_number: str | None = None,
) -> ModuleType:
    """Return one module type named after *prefix*, or after an explicit *model*."""
    model = model or f"{prefix} Module Type"
    return ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=part_number or model)


def make_module_bay_templates(device_type: DeviceType, names: tuple[str, ...]) -> list[ModuleBayTemplate]:
    """Return module bay templates on *device_type*, positioned in the order given.

    NetBox instantiates the bays when a device is created, so these must exist before the device.
    """
    return [
        ModuleBayTemplate.objects.create(device_type=device_type, name=name, position=str(position))
        for position, name in enumerate(names)
    ]


@dataclass(frozen=True)
class DevicePlacement:
    """The role and site a device needs, kept together so a test can reuse them."""

    role: DeviceRole
    site: Site


def make_placement(prefix: str) -> DevicePlacement:
    """Return the role and site for devices named after *prefix*."""
    return DevicePlacement(
        role=DeviceRole.objects.create(name=f"{prefix} Role", slug=slug_for(prefix, "role")),
        site=Site.objects.create(name=f"{prefix} Site", slug=slug_for(prefix, "site")),
    )


def make_device(
    prefix: str,
    device_type: DeviceType,
    placement: DevicePlacement | None = None,
    name: str | None = None,
    **fields,
) -> Device:
    """Return one device on *device_type*, creating a role and site when none is given."""
    placement = placement or make_placement(prefix)
    return Device.objects.create(
        name=name or slug_for(prefix, "01"),
        device_type=device_type,
        role=placement.role,
        site=placement.site,
        **fields,
    )
