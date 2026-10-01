# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Plugin writes started in a real netbox-branching branch land in the branch, on both connections at once.

Each test builds its rows on main first, because a branch copies main when it provisions. Requests
carry netbox-branching's cookie, so its request processor, NetBox's change log and the ChangeDiff rows
that netbox-branching writes on ``default`` run for real.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import partial
from unittest import skipUnless

from core.models import ObjectChange
from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay, VirtualChassis
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.db import connections, transaction
from django.db.models.signals import post_save, pre_save
from django.urls import reverse

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.engine import (
    apply_device_interface_rules,
    apply_rule_to_existing,
    find_matching_rule,
    supports_channelization,
)
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.rule_selection import pinned_rule_cache
from netbox_interface_name_rules.tests.helpers import (
    activate,
    interface_signal,
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
    make_placement,
    row_lock_in_another_session,
    set_lock_timeout,
)
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_branching import BranchTestCase
from netbox_interface_name_rules.tests.test_channelization import (
    CHANNEL_TYPE,
    PARENT_TYPE,
    PLAIN_TYPE,
    REQUIRES_CHANNELIZATION,
    _channelized_module_type,
)

User = get_user_model()
FLAT = BreakoutModeChoices.FLAT
CHANNELIZED = BreakoutModeChoices.CHANNELIZED


def names_of(module):
    """Return the sorted interface names of *module* on the active branch."""
    return sorted(Interface.objects.filter(module=module).values_list("name", flat=True))


def messages_of(response):
    """Return ``(level tag, text)`` for each message that *response*'s request produced."""
    return [(message.level_tag, str(message)) for message in get_messages(response.wsgi_request)]


def _create_in_its_own_session(branch, fields):
    try:
        # A lock wait on the test's own transaction would otherwise hang the test.
        set_lock_timeout(branch.connection_name, "5s")
        with activate(branch):
            return Interface.objects.create(**fields).pk
    finally:
        connections[branch.connection_name].close()


def create_in_another_session(branch, **fields):
    """Create an interface in *branch* through a session of another thread, which commits at once."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_create_in_its_own_session, branch, fields).result()


class _BranchWriteCase(BranchTestCase):
    """A superuser logged in with netbox-branching's cookie of a branch provisioned from the rows ``build`` made."""

    PREFIX = ""

    def setUp(self):
        self.user = User.objects.create_superuser(username=f"{self.PREFIX.lower()}-operator")
        self.build()
        self.branch = self.provision_branch(self.PREFIX, self.user)
        self.alias = self.branch.connection_name
        self.client.force_login(self.user)
        self.client.cookies["active_branch"] = self.branch.schema_id

    def build(self):
        """Create the rows on main that the branch copies."""
        raise NotImplementedError

    def in_branch(self):
        return activate(self.branch)

    def apply_url(self, rule):
        return reverse("plugins:netbox_interface_name_rules:interfacenamerule_apply_detail", kwargs={"pk": rule.pk})

    def change_diffs(self):
        """Return the ChangeDiff rows of the branch, which netbox-branching keeps on ``default``."""
        from netbox_branching.models import ChangeDiff

        return ChangeDiff.objects.filter(branch=self.branch)

    def branch_updates_of(self, instance):
        """Return ``(name before, name after)`` for each update record of *instance* in the branch."""
        changes = ObjectChange.objects.using(self.alias).filter(
            changed_object_type=ContentType.objects.get_for_model(instance), changed_object_id=instance.pk
        )
        return [(change.prechange_data["name"], change.postchange_data["name"]) for change in changes]


class _PlainModuleCase(_BranchWriteCase):
    """One device with one module bay, and a module whose one interface NetBox named ``0``."""

    def build(self):
        manufacturer = make_manufacturer(self.PREFIX)
        device_type = make_device_type(manufacturer, self.PREFIX)
        make_module_bay_templates(device_type, ("Bay 0",))
        self.device = make_device(self.PREFIX, device_type)
        self.device_type = device_type
        self.module_type = make_module_type(manufacturer, self.PREFIX)
        InterfaceTemplate.objects.create(module_type=self.module_type, name="{module}", type=PLAIN_TYPE)
        bay = ModuleBay.objects.get(device=self.device, name="Bay 0")
        # No rule exists yet, so the interface keeps NetBox's raw name.
        self.module = Module.objects.create(device=self.device, module_bay=bay, module_type=self.module_type)
        self.interface = Interface.objects.get(module=self.module)


