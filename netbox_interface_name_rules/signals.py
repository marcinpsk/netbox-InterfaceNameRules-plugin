# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django signals for automatic interface renaming on module install."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger("netbox_interface_name_rules")


@receiver(post_save, sender="dcim.Module", dispatch_uid="interface_name_rules_post_save_module")
def on_module_saved(sender, instance, created, **kwargs):
    """Apply interface name rules when a module is installed (created).

    Triggered by Django's post_save signal on dcim.Module.  Only acts on
    newly-created modules (created=True) to avoid double-renaming on updates.
    """
    if not created:
        return

    module = instance
    module_bay = getattr(module, "module_bay", None)
    if not module_bay:
        return

    try:
        from .engine import apply_interface_name_rules

        renamed = apply_interface_name_rules(module, module_bay)
        if renamed:
            logger.info(
                "Renamed %d interface(s) on %s after installing %s in %s",
                renamed,
                module.device,
                module.module_type,
                module_bay.name,
            )
    except Exception:
        logger.exception(
            "Failed to apply interface name rules for %s in %s",
            module.module_type,
            module_bay.name,
        )
