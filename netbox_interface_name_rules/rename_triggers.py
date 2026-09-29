# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Rename triggers: read the previous state, decide, and reapply the rules after commit.

The receivers in ``signals.py`` pass every module, module bay and device save here. ``before_save``
reads the previous state; ``after_save`` compares it with the saved values and, when the save is a
rename trigger, adds the trigger to the reapply plan of the transaction. Each trigger is a committed
callback, which runs only when its savepoint commits. Each trigger also appends a runner of the plan
that no savepoint rollback drops, and only the newest runner runs the plan, after every trigger. So
the plan acts on the triggers that committed, and it moves no committed callback. The plan reapplies
each module and each device at most once, from the earliest previous state of the transaction, so
it acts on the net change. A reapply that leaves an interface unrenamed, or fails, writes one journal
entry. A reapply that cannot read the committed rows is logged only.

A save that moves a module, or that changes what the names in an occupied bay are built from, also
reads before the save what named the interfaces of that module and of every module nested in it.
After the save that naming is gone. A save that changes a module's type reads it too when a rule is
scoped to the old or the new type as a parent module type, because a nested module can then select
another rule. The plan recognises the earlier names of each module from the first naming read for it
in the transaction, at the virtual-chassis position that its device had then. It also recognises the
raw names that NetBox gave at each naming point of the transaction: an install and, on NetBox 4.7,
each move that carried the module; see ``_naming_points``. A module inside the subtree of another
moved, edited or retyped module reports in the journal entry of the outermost one. The plan reapplies
the modules of its module triggers before the devices, and a device reapply leaves out each module
that already reapplied.
"""

import dataclasses
import functools
import logging
import math
import weakref
from typing import TYPE_CHECKING, NamedTuple

from django.db import transaction
from netbox.context import current_request

from .naming import bay_naming_values, chassis_position
from .rename_outcomes import OutcomeKind, RenameOutcome, renamed_count
from .rule_selection import parent_type_scopes_a_rule

if TYPE_CHECKING:
    from .engine import ModuleNaming

logger = logging.getLogger("netbox_interface_name_rules")


@dataclasses.dataclass(frozen=True)
class ModuleState:
    """The module values a rename trigger compares."""

    module_type_id: int
    module_bay_id: int
    device_id: int

    def placement(self):
        """Return the bay and the device that hold the module."""
        return self.module_bay_id, self.device_id

    def retyped_from(self, earlier):
        """Return whether the module type differs from the module type of the *earlier* state."""
        return self.module_type_id != earlier.module_type_id


@dataclasses.dataclass(frozen=True)
class DeviceState:
    """The device values a rename trigger compares."""

    virtual_chassis_id: int | None
    vc_position: int | None


@dataclasses.dataclass(frozen=True)
class BayState:
    """The module bay values a rename trigger compares, and the module installed in the bay.

    ``naming_values`` are the bay's ``bay_naming_values``. ``module_pk`` and ``module_state`` are None
    for an empty bay.
    """

    naming_values: tuple[str, str]
    module_pk: int | None
    module_state: ModuleState | None


def _bay_values(bay):
    """Return the ``bay_naming_values`` of *bay*."""
    return bay_naming_values(bay.position, bay.name)


def _state_of(state_class, instance):
    """Return the *state_class* values that *instance* holds."""
    return state_class(**{field.name: getattr(instance, field.name) for field in dataclasses.fields(state_class)})


@dataclasses.dataclass(eq=False)
class _Trigger:
    """A committed callback that records that its trigger's savepoint committed."""

    kept: bool = dataclasses.field(default=False, init=False)
    author: object = dataclasses.field(default=None, init=False)

    def __call__(self):
        """Mark the trigger as committed: Django drops this callback when its savepoint rolls back."""
        self.kept = True