class ForegroundApplyInABranchTest(_PlainModuleCase):
    PREFIX = "BrApply"

    def build(self):
        super().build()
        self.rule = InterfaceNameRule.objects.create(
            module_type=self.module_type, name_template="et-0/0/{bay_position}"
        )

    def test_the_apply_renames_in_the_branch_only(self):
        response = self.client.post(
            self.apply_url(self.rule), {"action": "apply", "interface_ids": [str(self.interface.pk)]}
        )

        self.assertEqual(messages_of(response), [("success", "Applied rule: 1 interface(s) renamed.")])
        with self.in_branch():
            self.assertEqual(names_of(self.module), ["et-0/0/0"])
        self.assertEqual(names_of(self.module), ["0"])
        self.assertEqual(self.branch_updates_of(self.interface), [("0", "et-0/0/0")])
        self.assertEqual(list(self.change_diffs().values_list("object_id", flat=True)), [self.interface.pk])

    def test_the_toggle_writes_the_flag_in_the_branch_only(self):
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_toggle", args=[self.rule.pk])

        response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.json(), {"enabled": False, "pk": self.rule.pk})
        with self.in_branch():
            self.assertFalse(InterfaceNameRule.objects.get(pk=self.rule.pk).enabled)
        self.assertTrue(InterfaceNameRule.objects.get(pk=self.rule.pk).enabled)


class TwoAliasAtomicityTest(_PlainModuleCase):
    PREFIX = "BrAtomic"

    def build(self):
        super().build()
        # The eleventh name is one character longer than NetBox allows, after ten rows were written.
        self.rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template=f"{'e' * 60}-{{bay_position}}:{{channel}}",
            breakout_mode=FLAT,
            channel_count=11,
            channel_start=0,
        )

    def test_a_flat_family_blocked_after_partial_writes_leaves_no_row_and_no_change_diff(self):
        response = self.client.post(
            self.apply_url(self.rule), {"action": "apply", "interface_ids": [str(self.interface.pk)]}
        )

        self.assertEqual(
            messages_of(response),
            [
                ("success", "Applied rule: 0 interface(s) renamed."),
                ("warning", "1 interface(s) skipped. The plugin log names each one."),
            ],
        )
        with self.in_branch():
            self.assertEqual(names_of(self.module), ["0"])
        self.assertEqual(ObjectChange.objects.using(self.alias).filter(changed_object_id=self.interface.pk).count(), 0)
        self.assertFalse(self.change_diffs().exists())


