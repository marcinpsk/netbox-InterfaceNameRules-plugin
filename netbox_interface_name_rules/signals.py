# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django signals for automatic interface renaming on module install."""

import functools
import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger("netbox_interface_name_rules")

# NOTE: We intentionally do NOT hook into pre_save on dcim.Interface.
#
# NetBox's Module.save() creates interfaces via bulk_create() which bypasses
# pre_save signals entirely.  NetBox then manually dispatches post_save for
# the Module object.  The actual renaming path is therefore:
#
#   Module.save()
#     → bulk_create() all interfaces (pre_save never fires)
#     → NetBox fires post_save(Module)
#     → on_module_saved → transaction.on_commit
#     → _apply_rules_deferred → apply_interface_name_rules()
#
# Adding a pre_save on Interface would be a no-op for the normal install
# path and would create a false sense of security.


@receiver(pre_save, sender="dcim.Module", dispatch_uid="interface_name_rules_pre_save_module")
def on_module_pre_save(sender, instance, **kwargs):
    """Capture old module_type_id before a Module save (for type-change detection)."""
    if instance.pk is None:
        instance._prev_module_type_id = None
        return
    try:
        old = sender.objects.filter(pk=instance.pk).values("module_type_id").first()
        instance._prev_module_type_id = old["module_type_id"] if old else None
    except Exception:
        logger.warning("Failed to capture previous module_type_id for Module pk=%s", instance.pk, exc_info=True)
        instance._prev_module_type_id = None


@receiver(post_save, sender="dcim.Module", dispatch_uid="interface_name_rules_post_save_module")
def on_module_saved(sender, instance, created, **kwargs):
    """Apply interface name rules after module install or module-type change.

    Primary renaming path: NetBox's Module.save() creates interfaces via
    bulk_create() and then manually dispatches post_save for the Module.
    This handler defers the actual renaming to on_commit so that all
    interfaces are visible in the DB before apply_interface_name_rules() runs.

    Also handles module-type changes: when an existing module's type is changed
    via the API (PATCH), force-reapply rules to rename existing interfaces
    according to the new module type's rule.
    """
    module = instance
    module_bay = getattr(module, "module_bay", None)
    if not module_bay:
        return

    if created:
        callback = functools.partial(_apply_rules_deferred, module.pk, module_bay.pk)
        transaction.on_commit(callback)
        return

    # Module type change: re-apply rules with force_reapply=True
    prev_module_type_id = getattr(instance, "_prev_module_type_id", None)
    if prev_module_type_id is None:
        return
    if prev_module_type_id == instance.module_type_id:
        return

    logger.debug(
        "Module %s type changed (%s → %s) — scheduling rule re-apply",
        instance.pk,
        prev_module_type_id,
        instance.module_type_id,
    )
    callback = functools.partial(_apply_rules_deferred, module.pk, module_bay.pk, force_reapply=True)
    transaction.on_commit(callback)


def _apply_rules_deferred(module_pk, module_bay_pk, force_reapply=False):
    """Apply interface name rules after transaction commit.

    Called via transaction.on_commit so all interfaces created by bulk_create()
    are visible in the DB before renaming begins.

    Args:
        module_pk: Primary key of the Module whose interfaces should be renamed.
        module_bay_pk: Primary key of the ModuleBay containing the module.
        force_reapply: When True, re-apply even if interfaces already have
            the expected names (used for module-type changes).

    """
    from dcim.models import Module, ModuleBay

    try:
        module = Module.objects.get(pk=module_pk)
        module_bay = ModuleBay.objects.get(pk=module_bay_pk)
    except (Module.DoesNotExist, ModuleBay.DoesNotExist):
        return

    try:
        from .engine import apply_interface_name_rules

        renamed = apply_interface_name_rules(module, module_bay, force_reapply=force_reapply)
        if renamed:
            logger.info(
                "Renamed %d interface(s) for %s in %s",
                renamed,
                module.module_type,
                module_bay.name,
            )
    except Exception:
        logger.exception(
            "Failed to apply interface name rules for %s in %s",
            module.module_type,
            module_bay.name,
        )


# ---------------------------------------------------------------------------
# Virtual Chassis membership change detection
# ---------------------------------------------------------------------------
# When a Device's vc_position or virtual_chassis changes, any module-attached
# interfaces with rules using {vc_position} need to be renamed.  We capture
# the old values in pre_save (stored on the instance) and compare in post_save.


