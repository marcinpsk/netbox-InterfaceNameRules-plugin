# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Rename triggers: read the previous state, decide, and reapply the rules after commit.

The receivers in ``signals.py`` pass every module and device save here. ``before_save`` reads the
previous state; ``after_save`` compares it with the saved values and schedules one reapply per
module or device per transaction. The reapply compares the earliest previous state of the
transaction with the committed row, so it acts on the net change. A reapply that leaves an
interface unrenamed, or fails, writes one journal entry on the module or device. A reapply that
cannot read the committed row is logged only.

A save that moves a module also reads, before the save, what named the interfaces of the module and
of every module nested in it. The reapply renames that subtree and recognises the earlier names
from it, because NetBox can change the nested bays in the same save.
"""

import dataclasses
import logging
import weakref

from django.db import transaction
from netbox.context import current_request

from .rename_outcomes import OutcomeKind, RenameOutcome, renamed_count

logger = logging.getLogger("netbox_interface_name_rules")


@dataclasses.dataclass(frozen=True)
class ModuleState:
    """The module values a rename trigger compares.

    *naming* is not compared. Only a save that moves the module reads it: the ``PreviousNaming`` of
    the module and of every module nested in it, the moved module first.
    """

    module_type_id: int
    module_bay_id: int
    device_id: int
    naming: tuple = dataclasses.field(default=(), compare=False, repr=False)

    def placement(self):
        """Return the bay and the device that hold the module."""
        return self.module_bay_id, self.device_id


@dataclasses.dataclass(frozen=True)
class DeviceState:
    """The device values a rename trigger compares."""

    virtual_chassis_id: int | None
    vc_position: int | None


def _compared_fields(state_class):
    """Return the names of the fields a rename trigger compares."""
    return [field.name for field in dataclasses.fields(state_class) if field.compare]


def _state_of(state_class, instance):
    """Return the *state_class* values that *instance* holds."""
    return state_class(**{name: getattr(instance, name) for name in _compared_fields(state_class)})


@dataclasses.dataclass(eq=False)
class ModuleReapply:
    """Reapply the rules to one module after commit.

    ``baseline`` is the earliest previous state of the transaction, or the state the module was
    installed with when ``installed`` is set. After a move it holds the naming of the moved subtree.
    """

    pk: int
    baseline: ModuleState
    installed: bool
    author: object = dataclasses.field(default=None, init=False)
    started: bool = dataclasses.field(default=False, init=False)  # captureOnCommitCallbacks keeps run callbacks

    @classmethod
    def after_install(cls, module):
        """Return the reapply for a module installed by this save."""
        return cls(module.pk, _state_of(ModuleState, module), installed=True)

    def covers(self, pending):
        """Return whether *pending*, not yet run, already reapplies what this trigger asks for."""
        # An install is never covered: a pending reapply compares with the row this install replaced.
        return isinstance(pending, ModuleReapply) and pending.pk == self.pk and not (pending.started or self.installed)

    def absorb(self, later):
        """Keep the earliest previous state, and take the naming of a *later* move when no earlier save read one."""
        if later.baseline.naming and not self.baseline.naming:
            self.baseline = dataclasses.replace(self.baseline, naming=later.baseline.naming)

    def _outcomes(self, module, current):
        """Yield the outcome facts of the reapply that the change from the baseline to *current* asks for."""
        from .engine import module_rule_outcomes, moved_module_rule_outcomes

        naming = self.baseline.naming
        if not naming or current.placement() == self.baseline.placement():
            yield from module_rule_outcomes(module, module.module_bay, force_reapply=current != self.baseline)
            return
        if current.module_type_id != self.baseline.module_type_id:
            # The module's earlier names came from another module type, so only its nested modules use them.
            yield from module_rule_outcomes(module, module.module_bay, force_reapply=True)
            naming = naming[1:]
        yield from moved_module_rule_outcomes(naming)

    def __call__(self):
        """Reapply the module's rule against the committed row."""
        self.started = True
        from dcim.models import Module

        try:
            module = Module.objects.select_related("module_bay", "module_type").get(pk=self.pk)
        except Module.DoesNotExist:
            return
        except Exception:
            # No committed row was read, so no object can carry a journal entry.
            logger.exception("Failed to read module %s for its rename trigger reapply", self.pk)
            return
        module_bay = module.module_bay
        current = _state_of(ModuleState, module)
        if current == self.baseline and not self.installed:
            return
        outcomes = []
        try:
            # extend() keeps the facts the generator yielded before a later family raised.
            outcomes.extend(self._outcomes(module, current))
        except Exception as error:
            logger.exception("Failed to apply interface name rules for %s in %s", module.module_type, module_bay.name)
            outcomes.append(_failure(error))
        renamed = renamed_count(outcomes)
        if renamed:
            logger.info("Renamed %d interface(s) for %s in %s", renamed, module.module_type, module_bay.name)
        _report(module, outcomes, self.author)