class RuleCacheInABranchTest(_PlainModuleCase):
    PREFIX = "BrCache"

    def build(self):
        super().build()
        self.rule = InterfaceNameRule.objects.create(
            module_type=self.module_type, name_template="et-0/0/{bay_position}"
        )

    def _select(self):
        return find_matching_rule(self.module_type, None, self.device_type)

    def test_each_alias_gets_its_own_rule_instance(self):
        on_main = self._select()
        with self.in_branch():
            in_branch = self._select()

        self.assertEqual((on_main.pk, on_main._state.db), (self.rule.pk, "default"))
        self.assertEqual((in_branch.pk, in_branch._state.db), (self.rule.pk, self.alias))

    def test_a_pinned_cache_does_not_serve_main_in_the_branch(self):
        with pinned_rule_cache():
            self._select()
            with self.in_branch():
                self.assertEqual(self._select()._state.db, self.alias)

    def test_a_rule_changed_in_the_branch_only_changes_the_selection_in_the_branch_only(self):
        self._select()
        with self.in_branch():
            self._select()
            rule = InterfaceNameRule.objects.get(pk=self.rule.pk)
            rule.snapshot()
            rule.name_template = "xe-0/0/{bay_position}"
            rule.save()
            in_branch = self._select()
        on_main = self._select()

        self.assertEqual((in_branch.name_template, in_branch._state.db), ("xe-0/0/{bay_position}", self.alias))
        self.assertEqual((on_main.name_template, on_main._state.db), ("et-0/0/{bay_position}", "default"))


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class _ConversionCase(_BranchWriteCase):
    """A flat family built on main by a flat rule, and the rule then switched to the channelized topology."""

    def build(self):
        manufacturer = make_manufacturer(self.PREFIX)
        device_type = make_device_type(manufacturer, self.PREFIX)
        make_module_bay_templates(device_type, ("Bay 0", "Bay 1", "Bay 2", "Bay 3"))
        self.device = make_device(self.PREFIX, device_type)
        module_type = make_module_type(manufacturer, self.PREFIX)
        InterfaceTemplate.objects.create(module_type=module_type, name="{module}", type=PLAIN_TYPE)
        self.rule = InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=FLAT,
            channel_count=4,
            channel_start=0,
        )
        bay = ModuleBay.objects.get(device=self.device, name="Bay 3")
        # The rename trigger builds the family when the install commits.
        with transaction.atomic():
            self.module = Module.objects.create(device=self.device, module_bay=bay, module_type=module_type)
        self.rule.snapshot()
        self.rule.breakout_mode = CHANNELIZED
        self.rule.parent_name_template = "et-0/0/{bay_position}"
        self.rule.save()
        self.base = Interface.objects.get(module=self.module, name="xe-0/0/3:0")