@dataclasses.dataclass(eq=False)
class ModuleTrigger(_Trigger):
    """A save after which the names of one module may be wrong.

    ``baseline`` is the module's state before the save, or the state it was installed with when
    ``installed`` is set. ``naming`` is the ``ModuleNaming`` of the module and of each module nested in
    it, read by a save that moved the module, edited its bay or changed its type while a rule is scoped
    to the old or the new type as a parent module type; it is empty otherwise. ``moved`` is set when the
    save moved the module, and with it every module that ``naming`` holds.
    """

    pk: int
    baseline: ModuleState
    installed: bool = False
    naming: tuple = ()
    moved: bool = False

    @classmethod
    def after_install(cls, module):
        """Return the trigger of a module installed by this save."""
        return cls(module.pk, _state_of(ModuleState, module), installed=True)


@dataclasses.dataclass(eq=False)
class DeviceTrigger(_Trigger):
    """A save after which the names on one device may be wrong; ``baseline`` is its state before the save."""

    pk: int
    baseline: DeviceState


@dataclasses.dataclass(eq=False)
class ReapplyPlan:
    """What the rename triggers of one transaction ask for, in save order, and the newest runner."""

    triggers: list = dataclasses.field(default_factory=list)
    runner: "PlanRunner | None" = None
    started: bool = dataclasses.field(default=False, init=False)  # captureOnCommitCallbacks keeps run callbacks


@dataclasses.dataclass(eq=False)
class PlanRunner:
    """A committed callback of a reapply plan; only the newest runner of the plan runs it."""

    plan: ReapplyPlan

    def __call__(self):
        """Reapply the rules for the triggers whose savepoints committed, when this is the newest runner."""
        if self.plan.runner is not self or self.plan.started:
            return
        self.plan.started = True
        reapply([trigger for trigger in self.plan.triggers if trigger.kept])


def reapply(triggers):
    """Reapply the rules once for what *triggers*, in save order, ask for.

    The modules that the module triggers reach are reapplied first. Then each device is reapplied from
    its first trigger, in the order of those triggers, without the modules that the module reapply
    attempted. The place of a trigger is its index in *triggers*.
    """
    module_triggers = []
    device_triggers = {}
    for place, trigger in enumerate(triggers):
        if isinstance(trigger, DeviceTrigger):
            device_triggers.setdefault(trigger.pk, []).append((place, trigger))
        else:
            module_triggers.append((place, trigger))
    attempted_pks = _reapply_modules(module_triggers, device_triggers) if module_triggers else frozenset()
    for pairs in device_triggers.values():
        _, first = pairs[0]
        _reapply_device(first, attempted_pks)


@dataclasses.dataclass
class _ModuleRoot:
    """What the triggers of one module ask for.

    ``baseline`` and ``author`` come from its first trigger, and ``start`` is that trigger's place.
    ``members`` are the modules that its triggers read the naming of, in the order read.
    """

    baseline: ModuleState
    installed: bool
    author: object
    start: int
    members: dict = dataclasses.field(default_factory=dict)


def _module_roots(module_triggers):
    """Return the ``_ModuleRoot`` of each module that the ``(place, trigger)`` *module_triggers* name, in order."""
    roots = {}
    for place, trigger in module_triggers:
        root = roots.get(trigger.pk)
        if root is None or trigger.installed:
            # An install starts over: an earlier trigger with this key was for a module deleted since.
            root = roots[trigger.pk] = _ModuleRoot(trigger.baseline, trigger.installed, trigger.author, place)
        root.members.update(dict.fromkeys(entry.module_pk for entry in trigger.naming))
    return roots


def _earliest_naming(module_triggers, roots, device_triggers):
    """Return the first ``ModuleNaming`` that the ``(place, trigger)`` *module_triggers* read for each module.

    The entry of a module installed in the transaction is ``raw_only``, and an entry read before that
    install was for a module deleted since. An entry takes the virtual-chassis position that its device
    had when the module's interfaces got their names: before the transaction, or at the install of a
    module installed in it. *device_triggers* hold the ``(place, trigger)`` pairs of each device.
    """
    entries = {}
    for place, trigger in module_triggers:
        for entry in trigger.naming:
            root = roots.get(entry.module_pk)
            installed = root is not None and root.installed
            if entry.module_pk in entries or (installed and place < root.start):
                continue
            named_at = root.start if installed else -1  # -1 is before the first trigger of the transaction
            state = _state_when_named(device_triggers.get(entry.device_pk, ()), named_at, place)
            positioned = entry if state is None else entry.at_chassis_position(chassis_position(state))
            entries[entry.module_pk] = dataclasses.replace(positioned, raw_only=True) if installed else positioned
    return entries


