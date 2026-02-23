# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Django signals for automatic interface renaming on module install."""

import functools
import logging

from django.db import transaction
from django.db.models.signals import post_save
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


@receiver(post_save, sender="dcim.Module", dispatch_uid="interface_name_rules_post_save_module")
def on_module_saved(sender, instance, created, **kwargs):
    """Apply interface name rules after module install — primary renaming path.

    NetBox's Module.save() creates interfaces via bulk_create() and then
    manually dispatches post_save for the Module.  This handler defers the
    actual renaming to on_commit so that all interfaces are visible in the DB
    before apply_interface_name_rules() runs.  Handles both simple renames
    and breakout channel creation.
    """
    if not created:
        return

    module = instance
    module_bay = getattr(module, "module_bay", None)
    if not module_bay:
        return

    callback = functools.partial(_apply_rules_deferred, module.pk, module_bay.pk)
    transaction.on_commit(callback)


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
