# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Rename triggers: read the previous state, decide, and reapply the rules after commit.

The receivers in ``signals.py`` pass every module and device save here. ``before_save`` reads the
previous state; ``after_save`` compares it with the saved values and schedules a reapply after
commit. The reapply compares the previous state with the committed row.
"""

import dataclasses
import logging
import weakref

from django.db import transaction

logger = logging.getLogger("netbox_interface_name_rules")


@dataclasses.dataclass(frozen=True)
class ModuleState:
    """The module values a rename trigger compares."""

    module_type_id: int


@dataclasses.dataclass(frozen=True)
class DeviceState:
    """The device values a rename trigger compares."""

    virtual_chassis_id: int | None
    vc_position: int | None


def _state_of(state_class, instance):
    """Return the *state_class* values that *instance* holds."""
    return state_class(**{field.name: getattr(instance, field.name) for field in dataclasses.fields(state_class)})


@dataclasses.dataclass(eq=False)
class ModuleReapply:
    """Reapply the rules to one module after commit.

    ``baseline`` is the previous state, or the state the module was installed with when
    ``installed`` is set.
    """

    pk: int
    baseline: ModuleState
    installed: bool

    @classmethod
    def after_install(cls, module):
        """Return the reapply for a module installed by this save."""
        return cls(module.pk, _state_of(ModuleState, module), installed=True)

    def force_reapply(self, current):
        """Return the ``force_reapply`` flag for *current*, or None when no reapply is due."""
        changed = current != self.baseline
        return changed if self.installed or changed else None

    def __call__(self):
        """Reapply the module's rule against the committed row."""
        from dcim.models import Module, ModuleBay

        try:
            module = Module.objects.get(pk=self.pk)
            module_bay = ModuleBay.objects.get(pk=module.module_bay_id)
        except (Module.DoesNotExist, ModuleBay.DoesNotExist):
            return
        force_reapply = self.force_reapply(_state_of(ModuleState, module))
        if force_reapply is None:
            return
        try:
            from .engine import apply_interface_name_rules

            renamed = apply_interface_name_rules(module, module_bay, force_reapply=force_reapply)
        except Exception:
            logger.exception("Failed to apply interface name rules for %s in %s", module.module_type, module_bay.name)
            return
        if renamed:
            logger.info("Renamed %d interface(s) for %s in %s", renamed, module.module_type, module_bay.name)


@dataclasses.dataclass(eq=False)
class DeviceReapply:
    """Reapply the module and device-interface rules of one device after commit.

    ``baseline`` is the previous state.
    """

    pk: int
    baseline: DeviceState

    def is_due(self, current):
        """Return whether *current* differs from the baseline while the device is in a virtual chassis."""
        return current != self.baseline and current.virtual_chassis_id is not None

    def __call__(self):
        """Reapply the device's rules against the committed row."""
        from dcim.models import Device

        try:
            device = Device.objects.select_related("virtual_chassis").get(pk=self.pk)
        except Device.DoesNotExist:
            return
        if not self.is_due(_state_of(DeviceState, device)):
            return
        total = 0
        try:
            from .engine import reapply_module_rules

            total += reapply_module_rules(device)
        except Exception:
            logger.exception("Failed to re-apply module rules for device %s after VC change", self.pk)
        try:
            from .engine import apply_device_interface_rules

            total += apply_device_interface_rules(device) or 0
        except Exception:
            logger.exception("Failed to re-apply device interface rules for device %s after VC change", self.pk)
        if total:
            logger.info("Re-renamed %d interface(s) for device %s after VC change", total, device)


def _module_reapply(module, created, previous):
    """Return the reapply a module save asks for, or None when the save is not a rename trigger."""
    if created:
        return ModuleReapply.after_install(module)
    if previous is None:
        return None
    reapply = ModuleReapply(module.pk, previous, installed=False)
    current = _state_of(ModuleState, module)
    if reapply.force_reapply(current) is None:
        return None
    logger.debug(
        "Module %s type changed from %s to %s; scheduling a reapply",
        module.pk,
        previous.module_type_id,
        current.module_type_id,
    )
    return reapply


def _device_reapply(device, created, previous):
    """Return the reapply a device save asks for, or None when the save is not a rename trigger."""
    if created or previous is None:
        return None
    reapply = DeviceReapply(device.pk, previous)
    current = _state_of(DeviceState, device)
    if reapply.is_due(current):
        return reapply
    if current != previous:
        logger.debug("Device %s left its virtual chassis; no reapply without a vc_position", device.pk)
    return None


_TRIGGERS = {
    "dcim.Module": (ModuleState, _module_reapply),
    "dcim.Device": (DeviceState, _device_reapply),
}

# Keyed by id(): model equality follows the primary key, so two instances of one row would collide.
_previous_states = {}


def before_save(sender, instance):
    """Read the previous state of *instance* and hold it for its post_save. A read error propagates."""
    state_class, _ = _TRIGGERS[sender._meta.label]
    previous = None
    if instance.pk is not None:
        names = [field.name for field in dataclasses.fields(state_class)]
        row = sender.objects.filter(pk=instance.pk).values(*names).first()
        previous = None if row is None else state_class(**row)
    key = id(instance)

    def forget(reference):
        # A save that failed before post_save leaves its entry until the instance is collected.
        if _previous_states.get(key, (None,))[0] is reference:
            _previous_states.pop(key, None)

    _previous_states[key] = (weakref.ref(instance, forget), previous)


def after_save(sender, instance, created):
    """Schedule a reapply after commit when the save of *instance* is a rename trigger."""
    _, trigger = _TRIGGERS[sender._meta.label]
    # No entry: NetBox sent this post_save by hand, without a model save.
    _, previous = _previous_states.pop(id(instance), (None, None))
    reapply = trigger(instance, created, previous)
    if reapply is not None:
        transaction.on_commit(reapply)