def _state_when_named(pairs, named_at, read_at):
    """Return the device state at place *named_at* when a device trigger before *read_at* changed it, else None.

    *pairs* are the ``(place, trigger)`` pairs of the device, in save order. Nothing renamed the
    interfaces between *named_at* and *read_at*, so their names still come from that state.
    """
    return next((trigger.baseline for place, trigger in pairs if named_at < place < read_at), None)


def _moved_or_bay_changed(root, entry, module):
    """Return whether *module* moved from the baseline of *root*, or its bay values differ from *entry*."""
    if entry is None:
        return False
    moved = root is not None and _state_of(ModuleState, module).placement() != root.baseline.placement()
    return moved or _bay_values(module.module_bay) != entry.bay_values


def _journal_owner(pk, covering, roots):
    """Return the module whose journal entry reports module *pk*.

    That is the first of *covering*, the moved, edited or retyped modules whose naming read *pk*, that
    no other of them read, or *pk* itself when *covering* is empty.
    """
    return next(
        (
            owner
            for owner in covering
            if not any(other != owner and owner in roots[other].members for other in covering)
        ),
        covering[0] if covering else pk,
    )


def _reapply_from_naming(module, entry, covering, changed):
    """Return whether *module* reapplies from its naming *entry*; the rule comparison reads the rule cache.

    It does when *entry* is ``raw_only``: the raw names were resolved at the install, so the naming
    recognises them. It does when the module, or a module of *covering*, is in *changed*. It does when a
    module of *covering* changed type and the module now selects another rule than *entry*.
    """
    if entry.raw_only or module.pk in changed or not changed.isdisjoint(covering):
        return True
    return bool(covering) and entry.selects_another_rule(module)


def _reapply_options(module, root, entry, covering, changed, device_triggers, reads):
    """Return the ``module_rule_outcomes`` options that reapply *module*, or None when it needs no reapply.

    *covering* are the moved, edited or retyped modules whose naming read the module, and *changed* are
    the modules that moved or had their bay values changed. *device_triggers* and *reads* give the
    naming points of the module; see ``_naming_points``.
    """
    current = _state_of(ModuleState, module)
    if root is not None and current.retyped_from(root.baseline):
        # The module's earlier names came from another module type, so it reapplies as a type change.
        return {"force_reapply": True}
    naming_points = _naming_points(module, root, reads, device_triggers)
    if entry is not None and _reapply_from_naming(module, entry, covering, changed):
        return {"naming": entry, "naming_points": naming_points}
    if root is not None and (root.installed or current != root.baseline):
        return {"force_reapply": current != root.baseline, "naming_points": naming_points}
    return None


def _naming_points(module, root, reads, device_triggers):
    """Return each point in the transaction at which NetBox gave *module*'s templates raw names.

    Its install names them, and on NetBox 4.7 so does each move that carried it. *reads* are the
    ``_NamingRead`` of each trigger that read the module's naming, in save order. A point
    names the bay chain that the next read holds, or the committed one after the last read, at the
    position that the device had at that point. A read before the install of the module was for a
    module deleted since.
    """
    from .engine import NamingPoint
    from .family import supports_module_moves

    start = root.start if root is not None and root.installed else None
    after = [read for read in reads if start is None or read.place > start]
    places = [] if start is None else [(start, False)]
    if supports_module_moves():  # pragma: no cover - requires a NetBox that renames moved components
        places += [(read.place, True) for read in after if read.moved]
    points = []
    for named_at, move in places:
        chain = next((read.naming for read in after if read.place > named_at), None)
        device_pk = module.device_id if chain is None else chain.device_pk
        state = _state_when_named(device_triggers.get(device_pk, ()), named_at, math.inf)
        if state is not None:
            position = chassis_position(state)
        else:
            position = chassis_position(module.device) if chain is None else chain.vc_position
        points.append(NamingPoint(chain, position, move))
    return tuple(points)