@dataclasses.dataclass(eq=False)
class DeviceReapply:
    """Reapply the module and device-interface rules of one device after commit.

    ``baseline`` is the earliest previous state of the transaction.
    """

    pk: int
    baseline: DeviceState
    author: object = dataclasses.field(default=None, init=False)
    started: bool = dataclasses.field(default=False, init=False)  # captureOnCommitCallbacks keeps run callbacks

    def is_due(self, current):
        """Return whether *current* differs from the baseline."""
        return current != self.baseline

    def covers(self, pending):
        """Return whether *pending*, not yet run, already reapplies what this trigger asks for."""
        return isinstance(pending, DeviceReapply) and pending.pk == self.pk and not pending.started

    def absorb(self, later):
        """Keep the earliest previous state: a later device trigger adds nothing to it."""

    def __call__(self):
        """Reapply the device's rules against the committed row."""
        self.started = True
        from dcim.models import Device

        try:
            device = Device.objects.select_related("virtual_chassis").get(pk=self.pk)
        except Device.DoesNotExist:
            return
        except Exception:
            # No committed row was read, so no object can carry a journal entry.
            logger.exception("Failed to read device %s for its rename trigger reapply", self.pk)
            return
        current = _state_of(DeviceState, device)
        if not self.is_due(current):
            return
        # Off a chassis or without a position, what the interfaces are called is the operator's decision.
        report_only = current.virtual_chassis_id is None or current.vc_position is None
        outcomes = []
        try:
            from .engine import device_module_rule_outcomes

            # extend() keeps the facts the generator yielded before a later module raised.
            outcomes.extend(device_module_rule_outcomes(device, report_only=report_only))
        except Exception as error:
            logger.exception("Failed to re-apply module rules for device %s after VC change", self.pk)
            outcomes.append(_failure(error))
        try:
            from .engine import device_interface_rule_outcomes

            outcomes.extend(device_interface_rule_outcomes(device, report_only=report_only))
        except Exception as error:
            logger.exception("Failed to re-apply device interface rules for device %s after VC change", self.pk)
            outcomes.append(_failure(error))
        total = renamed_count(outcomes)
        if total:
            logger.info("Re-renamed %d interface(s) for device %s after VC change", total, device)
        _report(device, outcomes, self.author)


def _failure(error):
    """Return the outcome fact of a reapply that *error* stopped."""
    return RenameOutcome(OutcomeKind.FAILED, None, f"the reapply stopped with {type(error).__name__}: {error}")


def _journal_line(outcome):
    """Return the Markdown list item that names one outcome's interface and reason."""
    if outcome.interface_name is None:
        return f"- {outcome.reason} ({outcome.kind})"
    subject = f"`{outcome.interface_name}`"
    if outcome.target_name not in (None, outcome.interface_name):
        subject += f" to `{outcome.target_name}`"
    return f"- {subject}: {outcome.reason} ({outcome.kind})"


