# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Every short sequence of module and chassis changes in one transaction ends named, or reported once.

One module of each template shape and rule goes through each sequence of up to three operations in
one transaction. After commit an oracle that reads only the final state checks each interface: it has
the name that its rule gives now, or one journal line reports it. No module is reapplied twice. The
stricter check asks for the name, except for the cases that ``_expected_report`` lists with a reason.
Each first and second operation has a test class of its own, so the sequences run on several test
workers.
"""

import collections
import itertools
import re
from unittest import skipUnless

from dcim.models import Device, Interface, InterfaceTemplate, Module, ModuleBay
from django.db import transaction
from extras.models import JournalEntry

from netbox_interface_name_rules.engine import supports_vc_position_token
from netbox_interface_name_rules.family import supports_module_moves
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.naming import bay_naming_values, chassis_position
from netbox_interface_name_rules.tests.test_module_move_trigger import (
    PLAIN_TYPE,
    ModuleMoveTestCase,
    _module_reapplies,
)
from netbox_interface_name_rules.tests.test_vc_drift import REQUIRES_VC_POSITION_TOKEN

# The template name after the module's code, or None for a module type without interface templates.
SHAPES = {
    "token": "-{vc_position}/{module}",
    "adjacent": "-{vc_position}{vc_position}/{module}",
    "bay token": "-{module}",
    "fallback": "-{vc_position:{module}}",
    "no templates": None,
}
RULES = {"plain": "et-{vc_position}/{bay_position}", "base": "p{base}"}
OPERATIONS = ("move", "other device", "chassis", "bay edit", "move back", "move to the original bay")
MOVES = ("move", "other device", "move back", "move to the original bay")
LINE = re.compile(r"^- `([^`]*)`", re.MULTILINE)
FIRST_OPERATIONS = ("install", *OPERATIONS)
ORIGIN = (0, 0)


def _locations(sequence):
    """Return the ``(device, slot)`` of the modules after each operation of *sequence*, or None when one is not valid.

    A move goes to a new slot, "move back" returns to the location before the last move, and "move to
    the original bay" is valid only where it differs from "move back".
    """
    location, history, fresh, locations = ORIGIN, [], 0, []
    for operation in sequence:
        if operation in ("move", "other device"):
            fresh += 1
            history.append(location)
            location = (location[0] if operation == "move" else 1 - location[0], fresh)
        elif operation == "move back":
            if not history:
                return None
            location = history.pop()
        elif operation == "move to the original bay":
            if location == ORIGIN or (history and history[-1] == ORIGIN):
                return None
            history.append(location)
            location = ORIGIN
        locations.append(location)
    return locations


def _sequences(prefix):
    """Return every valid sequence of one operation for an empty *prefix*, else *prefix* and each longer by one."""
    if not prefix:
        sequences = [(first,) for first in FIRST_OPERATIONS]
    else:
        sequences = [prefix, *((*prefix, last) for last in OPERATIONS)]
    if not supports_module_moves():
        sequences = [sequence for sequence in sequences if "other device" not in sequence]
    return [sequence for sequence in sequences if _locations(sequence) is not None]


def _untouched(sequence):
    """Return whether *sequence* leaves a module installed before it where it was, with nothing it reads changed."""
    return (
        sequence[0] != "install" and not {"chassis", "bay edit"} & set(sequence) and _locations(sequence)[-1] == ORIGIN
    )


def _expected_report(shape, sequence):
    """Return why the design reports this case instead of renaming it, or None when it renames it."""
    if shape == "no templates" and {*MOVES, "bay edit"} & set(sequence) and not _untouched(sequence):
        return "a module type without templates claims nothing after a move or a bay edit (ADR 0015)"
    return None


class _SequenceTestCase(ModuleMoveTestCase):
    """One module of each shape and rule, installed without a rule, and slots for them on both devices."""

    @classmethod
    def setUpTestData(cls):
        # A short prefix: a generated class name makes some fixture names longer than their field allows.
        cls.build("Sequence")
        cls.subjects = []
        for index, (shape, rule) in enumerate(itertools.product(SHAPES, RULES)):
            templates = () if SHAPES[shape] is None else (f"s{index}{SHAPES[shape]}",)
            module_type = cls._module_type(f"{shape} {rule}", *templates)
            rule_row = InterfaceNameRule.objects.create(
                module_type=module_type, name_template=RULES[rule], enabled=False
            )
            numbers = [10 * (index + 1) + slot for slot in range(4)]
            for device in (cls.device, cls.peer):
                for number in numbers:
                    position = "{vc_position}" if shape == "bay token" else str(number)
                    ModuleBay.objects.create(device=device, name=f"Slot {number}", position=position)
            module = cls._create_subject(shape, module_type, cls.device, numbers[0])
            cls.subjects.append((shape, rule, module_type, rule_row, numbers, module.pk))

    @classmethod
    def _create_subject(cls, shape, module_type, device, number):
        """Create a module of *module_type* in slot *number* of *device*; the committed callbacks do not run."""
        bay = ModuleBay.objects.get(device=device, name=f"Slot {number}")
        module = Module.objects.create(device=device, module_bay=bay, module_type=module_type)
        if shape == "no templates":
            Interface.objects.create(device=device, module=module, name=str(number), type=PLAIN_TYPE)
        return module


class _SequenceChecks:
    """The check of every sequence of ``_sequences(PREFIX)``."""

    PREFIX = ()

    def test_every_sequence_ends_named_or_reported_once(self):
        for sequence in _sequences(self.PREFIX):
            with self.subTest(sequence=sequence), transaction.atomic():
                try:
                    self._check(sequence)
                finally:
                    transaction.set_rollback(True)

    def _check(self, sequence):
        devices = [Device.objects.get(pk=self.device.pk), Device.objects.get(pk=self.peer.pk)]
        installs = sequence[0] == "install"
        locations = iter(_locations(sequence))
        modules = [Module.objects.get(pk=subject[5]) for subject in self.subjects]
        before = [Interface.objects.filter(module=module).values_list("name", flat=True).get() for module in modules]
        if installs:
            # The install takes the slot of the module installed before, whose name a second one would repeat.
            for module in modules:
                module.delete()
            modules = []
        InterfaceNameRule.objects.filter(pk__in=[subject[3].pk for subject in self.subjects]).update(enabled=True)
        marker = JournalEntry.objects.order_by("-pk").values_list("pk", flat=True).first() or 0

        def install():
            advance()
            for shape, _rule, module_type, _rule_row, numbers, _pk in self.subjects:
                modules.append(self._create_subject(shape, module_type, devices[0], numbers[0]))

        current = {"location": ORIGIN}

        def advance():
            current["location"] = next(locations)

        def move():
            advance()
            device, slot = current["location"]
            for module, subject in zip(modules, self.subjects, strict=True):
                bay = ModuleBay.objects.get(device=devices[device], name=f"Slot {subject[4][slot]}")
                self._save_move(module, bay)

        def change_the_chassis_position():
            advance()
            device = devices[current["location"][0]]
            device.vc_position += 2
            device.save()

        def edit_the_bays():
            advance()
            for module in modules:
                bay = ModuleBay.objects.get(pk=module.module_bay_id)
                bay.position = str(500 + int(bay.name.rsplit(" ", 1)[1]))
                bay.save()

        saves = {
            "install": install,
            "chassis": change_the_chassis_position,
            "bay edit": edit_the_bays,
            **dict.fromkeys(MOVES, move),
        }
        with _module_reapplies() as spy:
            self._save_in_one_transaction(*(saves[op] for op in sequence))

        reapplies = collections.Counter(call.args[0].pk for call in spy.call_args_list)
        self.assertEqual([pk for pk, count in reapplies.items() if count > 1], [])
        lines = collections.Counter(
            name for entry in JournalEntry.objects.filter(pk__gt=marker) for name in LINE.findall(entry.comments)
        )
        for index, (module, subject) in enumerate(zip(modules, self.subjects, strict=True)):
            shape, rule, module_type = subject[:3]
            (name,) = Interface.objects.filter(module=module).values_list("name", flat=True)
            expected = self._expected_name(module.pk, module_type, rule)
            with self.subTest(shape=shape, rule=rule):
                # A module that the sequence leaves untouched keeps the name it had, which no rule gave yet.
                if name == expected or (_untouched(sequence) and name == before[index]):
                    self.assertEqual(lines[name], 0)
                    continue
                self.assertEqual(lines[name], 1, f"{name!r} is neither {expected!r} nor reported once")
                self.assertIsNotNone(_expected_report(shape, sequence), f"{name!r} is reported, not {expected!r}")

    @staticmethod
    def _expected_name(pk, module_type, rule):
        """Return the name that the rule gives the module's interface in the committed state."""
        module = Module.objects.select_related("device", "module_bay").get(pk=pk)
        bay = module.module_bay
        bay_position = bay_naming_values(bay.position, bay.name)[1]
        if rule == "plain":
            return f"et-{chassis_position(module.device)}/{bay_position}"
        template = InterfaceTemplate.objects.filter(module_type=module_type).first()
        return "p" + (bay_position if template is None else template.resolve_name(module))


def _sequence_test(prefix):
    """Return the test class of the sequences of *prefix*, named after its operations."""
    words = "".join(word.title() for operation in prefix for word in operation.split()) or "SingleOperation"
    name = f"{words}SequenceTest"
    doc = (
        f"Every sequence of up to three operations that starts with {', '.join(prefix)}."
        if prefix
        else ("Every sequence of one operation.")
    )
    attributes = {"PREFIX": prefix, "__doc__": doc, "__module__": __name__, "__qualname__": name}
    return skipUnless(supports_vc_position_token(), REQUIRES_VC_POSITION_TOKEN)(
        type(name, (_SequenceChecks, _SequenceTestCase), attributes)
    )


for _test in map(_sequence_test, [(), *itertools.product(FIRST_OPERATIONS, OPERATIONS)]):
    if _sequences(_test.PREFIX):
        globals()[_test.__name__] = _test
del _test
