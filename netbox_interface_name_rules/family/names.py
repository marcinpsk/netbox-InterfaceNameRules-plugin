# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared live interface-name primitives for family execution and its preview."""

import logging
from collections import Counter

from dcim.models import Interface
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError

from ..transactions import atomic_with_events, on_commit

logger = logging.getLogger(__name__)

COLLISION_REASON = "target name is already in use"
REPEATED_NAME_REASON = "the module's families want it more than once"
INTERFACE_NAME_CONSTRAINT = "dcim_interface_unique_device_name"


def name_owners(device_id, names) -> dict[str, int]:
    """Return the primary key of the interface that owns each of *names* on the device, by name."""
    return dict(Interface.objects.filter(device_id=device_id, name__in=names).values_list("name", "pk"))


def first_taken_name(target_names, owners, own_pks):
    """Return the first of *target_names* that *owners* gives to an interface outside *own_pks*, or None."""
    return next((name for name in target_names if name in owners and owners[name] not in own_pks), None)


def name_holders(device_id, names) -> dict:
    """Return the interface that has each of *names* on the device, by name, with its parent."""
    return {
        interface.name: interface
        for interface in Interface.objects.filter(device_id=device_id, name__in=names).select_related("parent")
    }


def channel_holder_refusal(holder, module_id, family_bases=frozenset()) -> str:
    """Return why a family built on module *module_id* cannot take over *holder*, or an empty string.

    *holder* is the row that has one of the family's channel names, and *family_bases* holds the
    primary key of each row another family is built on. Planning, the preview and execution all ask
    this one question (ADR 0017).
    """
    if holder.pk in family_bases:
        return "another family is built on it"
    if holder.module_id is not None and holder.module_id != module_id:
        return "it belongs to another module"
    if holder.parent_id is not None or getattr(holder, "channel_id", None) is not None:
        return f"it is already a subinterface of {getattr(holder.parent, 'name', 'another interface')}"
    if getattr(holder, "channels", None):
        return "it is a channelized parent"
    # NetBox refuses a channel ID on an interface with a cable, and the plugin never edits a cable.
    if holder.cable_id is not None:
        return "it has a cable"
    return ""


def channelized_name_refusal(parent_name, channel_names, holders, own_pks, module_id, family_bases=frozenset()):
    """Return ``(name, reason)`` for the first name a channelized family cannot take, or None.

    *holders* maps each name in use on the device to the row that has it, and a row in *own_pks* is
    the family's own. The family takes over a row with a channel name that ``channel_holder_refusal``
    allows, given *family_bases*.
    """
    holder = holders.get(parent_name)
    if holder is not None and holder.pk not in own_pks:
        return parent_name, f"{COLLISION_REASON}: {parent_name}"
    for name in channel_names:
        holder = holders.get(name)
        if holder is None or holder.pk in own_pks:
            continue
        refusal = channel_holder_refusal(holder, module_id, family_bases)
        if refusal:
            return name, f"{COLLISION_REASON}: {name} ({refusal})"
    return None


def repeated_names(families) -> frozenset:
    """Return each channel name that the channelized families on one module want more than once.

    *families* holds the channel names of each family, so a name repeated inside one family counts as
    well as a name two families share. Planning, the preview and execution refuse every family that
    wants such a name, and none of them takes over the row that has it (ADR 0017).
    """
    counts = Counter(name for channel_names in families for name in channel_names)
    return frozenset(name for name, count in counts.items() if count > 1)


def repeated_name_refusal(channel_names, repeated):
    """Return ``(name, reason)`` for the first of *channel_names* in *repeated*, or None."""
    name = next((name for name in channel_names if name in repeated), None)
    return None if name is None else (name, f"{COLLISION_REASON}: {name} ({REPEATED_NAME_REASON})")


def validate_or_block(interface, role):
    """Run NetBox's own validation on *interface*, restating a rejection as this family's reason."""
    try:
        interface.full_clean()
    except ValidationError as error:
        raise ValidationError(f"{role} {interface.name!r}: {' '.join(error.messages)}") from error


def name_is_taken(device_id, target_name, exclude_pk) -> bool:
    """Return whether an interface other than *exclude_pk* already owns *target_name* on the device."""
    return Interface.objects.filter(device_id=device_id, name=target_name).exclude(pk=exclude_pk).exists()


def is_name_collision(error: IntegrityError) -> bool:
    """Return whether PostgreSQL identified NetBox's interface-name constraint."""
    cause = error.__cause__
    diagnostics = getattr(cause, "diag", None)
    return getattr(diagnostics, "constraint_name", None) == INTERFACE_NAME_CONSTRAINT


class ChannelReconciliationError(RuntimeError):
    """The names that NetBox's parent cascade gave the channels this plugin kept could not be restored."""


def restore_deferred_channel_names(reconciliations):
    """Restore plugin-owned names that NetBox's parent cascade changed after commit.

    A database failure rolls the whole restore back and raises ``ChannelReconciliationError``, which
    names each channel and the name it kept, so that an operator can rename it back.
    """
    try:
        _restore_channel_names(reconciliations)
    except DatabaseError as error:
        kept = ", ".join(f"`{cascade_name}` to `{final_name}`" for _pk, final_name, cascade_name in reconciliations)
        raise ChannelReconciliationError(
            f"NetBox's parent cascade renamed channels that kept their names, and restoring those names failed "
            f"with {type(error).__name__}. Rename each channel back: {kept}"
        ) from error


def _restore_channel_names(reconciliations):
    """Restore each kept name in one block, and leave a channel that changed since the cascade alone."""
    child_pks = [child_pk for child_pk, _final_name, _cascade_name in reconciliations]
    with atomic_with_events():
        children = (
            Interface.objects.select_for_update(of=("self",)).select_related("device").order_by("pk").in_bulk(child_pks)
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
            child.snapshot()
            try:
                with atomic_with_events():
                    child.name = final_name
                    child.full_clean()
                    child.save()
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


def reconcile_after_parent_cascade(parent_before, parent_after, channels):
    """Schedule restoration of the channel names NetBox's deferred parent cascade will overwrite.

    *channels* carries ``(child_pk, channel_id, final_name)`` for every channel the caller settled.
    The callback runs after the open transactions commit on both connections, so after NetBox's cascade.
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
    on_commit(lambda: restore_deferred_channel_names(reconciliations))