def _may_keep_a_name_of_a_move(reads, device_triggers):  # pragma: no cover - requires a NetBox that moves components
    """Return whether a module can keep a raw name that a move gave it, also when it is back where it was.

    NetBox renames a raw name back at a later move only when nothing that the name is built from changed
    since the earlier move: the position of a device that the module was on, and its bay chain, which
    a read that is not a move shows. *reads* are the module's ``_NamingRead``.
    """
    moves = [read.place for read in reads if read.moved]
    if len(moves) < 2:
        return False
    first, last = moves[0], moves[-1]
    if any(first < read.place < last and not read.moved for read in reads):
        return True
    devices = {read.naming.device_pk for read in reads}
    return any(first < place < last for pk in devices for place, _ in device_triggers.get(pk, ()))


class _NamingRead(NamedTuple):
    """A trigger's read of one module's naming, at the *place* of the trigger in save order."""

    place: int
    naming: "ModuleNaming"
    moved: bool


def _reads(module_triggers):
    """Return the ``_NamingRead`` of each trigger that read a module's naming, by module."""
    reads = {}
    for place, trigger in module_triggers:
        for naming in trigger.naming:
            reads.setdefault(naming.module_pk, []).append(_NamingRead(place, naming, trigger.moved))
    return reads


def _reapply_module(module, decide, outcomes):
    """Reapply *module* with the options that *decide* returns, and add its outcome facts to *outcomes*.

    *decide* returns None when the module needs no reapply. It can read the rules, so a failure to
    decide is one more fact, as a failure to reapply is. Return True when the module reapplied or
    failed to, and False when it needs no reapply.
    """
    from .engine import module_rule_outcomes

    renamed_before = renamed_count(outcomes)
    try:
        options = decide()
        if options is None:
            return False
        # extend() keeps the facts the generator yielded before a later family raised.
        outcomes.extend(module_rule_outcomes(module, module.module_bay, **options))
    except Exception as error:
        logger.exception(
            "Failed to apply interface name rules for %s in %s", module.module_type, module.module_bay.name
        )
        outcomes.append(_failure(error))
    renamed = renamed_count(outcomes) - renamed_before
    if renamed:
        logger.info("Renamed %d interface(s) for %s in %s", renamed, module.module_type, module.module_bay.name)
    return True


def _reapply_modules(module_triggers, device_triggers):
    """Reapply each module that the ``(place, trigger)`` *module_triggers* reach once, and report each owner once.

    A module reapplies as after a type change when its type changed. It reapplies from its earliest
    naming when it or a module whose naming read it moved or had its bay edited, when a module whose
    naming read it changed type and it now selects another rule, or when it was installed in the
    transaction and a naming was read for it. It reapplies as an install otherwise, which also
    recognises its raw names at the position of its install. Its outcomes go to the outermost of the
    moved, edited or retyped modules that read its naming, or to the module itself. *device_triggers*
    give each naming and each install the position of its device when the module was named. Return the
    primary key of each module that reapplied, or that failed to.
    """
    from .engine import committed_modules, pinned_reapply
    from .family import supports_module_moves

    roots = _module_roots(module_triggers)
    entries = _earliest_naming(module_triggers, roots, device_triggers)
    try:
        modules = committed_modules({*roots, *entries})
    except Exception:
        # No committed row was read, so no object can carry a journal entry.
        logger.exception("Failed to read modules %s for their rename trigger reapply", sorted(roots))
        return frozenset()
    present = [pk for pk in roots if pk in modules]
    reads = _reads(module_triggers)
    moves_rename = supports_module_moves()
    changed = {
        pk
        for pk in present
        if _moved_or_bay_changed(roots[pk], entries.get(pk), modules[pk])
        or (moves_rename and _may_keep_a_name_of_a_move(reads.get(pk, ()), device_triggers))
    }
    retyped = {pk for pk in present if _state_of(ModuleState, modules[pk]).retyped_from(roots[pk].baseline)}
    covers = [pk for pk in present if pk in changed or pk in retyped]
    walked = dict.fromkeys(pk for pk in roots for pk in (pk, *(roots[pk].members if pk in covers else ())))
    outcomes_by_owner = {}
    attempted_pks = set()
    with pinned_reapply(modules.values()):
        for pk in walked:
            module = modules.get(pk)
            if module is None:
                continue
            covering = [other for other in covers if other != pk and pk in roots[other].members]
            decide = functools.partial(
                _reapply_options,
                module,
                roots.get(pk),
                entries.get(pk),
                covering,
                changed,
                device_triggers,
                reads.get(pk, ()),
            )
            outcomes = outcomes_by_owner.setdefault(_journal_owner(pk, covering, roots), [])
            if _reapply_module(module, decide, outcomes):
                attempted_pks.add(pk)
    for owner, outcomes in outcomes_by_owner.items():
        _report(modules[owner], outcomes, roots[owner].author)
    return frozenset(attempted_pks)