class ForegroundConvertInABranchTest(_ConversionCase):
    PREFIX = "BrConvert"
    FLAT_NAMES = ("xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3")

    def test_the_conversion_rewrites_the_family_in_the_branch_only(self):
        response = self.client.post(
            self.apply_url(self.rule), {"action": "convert", "convert_ids": [str(self.base.pk)]}
        )

        self.assertEqual(
            messages_of(response), [("success", "Converted 1 interface family(ies) to the channelized topology.")]
        )
        with self.in_branch():
            self.assertEqual(names_of(self.module), ["et-0/0/3", *self.FLAT_NAMES])
        self.assertEqual(names_of(self.module), list(self.FLAT_NAMES))

    def test_the_conversion_preview_writes_nothing_on_either_connection(self):
        response = self.client.get(self.apply_url(self.rule))

        self.assertEqual([verdict.current_name for verdict in response.context["conversions"]], ["xe-0/0/3:0"])
        with self.in_branch():
            self.assertEqual(names_of(self.module), list(self.FLAT_NAMES))
            self.assertFalse(Interface.objects.filter(module=self.module, channels__isnull=False).exists())
        self.assertFalse(ObjectChange.objects.using(self.alias).exists())
        self.assertFalse(self.change_diffs().exists())


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class _KeptChannelCase(_BranchWriteCase):
    """Channelized families that NetBox named ``<position>`` and ``<position>:<channel>``, and a channelized rule.

    An interface outside each module takes the target of channel 2, so the rule keeps that channel at
    its old name while it renames the parent. NetBox's cascade then renames the kept channel after the
    parent, and the plugin's reconciliation gives it back the name it kept.
    """

    POSITIONS = ("1",)

    def build(self):
        manufacturer = make_manufacturer(self.PREFIX)
        device_type = make_device_type(manufacturer, self.PREFIX)
        make_module_bay_templates(device_type, [f"Bay {index}" for index in range(max(map(int, self.POSITIONS)) + 1)])
        self.device = make_device(self.PREFIX, device_type)
        module_type = _channelized_module_type(manufacturer, f"{self.PREFIX}-QSFP")
        # No rule exists yet, so every family keeps NetBox's raw names.
        self.modules = {
            position: Module.objects.create(
                device=self.device,
                module_bay=ModuleBay.objects.get(device=self.device, name=f"Bay {position}"),
                module_type=module_type,
            )
            for position in self.POSITIONS
        }
        for position in self.POSITIONS:
            Interface.objects.create(device=self.device, name=f"xe-0/0/{position}:1", type=PLAIN_TYPE)
        self.rule = InterfaceNameRule.objects.create(
            module_type=module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    @staticmethod
    def kept(position):
        """Return the names of the family at *position* after the reconciliation gave channel 2 its kept name."""
        return [
            f"{position}:2",
            f"et-0/0/{position}",
            f"xe-0/0/{position}:0",
            f"xe-0/0/{position}:2",
            f"xe-0/0/{position}:3",
        ]

    @staticmethod
    def cascaded(position):
        """Return the names of the family at *position* when channel 2 carries the name of NetBox's cascade."""
        return sorted([f"et-0/0/{position}:2", f"et-0/0/{position}", *(f"xe-0/0/{position}:{c}" for c in (0, 2, 3))])

    def parent(self, position):
        return Interface.objects.get(module=self.modules[position], channels__isnull=False)

    def apply(self, position):
        """Apply the rule in the branch to the family at *position*."""
        with self.in_branch():
            return apply_rule_to_existing(self.rule, interface_ids=[self.parent(position).pk])

    def names(self, position):
        with self.in_branch():
            return names_of(self.modules[position])


class CommitOrderInEveryNestingTest(_KeptChannelCase):
    PREFIX = "BrNesting"
    POSITIONS = ("1", "2", "3", "4", "5")
    # The caller's transactions at the call, outermost first.
    NESTINGS = (
        ("1", ()),
        ("2", ("default",)),
        ("3", ("branch",)),
        ("4", ("default", "branch")),
        ("5", ("branch", "default")),
    )

    def _enter(self, stack, nesting):
        for name in nesting:
            stack.enter_context(transaction.atomic(using=self.alias if name == "branch" else "default"))

    def test_on_commit_orders_cascade_and_reconciliation_for_each_transaction_nesting(self):
        for position, nesting in self.NESTINGS:
            with self.subTest(nesting=nesting), self.in_branch():
                with ExitStack() as caller:
                    self._enter(caller, nesting)
                    self.apply(position)
                self.assertEqual(self.names(position), self.kept(position))

    def test_branch_outer_default_rollback_discards_reconciliation(self):
        with self.in_branch(), transaction.atomic(using=self.alias), transaction.atomic(using="default"):
            self.apply("1")
            transaction.set_rollback(True, using="default")

        self.assertEqual(self.names("1"), self.cascaded("1"))

    def test_default_savepoint_rollback_discards_reconciliation_before_branch_commit(self):
        with self.in_branch(), transaction.atomic(using="default"), transaction.atomic(using=self.alias):
            with transaction.atomic(using="default"):
                self.apply("1")
                transaction.set_rollback(True, using="default")

        self.assertEqual(self.names("1"), self.cascaded("1"))


class CollisionInABranchTest(_KeptChannelCase):
    PREFIX = "BrCollide"

    def test_a_collision_via_apply_skips_that_member_and_keeps_the_rest(self):
        """Another session takes the target of channel 3 after the plugin checked it, before the member saves."""
        channel = Interface.objects.get(module=self.modules["1"], channel_id=3)
        occupied = []

        def occupy(sender, instance, **kwargs):
            if instance.pk == channel.pk and instance.name == "xe-0/0/1:2" and not occupied:
                occupied.append(
                    create_in_another_session(self.branch, device_id=self.device.pk, name="xe-0/0/1:2", type=PLAIN_TYPE)
                )

        with interface_signal(pre_save, occupy):
            response = self.client.post(
                self.apply_url(self.rule), {"action": "apply", "interface_ids": [str(self.parent("1").pk)]}
            )

        self.assertEqual(
            messages_of(response),
            [
                ("success", "Applied rule: 3 interface(s) renamed."),
                ("warning", "2 interface(s) skipped. The plugin log names each one."),
            ],
        )
        self.assertEqual(self.names("1"), ["1:2", "1:3", "et-0/0/1", "xe-0/0/1:0", "xe-0/0/1:3"])
        with self.in_branch():
            self.assertEqual(Interface.objects.get(pk=occupied[0]).name, "xe-0/0/1:2")


class ReconciliationLockTimeoutTest(_KeptChannelCase):
    PREFIX = "BrReconcile"

    def test_a_reconciliation_lock_timeout_keeps_the_change_diff_rows_and_names_each_kept_channel(self):
        """A second session locks the kept channel after NetBox's cascade renamed it, before the reconciliation."""
        channel = Interface.objects.get(module=self.modules["1"], channel_id=2)

        with row_lock_in_another_session(self.alias) as lock:

            def lock_after_the_cascade(sender, instance, **kwargs):
                if instance.pk == channel.pk and instance.name == "et-0/0/1:2":
                    transaction.on_commit(partial(lock, channel.pk), using=instance._state.db)

            with interface_signal(post_save, lock_after_the_cascade):
                response = self.client.post(
                    self.apply_url(self.rule), {"action": "apply", "interface_ids": [str(self.parent("1").pk)]}
                )

        [(level, text)] = messages_of(response)
        self.assertEqual(level, "danger")
        self.assertTrue(text.startswith(f"Failed to apply rule {self.rule}: "), text)
        self.assertIn("Rename each channel back: `et-0/0/1:2` to `1:2`", text)
        self.assertEqual(self.names("1"), self.cascaded("1"))
        with self.in_branch():
            renamed = set(
                Interface.objects.filter(module=self.modules["1"]).exclude(pk=channel.pk).values_list("pk", flat=True)
            )
        self.assertLessEqual(renamed, set(self.change_diffs().values_list("object_id", flat=True)))


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class DocumentedEngineFunctionInACallerTransactionTest(_BranchWriteCase):
    """``apply_device_interface_rules`` inside a caller's transaction keeps a blocked channel at its name.

    The caller frees the target of the blocked channel before it commits, so NetBox's cascade at the
    commit gives the channel that target. The reconciliation runs after the cascade and gives the
    channel back its kept name.
    """

    PREFIX = "BrEngine"
    # The caller's transactions at the call, outermost first, one device each.
    NESTINGS = (("branch",), ("default", "branch"), ("branch", "default"))

    def build(self):
        device_type = make_device_type(make_manufacturer(self.PREFIX), self.PREFIX)
        placement = make_placement(self.PREFIX)
        chassis = VirtualChassis.objects.create(name=f"{self.PREFIX} chassis")
        self.devices = []
        for position in range(1, len(self.NESTINGS) + 1):
            device = make_device(
                self.PREFIX,
                device_type,
                placement,
                name=f"brengine-{position}",
                virtual_chassis=chassis,
                vc_position=position,
            )
            parent = Interface.objects.create(device=device, name="et0", type=PARENT_TYPE, channels=4)
            for channel_id in range(1, 5):
                Interface.objects.create(
                    device=device, name=f"et0:{channel_id}", type=CHANNEL_TYPE, parent=parent, channel_id=channel_id
                )
            # Channel 2 cannot take its target, so the rule keeps its name.
            Interface.objects.create(device=device, name=f"eth{position}:2", type=PLAIN_TYPE)
            self.devices.append(device)
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True, module_type_pattern=r"et\d+", name_template="eth{vc_position}"
        )

    def test_a_blocked_channel_keeps_its_name_after_the_caller_frees_its_target_and_commits(self):
        for position, (device, nesting) in enumerate(zip(self.devices, self.NESTINGS, strict=True), start=1):
            with self.subTest(nesting=nesting), self.in_branch():
                with ExitStack() as caller:
                    for name in nesting:
                        caller.enter_context(transaction.atomic(using=self.alias if name == "branch" else "default"))
                    self.assertEqual(apply_device_interface_rules(device), 4)
                    rename_out_of_band(Interface.objects.get(device=device, name=f"eth{position}:2"), f"free{position}")
                names = sorted(Interface.objects.filter(device=device).values_list("name", flat=True))
                self.assertEqual(
                    names, ["et0:2", f"eth{position}", *(f"eth{position}:{c}" for c in (1, 3, 4)), f"free{position}"]
                )