@receiver(pre_save, sender="dcim.Device", dispatch_uid="interface_name_rules_pre_save_device")
def on_device_pre_save(sender, instance, **kwargs):
    """Capture old virtual_chassis_id and vc_position before a Device save."""
    if instance.pk is None:
        # New device — nothing to compare
        instance._prev_virtual_chassis_id = None
        instance._prev_vc_position = None
        return
    try:
        old = sender.objects.filter(pk=instance.pk).values("virtual_chassis_id", "vc_position").first()
        instance._prev_virtual_chassis_id = old["virtual_chassis_id"] if old else None
        instance._prev_vc_position = old["vc_position"] if old else None
    except Exception:
        logger.warning("Failed to capture previous VC state for Device pk=%s", instance.pk, exc_info=True)
        instance._prev_virtual_chassis_id = None
        instance._prev_vc_position = None


@receiver(post_save, sender="dcim.Device", dispatch_uid="interface_name_rules_post_save_device")
def on_device_saved(sender, instance, created, **kwargs):
    """Re-apply interface name rules when a device's VC membership or position changes."""
    if created:
        return

    prev_vc_id = getattr(instance, "_prev_virtual_chassis_id", None)
    prev_vc_pos = getattr(instance, "_prev_vc_position", None)
    new_vc_id = instance.virtual_chassis_id
    new_vc_pos = instance.vc_position

    vc_id_changed = prev_vc_id != new_vc_id
    vc_pos_changed = prev_vc_pos != new_vc_pos

    if not (vc_id_changed or vc_pos_changed):
        return

    # Only rename when the device is (now) in a VC — if it was removed, no {vc_position} available.
    if new_vc_id is None:
        logger.debug(
            "Device %s removed from VC — skipping interface rename (no vc_position available)",
            instance.pk,
        )
        return

    callback = functools.partial(_apply_rules_for_device_deferred, instance.pk)
    transaction.on_commit(callback)


def _apply_rules_for_device_deferred(device_pk):
    """Re-apply all interface name rules for a device after a VC membership or position change.

    Handles two categories of interfaces:
    - Module-attached interfaces: re-applies module-level rules with force_reapply=True
      so {vc_position} is updated even when the interface name already matches the template.
    - Device-level interfaces (module=None): re-applies device-level rules which use
      applies_to_device_interfaces=True rules matched by interface-name regex.

    Args:
        device_pk: Primary key of the Device to re-apply rules for.

    """
    from dcim.models import Device, Module

    try:
        device = Device.objects.select_related("virtual_chassis").get(pk=device_pk)
    except Device.DoesNotExist:
        return

    total = 0

    # Re-apply module interface rules
    modules = Module.objects.filter(device=device).select_related(
        "module_bay",
        "module_type",
        "device",
        "device__device_type",
        "device__platform",
        "device__virtual_chassis",
    )
    try:
        from .engine import apply_interface_name_rules

        for module in modules:
            module_bay = module.module_bay
            if not module_bay:
                continue
            try:
                renamed = apply_interface_name_rules(module, module_bay, force_reapply=True)
                total += renamed or 0
            except Exception:
                logger.exception(
                    "Failed to re-apply rules for %s in %s after VC change",
                    module.module_type,
                    module_bay.name,
                )
    except Exception:
        logger.exception("Failed to re-apply module rules for device %s after VC change", device_pk)

    # Re-apply device-level interface rules (interfaces with module=None)
    try:
        from .engine import apply_device_interface_rules

        renamed = apply_device_interface_rules(device)
        total += renamed or 0
    except Exception:
        logger.exception("Failed to re-apply device interface rules for device %s after VC change", device_pk)

    if total:
        logger.info(
            "Re-renamed %d interface(s) for device %s after VC change",
            total,
            device,
        )


# ---------------------------------------------------------------------------
# Cross-plugin integration: netbox-librenms-plugin name prediction
# ---------------------------------------------------------------------------
# When netbox-librenms-plugin is installed it exposes a Signal that asks
# subscribers to rewrite the list of interface names it predicts a module's
# templates will produce. Without this rewrite, its module-adoption lookup
# would only see NetBox's raw template output and miss interfaces we renamed
# after install. The import is guarded so INR works fine when librenms-plugin
# is absent.

try:
    from netbox_librenms_plugin.signals import (
        predict_module_interface_names as _librenms_predict_signal,
    )
except ImportError:
    _librenms_predict_signal = None


if _librenms_predict_signal is not None:

    @receiver(
        _librenms_predict_signal,
        dispatch_uid="interface_name_rules_predict_module_interface_names",
    )
    def on_librenms_predict_module_interface_names(sender, device, module, names, **kwargs):
        """Rewrite librenms-plugin's predicted template names through our rule engine."""
        module_bay = getattr(module, "module_bay", None)
        if module_bay is None:
            return None
        try:
            from .engine import predict_rule_output

            return predict_rule_output(module, module_bay, names)
        except Exception:
            logger.exception(
                "predict_rule_output failed for module pk=%s; leaving names unchanged",
                getattr(module, "pk", "?"),
            )
            return None
