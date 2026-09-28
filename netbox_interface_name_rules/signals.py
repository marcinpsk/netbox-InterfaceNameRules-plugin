# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django receivers: module, module bay and device saves go to the rename-trigger lifecycle."""

import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from . import rename_triggers

logger = logging.getLogger("netbox_interface_name_rules")

# No Interface receiver: NetBox creates module interfaces with bulk_create(), so pre_save never runs.


@receiver(pre_save, sender="dcim.Module", dispatch_uid="interface_name_rules_pre_save_module")
def on_module_pre_save(sender, instance, **kwargs):
    """Pass a module save to the rename-trigger lifecycle before the row is written."""
    rename_triggers.before_save(sender, instance)


@receiver(post_save, sender="dcim.Module", dispatch_uid="interface_name_rules_post_save_module")
def on_module_saved(sender, instance, created, **kwargs):
    """Pass a module save to the rename-trigger lifecycle after the row is written."""
    rename_triggers.after_save(sender, instance, created)


@receiver(pre_save, sender="dcim.ModuleBay", dispatch_uid="interface_name_rules_pre_save_module_bay")
def on_module_bay_pre_save(sender, instance, **kwargs):
    """Pass a module bay save to the rename-trigger lifecycle before the row is written."""
    rename_triggers.before_save(sender, instance)


@receiver(post_save, sender="dcim.ModuleBay", dispatch_uid="interface_name_rules_post_save_module_bay")
def on_module_bay_saved(sender, instance, created, **kwargs):
    """Pass a module bay save to the rename-trigger lifecycle after the row is written."""
    rename_triggers.after_save(sender, instance, created)


@receiver(pre_save, sender="dcim.Device", dispatch_uid="interface_name_rules_pre_save_device")
def on_device_pre_save(sender, instance, **kwargs):
    """Pass a device save to the rename-trigger lifecycle before the row is written."""
    rename_triggers.before_save(sender, instance)


@receiver(post_save, sender="dcim.Device", dispatch_uid="interface_name_rules_post_save_device")
def on_device_saved(sender, instance, created, **kwargs):
    """Pass a device save to the rename-trigger lifecycle after the row is written."""
    rename_triggers.after_save(sender, instance, created)


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


def on_librenms_predict_module_interface_names(sender, device, module, names, **kwargs):
    """Rewrite librenms-plugin's predicted template names through our rule engine.

    Always defined so it can be imported and tested in isolation even when
    netbox-librenms-plugin is not installed.  Signal registration is guarded
    below so it only connects when the signal actually exists.
    """
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


if _librenms_predict_signal is not None:
    receiver(
        _librenms_predict_signal,
        dispatch_uid="interface_name_rules_predict_module_interface_names",
    )(on_librenms_predict_module_interface_names)