def _reapply_device(trigger, attempted_pks):
    """Reapply the module and device-interface rules of the device of its first *trigger*, once.

    The modules in *attempted_pks* are left out: the module reapply reapplied them, reading the device
    after commit too, or reported that it failed to.
    """
    from dcim.models import Device

    pk, baseline, author = trigger.pk, trigger.baseline, trigger.author
    try:
        device = Device.objects.select_related("virtual_chassis").get(pk=pk)
    except Device.DoesNotExist:
        return
    except Exception:
        # No committed row was read, so no object can carry a journal entry.
        logger.exception("Failed to read device %s for its rename trigger reapply", pk)
        return
    current = _state_of(DeviceState, device)
    if current == baseline:
        return
    # Off a chassis or without a position, what the interfaces are called is the operator's decision.
    report_only = current.virtual_chassis_id is None or current.vc_position is None
    outcomes = []
    try:
        from .engine import NamingPoint, device_module_rule_outcomes

        # The module names on the device come from its position before its first trigger.
        before = (NamingPoint(None, chassis_position(baseline), move=False),)
        # extend() keeps the facts the generator yielded before a later module raised.
        outcomes.extend(
            device_module_rule_outcomes(
                device, report_only=report_only, excluded_pks=attempted_pks, naming_points=before
            )
        )
    except Exception as error:
        logger.exception("Failed to re-apply module rules for device %s after VC change", pk)
        outcomes.append(_failure(error))
    try:
        from .engine import device_interface_rule_outcomes

        outcomes.extend(device_interface_rule_outcomes(device, report_only=report_only))
    except Exception as error:
        logger.exception("Failed to re-apply device interface rules for device %s after VC change", pk)
        outcomes.append(_failure(error))
    total = renamed_count(outcomes)
    if total:
        logger.info("Re-renamed %d interface(s) for device %s after VC change", total, device)
    _report(device, outcomes, author)


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


def _module_trigger(module, created, previous):
    """Return the trigger a module save is, or None when the save is not a rename trigger.

    *previous* is ``(state, naming)`` from ``_read_module``, or None without a previous state.
    """
    if created:
        return ModuleTrigger.after_install(module)
    state, naming = previous or (None, ())
    if state is None:
        return None
    current = _state_of(ModuleState, module)
    if current == state:
        return None
    logger.debug("Module %s changed from %s to %s; adding it to the reapply plan", module.pk, state, current)
    return ModuleTrigger(module.pk, state, naming=naming, moved=current.placement() != state.placement())


def _device_trigger(device, created, previous):
    """Return the trigger a device save is, or None when the save is not a rename trigger."""
    if created or previous is None:
        return None
    if _state_of(DeviceState, device) == previous:
        return None
    return DeviceTrigger(device.pk, previous)


def _bay_trigger(bay, created, previous):
    """Return the trigger of the module in *bay* that a bay save is, or None when it is not a rename trigger."""
    state, naming = previous or (None, ())
    if created or state is None or state.module_pk is None or _bay_values(bay) == state.naming_values:
        return None
    logger.debug("Module bay %s changed from %s; adding module %s to the reapply plan", bay.pk, state, state.module_pk)
    return ModuleTrigger(state.module_pk, state.module_state, naming=naming)


