# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django signals for automatic interface renaming on module install."""

import functools
import logging
import threading

from django.db import connection, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger("netbox_interface_name_rules")

# Delay for auto-commit mode (seconds). Allows Module.save() to finish
# creating component instances before the rename logic runs.
_AUTOCOMMIT_DELAY = 0.5


@receiver(post_save, sender="dcim.Module", dispatch_uid="interface_name_rules_post_save_module")
def on_module_saved(sender, instance, created, **kwargs):
    """Apply interface name rules when a module is installed (created).

    NetBox creates module component instances (interfaces) in Module.save()
    *after* super().save() returns — the point at which post_save fires.

    In an explicit transaction (UI/API with atomic block),
    transaction.on_commit() correctly defers execution until after the
    full save completes, including interface creation.

    In auto-commit mode, on_commit() fires immediately — a short timer
    is used as a fallback to allow Module.save() to finish.
    """
    if not created:
        return

    module = instance
    module_bay = getattr(module, "module_bay", None)
    if not module_bay:
        return

    callback = functools.partial(_apply_rules_deferred, module.pk, module_bay.pk)

    if connection.in_atomic_block:
        transaction.on_commit(callback)
    else:
        threading.Timer(_AUTOCOMMIT_DELAY, callback).start()


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
