# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared live interface-name primitives for family execution."""

import logging

from dcim.models import Interface
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)

COLLISION_REASON = "target name is already in use"
INTERFACE_NAME_CONSTRAINT = "dcim_interface_unique_device_name"


def name_is_taken(device_id, target_name, db_alias, exclude_pk) -> bool:
    """Return whether an interface other than *exclude_pk* already owns *target_name* on the device."""
    return (
        Interface.objects.using(db_alias).filter(device_id=device_id, name=target_name).exclude(pk=exclude_pk).exists()
    )


def is_name_collision(error: IntegrityError) -> bool:
    """Return whether PostgreSQL identified NetBox's interface-name constraint."""
    cause = error.__cause__
    diagnostics = getattr(cause, "diag", None)
    return getattr(diagnostics, "constraint_name", None) == INTERFACE_NAME_CONSTRAINT


def restore_deferred_channel_names(reconciliations, db_alias):
    """Restore plugin-owned names that NetBox's parent cascade changed after commit."""
    child_pks = [child_pk for child_pk, _final_name, _cascade_name in reconciliations]
    with transaction.atomic(using=db_alias):
        children = (
            Interface.objects.using(db_alias)
            .select_for_update(of=("self",))
            .select_related("device")
            .order_by("pk")
            .in_bulk(child_pks)
        )
        for child_pk, final_name, cascade_name in reconciliations:
            child = children.get(child_pk)
            if child is None or child.name == final_name:
                continue
            if child.name != cascade_name:
                logger.warning(
                    "Channel interface %s changed to unexpected name %r before deferred reconciliation; "
                    "leaving it unchanged.",
                    child_pk,
                    child.name,
                )
                continue
            previous_name = child.name
            try:
                with transaction.atomic(using=db_alias):
                    child.name = final_name
                    child.full_clean()
                    child.save(using=db_alias)
            except ValidationError:
                child.name = previous_name
                logger.exception(
                    "Failed to restore channel interface %s from NetBox's deferred name %r to %r; skipping.",
                    child_pk,
                    cascade_name,
                    final_name,
                )
            except IntegrityError as error:
                child.name = previous_name
                if not is_name_collision(error):
                    raise
                logger.warning(
                    "Channel interface %s could not reclaim name %r after NetBox's deferred rename; skipping.",
                    child_pk,
                    final_name,
                )


def reconcile_after_parent_cascade(parent_before, parent_after, channels, db_alias):
    """Schedule restoration of the channel names NetBox's deferred parent cascade will overwrite.

    *channels* carries ``(child_pk, channel_id, final_name)`` for every channel the caller settled.
    Registration happens on the caller's open transaction so the callback runs after NetBox's own.
    """
    if parent_after == parent_before:
        return
    reconciliations = tuple(
        (child_pk, final_name, f"{parent_after}:{channel_id}")
        for child_pk, channel_id, final_name in channels
        if final_name == f"{parent_before}:{channel_id}" and final_name != f"{parent_after}:{channel_id}"
    )
    if not reconciliations:
        return
    transaction.on_commit(
        lambda: restore_deferred_channel_names(reconciliations, db_alias),
        using=db_alias,
    )