def _read_state(model, state_class, pk):
    """Return the *state_class* values the database holds for row *pk*, or None without a row."""
    row = model.objects.filter(pk=pk).values(*(field.name for field in dataclasses.fields(state_class))).first()
    return None if row is None else state_class(**row)


def _needs_subtree_naming(state, current):
    """Return whether a module save from *state* to *current* needs the naming of the module's subtree.

    A move needs it. A type change needs it when a rule is scoped to the old or the new type as a parent
    module type: only then can a module nested in the module select another rule.
    """
    if current.placement() != state.placement():
        return True
    return current.retyped_from(state) and parent_type_scopes_a_rule((state.module_type_id, current.module_type_id))


def _read_module(module):
    """Return ``(state, naming)``: the module's previous state, and the naming of its subtree if the save needs it."""
    state = _read_state(type(module), ModuleState, module.pk)
    if state is None or not _needs_subtree_naming(state, _state_of(ModuleState, module)):
        return state, ()
    from .engine import read_subtree_naming

    return state, read_subtree_naming(module.pk)


def _read_device(device):
    """Return the device's previous state."""
    return _read_state(type(device), DeviceState, device.pk)


def _read_bay(bay):
    """Return ``(state, naming)``: the bay's previous state, and its module's subtree naming if the save changes it."""
    row = (
        type(bay)
        .objects.filter(pk=bay.pk)
        .values("position", "name", "installed_module", "installed_module__module_type", "installed_module__device")
        .first()
    )
    if row is None:
        return None
    module_pk = row["installed_module"]
    module_state = (
        None
        if module_pk is None
        else ModuleState(
            module_type_id=row["installed_module__module_type"],
            module_bay_id=bay.pk,
            device_id=row["installed_module__device"],
        )
    )
    state = BayState(bay_naming_values(row["position"], row["name"]), module_pk, module_state)
    if module_pk is None or _bay_values(bay) == state.naming_values:
        return state, ()
    from .engine import read_subtree_naming

    return state, read_subtree_naming(module_pk)


_TRIGGERS = {
    "dcim.Module": (_read_module, _module_trigger),
    "dcim.ModuleBay": (_read_bay, _bay_trigger),
    "dcim.Device": (_read_device, _device_trigger),
}

# Keyed by id(): model equality follows the primary key, so two instances of one row would collide.
_previous_states = {}


def before_save(sender, instance):
    """Read the previous state of *instance* and hold it for its post_save. A read error propagates."""
    read, _ = _TRIGGERS[sender._meta.label]
    previous = None if instance.pk is None else read(instance)
    key = id(instance)

    def forget(reference):
        # A save that failed before post_save leaves its entry until the instance is collected.
        if _previous_states.get(key, (None,))[0] is reference:
            _previous_states.pop(key, None)

    _previous_states[key] = (weakref.ref(instance, forget), previous)


def after_save(sender, instance, created):
    """Add the save of *instance* to the reapply plan of the transaction when it is a rename trigger."""
    _, trigger_of = _TRIGGERS[sender._meta.label]
    # No entry: NetBox sent this post_save by hand, without a model save.
    _, previous = _previous_states.pop(id(instance), (None, None))
    trigger = trigger_of(instance, created, previous)
    if trigger is None:
        return
    trigger.author = _request_user()
    connection = transaction.get_connection()
    entries = connection.run_on_commit if connection.in_atomic_block else ()
    plan = (
        next(
            (
                callback.plan
                for _, callback, _ in entries
                if isinstance(callback, PlanRunner) and not callback.plan.started
            ),
            None,
        )
        or ReapplyPlan()
    )
    plan.triggers.append(trigger)
    # Django drops the callbacks of a rolled-back savepoint from run_on_commit, so a dropped trigger is not kept.
    transaction.on_commit(trigger)
    plan.runner = PlanRunner(plan)
    if connection.in_atomic_block:
        # Without a savepoint tag no rollback but the transaction's drops the runner; it runs after every trigger.
        connection.run_on_commit.append((set(), plan.runner, False))
    else:
        plan.runner()
