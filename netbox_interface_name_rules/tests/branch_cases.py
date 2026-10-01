# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Test cases that provision a real netbox-branching branch, and the rows and values that the branch tests share.

The cases skip when netbox-branching is not installed. This module imports netbox-branching only inside
functions, so it imports where netbox-branching is not installed.
"""

from unittest import skipUnless

from core.models import ObjectChange
from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connections, transaction
from django.test import TransactionTestCase
from django.urls import reverse

from netbox_interface_name_rules.engine import apply_rule_to_existing, supports_channelization
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import (
    CHANNELIZED,
    FLAT,
    PLAIN_TYPE,
    REQUIRES_CHANNELIZATION,
    activate,
    branch_cookie,
    channelized_module_type,
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_bay_templates,
    make_module_type,
)

User = get_user_model()
BRANCHING_INSTALLED = apps.is_installed("netbox_branching")
BRANCHING_SKIP_REASON = "netbox-branching is not installed"
# Each connection starts from its own value, so a test can tell which value came back where.
DEFAULT_BEFORE = "3s"
BRANCH_BEFORE = "4s"
# The value that PostgreSQL gives a new session.
SERVER_DEFAULT = "0"
# The names of the flat family that each ConversionCase builds in bay 3.
FLAT_NAMES = ("xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3")


def remove_branch(branch):
    """Close the connection of *branch*, then drop its schema."""
    connections[branch.connection_name].close()
    branch.deprovision()


@skipUnless(BRANCHING_INSTALLED, BRANCHING_SKIP_REASON)
class BranchTestCase(TransactionTestCase):
    """Provision real branches. Each branch is removed when its test ends, because ``--reuse-db`` keeps schemas."""

    def provision_branch(self, name, user):
        """Return a new branch named *name*, provisioned by *user* as netbox-branching's own tests do."""
        from netbox_branching.models import Branch

        branch = Branch(name=name)
        branch.save(provision=False)
        self.addCleanup(remove_branch, branch)
        branch.provision(user=user)
        # provision() writes the status with a queryset update, which the instance does not see.
        branch.refresh_from_db()
        return branch


class BranchWriteCase(BranchTestCase):
    """A superuser logged in with netbox-branching's cookie of a branch provisioned from the rows ``build`` made."""

    PREFIX = ""

    def setUp(self):
        self.user = User.objects.create_superuser(username=f"{self.PREFIX.lower()}-operator")
        self.build()
        self.branch = self.provision_branch(self.PREFIX, self.user)
        self.alias = self.branch.connection_name
        self.client.force_login(self.user)
        self.client.cookies[branch_cookie()] = self.branch.schema_id

    def build(self):
        """Create the rows on main that the branch copies."""
        raise NotImplementedError

    def in_branch(self):
        return activate(self.branch)

    def bay(self, position):
        return ModuleBay.objects.get(device=self.device, name=f"Bay {position}")

    def interfaces_at(self, position):
        """Return ``(pk, name)`` of each interface of the module in the bay at *position*, on the active branch or main."""
        interfaces = Interface.objects.filter(device=self.device, module__module_bay__name=f"Bay {position}")
        return list(interfaces.values_list("pk", "name"))

    def interfaces_in_branch(self, position):
        """Return ``(pk, name)`` of each interface in the branch of the module in the bay at *position*."""
        with self.in_branch():
            return self.interfaces_at(position)

    def names_in_branch(self, position):
        """Return the sorted interface names in the branch of the module in the bay at *position*."""
        return sorted(name for _, name in self.interfaces_in_branch(position))

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


class PlainModuleCase(BranchWriteCase):
    """One device with one module bay, and a module whose one interface NetBox named ``0``."""

    def build(self):
        manufacturer = make_manufacturer(self.PREFIX)
        device_type = make_device_type(manufacturer, self.PREFIX)
        make_module_bay_templates(device_type, ("Bay 0",))
        self.device = make_device(self.PREFIX, device_type)
        self.device_type = device_type
        self.module_type = make_module_type(manufacturer, self.PREFIX)
        InterfaceTemplate.objects.create(module_type=self.module_type, name="{module}", type=PLAIN_TYPE)
        # No rule exists yet, so the interface keeps NetBox's raw name.
        self.module = Module.objects.create(device=self.device, module_bay=self.bay(0), module_type=self.module_type)
        self.interface = Interface.objects.get(module=self.module)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ConversionCase(BranchWriteCase):
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
        # The rename trigger builds the family when the install commits.
        with transaction.atomic():
            self.module = Module.objects.create(device=self.device, module_bay=self.bay(3), module_type=module_type)
        self.rule.snapshot()
        self.rule.breakout_mode = CHANNELIZED
        self.rule.parent_name_template = "et-0/0/{bay_position}"
        self.rule.save()
        self.base = Interface.objects.get(module=self.module, name="xe-0/0/3:0")


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelCase(BranchWriteCase):
    """Empty module bays, a channelized module type, and an interface on the target of channel 2 at each position.

    NetBox names a family ``<position>`` and ``<position>:<channel>``. The rule that ``add_rule`` creates
    keeps channel 2 at its old name while it renames the parent. NetBox's cascade then renames the kept
    channel after the parent, and the plugin's reconciliation gives it back the name it kept.
    """

    POSITIONS = ("1",)

    def build(self):
        manufacturer = make_manufacturer(self.PREFIX)
        device_type = make_device_type(manufacturer, self.PREFIX)
        make_module_bay_templates(device_type, [f"Bay {index}" for index in range(max(map(int, self.POSITIONS)) + 1)])
        self.device = make_device(self.PREFIX, device_type)
        self.module_type = channelized_module_type(manufacturer, f"{self.PREFIX}-QSFP")
        for position in self.POSITIONS:
            Interface.objects.create(device=self.device, name=f"xe-0/0/{position}:1", type=PLAIN_TYPE)

    def add_rule(self):
        self.rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
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


class KeptChannelCase(ChannelCase):
    """The families installed on main before the rule exists, so they keep NetBox's raw names, and the rule."""

    def build(self):
        super().build()
        self.modules = {
            position: Module.objects.create(
                device=self.device, module_bay=self.bay(position), module_type=self.module_type
            )
            for position in self.POSITIONS
        }
        self.add_rule()

    def parent(self, position):
        return Interface.objects.get(module=self.modules[position], channels__isnull=False)

    def apply(self, position):
        """Apply the rule in the branch to the family at *position*."""
        with self.in_branch():
            return apply_rule_to_existing(self.rule, interface_ids=[self.parent(position).pk])