def _report(target, outcomes, author):
    """Write one journal entry on *target* when an outcome is a skip or a failure. A failed write is logged."""
    reported = [outcome for outcome in outcomes if outcome.kind != OutcomeKind.RENAMED]
    if not reported:
        return
    from extras.choices import JournalEntryKindChoices
    from extras.models import JournalEntry

    failed = any(outcome.kind == OutcomeKind.FAILED for outcome in reported)
    kind = JournalEntryKindChoices.KIND_DANGER if failed else JournalEntryKindChoices.KIND_WARNING
    comments = "\n".join(
        (
            "Interface Name Rules reapplied its rules after this change, with these results:",
            "",
            *(_journal_line(outcome) for outcome in reported),
        )
    )
    logger.warning("Rename trigger on %s: %d interface(s) skipped or failed; see its journal", target, len(reported))
    try:
        with transaction.atomic():
            JournalEntry.objects.create(assigned_object=target, created_by=author, kind=kind, comments=comments)
    except Exception:
        logger.exception("Failed to write the rename journal entry for %s", target)


def _request_user():
    """Return the authenticated user of the current request, or None outside a request."""
    user = getattr(current_request.get(), "user", None)
    return user if user is not None and user.is_authenticated else None


def _module_reapply(module, created, previous):
    """Return the reapply a module save asks for, or None when the save is not a rename trigger."""
    if created:
        return ModuleReapply.after_install(module)
    if previous is None:
        return None
    current = _state_of(ModuleState, module)
    if current == previous:
        return None
    logger.debug("Module %s changed from %s to %s; scheduling a reapply", module.pk, previous, current)
    return ModuleReapply(module.pk, previous, installed=False)


def _device_reapply(device, created, previous):
    """Return the reapply a device save asks for, or None when the save is not a rename trigger."""
    if created or previous is None:
        return None
    if _state_of(DeviceState, device) == previous:
        return None
    return DeviceReapply(device.pk, previous)


def _with_move_naming(module, previous):
    """Return *previous*, with the naming of the module's subtree when the save moves the module."""
    if _state_of(ModuleState, module).placement() == previous.placement():
        return previous
    from .engine import read_previous_naming

    return dataclasses.replace(previous, naming=read_previous_naming(module.pk))


def _as_read(_instance, previous):
    """Return *previous* unchanged: the compared values are the whole previous state."""
    return previous


_TRIGGERS = {
    "dcim.Module": (ModuleState, _with_move_naming, _module_reapply),
    "dcim.Device": (DeviceState, _as_read, _device_reapply),
}

# Keyed by id(): model equality follows the primary key, so two instances of one row would collide.
_previous_states = {}


def before_save(sender, instance):
    """Read the previous state of *instance* and hold it for its post_save. A read error propagates."""
    state_class, complete, _ = _TRIGGERS[sender._meta.label]
    previous = None
    if instance.pk is not None:
        row = sender.objects.filter(pk=instance.pk).values(*_compared_fields(state_class)).first()
        previous = None if row is None else complete(instance, state_class(**row))
    key = id(instance)

    def forget(reference):
        # A save that failed before post_save leaves its entry until the instance is collected.
        if _previous_states.get(key, (None,))[0] is reference:
            _previous_states.pop(key, None)

    _previous_states[key] = (weakref.ref(instance, forget), previous)


def after_save(sender, instance, created):
    """Schedule a reapply after commit when the save of *instance* is a rename trigger."""
    *_, trigger = _TRIGGERS[sender._meta.label]
    # No entry: NetBox sent this post_save by hand, without a model save.
    _, previous = _previous_states.pop(id(instance), (None, None))
    reapply = trigger(instance, created, previous)
    if reapply is None:
        return
    connection = transaction.get_connection()
    pending = None
    if connection.in_atomic_block:
        # Django drops the callbacks of a rolled-back savepoint or transaction from run_on_commit.
        pending = next((pending for _, pending, _ in connection.run_on_commit if reapply.covers(pending)), None)
    if pending is not None:
        pending.absorb(reapply)
        return
    reapply.author = _request_user()
    transaction.on_commit(reapply)
