# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django signals for automatic interface renaming on module install."""

import functools
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger("netbox_interface_name_rules")


@receiver(post_save, sender="dcim.Module", dispatch_uid="interface_name_rules_post_save_module")
def on_module_saved(sender, instance, created, **kwargs):
    """Apply interface name rules when a module is installed (created).

    Triggered by Django's post_save signal on dcim.Module.  Only acts on
    newly-created modules (created=True) to avoid double-renaming on updates.

    Uses transaction.on_commit() because NetBox creates module component
    instances (interfaces, etc.) after super().save() returns — the point
    at which post_save fires.  Deferring to on_commit ensures the
    interfaces exist by the time the rename logic runs.
    """
    if not created:
        return

    module = instance
    module_bay = getattr(module, "module_bay", None)
    if not module_bay:
        return

    transaction.on_commit(functools.partial(_apply_rules_deferred, module.pk, module_bay.pk))


def _apply_rules_deferred(module_pk, module_bay_pk):
    """Apply interface name rules after transaction commit."""
    from dcim.models import Module, ModuleBay

    try:
        module = Module.objects.get(pk=module_pk)
        module_bay = ModuleBay.objects.get(pk=module_bay_pk)
    except (Module.DoesNotExist, ModuleBay.DoesNotExist):
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
