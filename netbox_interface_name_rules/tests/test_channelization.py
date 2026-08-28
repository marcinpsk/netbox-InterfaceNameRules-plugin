# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for channel-aware renaming on NetBox versions that model channelized interfaces (4.7+).

A channelized family is one physical parent interface (``channels`` set) plus the channel
subinterfaces bound to it (``type='channel'``, ``parent``, ``channel_id`` 1..N).  The engine must
treat such a family as a single unit: children are never independent rename candidates, they follow
their parent, and a breakout rule renames the existing children instead of creating flat siblings.

Every fixture here is built from real interface templates and installed through NetBox's own module
instantiation, so the parent/child links under test are the ones NetBox creates in production.
"""

import os
from unittest import skipUnless

from dcim.choices import InterfaceTypeChoices
from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    InterfaceTemplate,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    Site,
    VirtualChassis,
)
from django.test import TestCase

from netbox_interface_name_rules.engine import (
    apply_device_interface_rules,
    apply_interface_name_rules,
    apply_rule_to_existing,
    build_variables,
    find_interfaces_for_rule,
    has_applicable_interfaces,
    predict_rule_output,
    supports_channelization,
)
from netbox_interface_name_rules.family import FamilyStatus, execute_installed_plan_set, plan_installed_families
from netbox_interface_name_rules.models import InterfaceNameRule

# Resolved defensively so this module still imports on NetBox releases without channelization.
CHANNEL_TYPE = getattr(InterfaceTypeChoices, "TYPE_CHANNEL", "channel")
PARENT_TYPE = InterfaceTypeChoices.TYPE_40GE_QSFP_PLUS
PLAIN_TYPE = InterfaceTypeChoices.TYPE_10GE_SFP_PLUS

REQUIRES_CHANNELIZATION = "requires a NetBox that models channelized interfaces (4.7+)"
PLUGIN_LOGGER = "netbox_interface_name_rules"


def _build_device(prefix, bay_positions=(), **device_kwargs):
    """Create a manufacturer, a device type with module bays at *bay_positions*, and one device."""
    slug = prefix.lower()
    manufacturer = Manufacturer.objects.create(name=f"{prefix}Mfg", slug=f"{slug}-mfg")
    device_type = DeviceType.objects.create(manufacturer=manufacturer, model=f"{prefix}-Dev", slug=f"{slug}-dev")
    for position in bay_positions:
        ModuleBayTemplate.objects.create(device_type=device_type, name=f"Bay {position}", position=position)
    role = DeviceRole.objects.create(name=f"{prefix}Role", slug=f"{slug}-role")
    site = Site.objects.create(name=f"{prefix}Site", slug=f"{slug}-site")
    device = Device.objects.create(name=f"{slug}-sw1", device_type=device_type, role=role, site=site, **device_kwargs)
    return manufacturer, device


def _channelized_family(module_type, parent_name, child_names, channels=4):
    """Add one channelized parent template plus its channel templates to *module_type*.

    *child_names* maps a channel_id to the template name that channel takes.
    """
    parent = InterfaceTemplate.objects.create(
        module_type=module_type, name=parent_name, type=PARENT_TYPE, channels=channels
    )
    for channel_id, name in child_names.items():
        InterfaceTemplate.objects.create(
            module_type=module_type,
            name=name,
            type=CHANNEL_TYPE,
            parent=parent,
            channel_id=channel_id,
        )
    return parent


def _channelized_module_type(manufacturer, model, channels=4, child_channel_ids=(1, 2, 3, 4), child_names=None):
    """Create a ModuleType whose interface templates form a channelized family.

    The parent template is ``{module}`` with *channels* set; each entry in *child_channel_ids* adds a
    channel-type template bound to it.  *child_names* maps a channel_id to a template name, defaulting
    to the upstream ``<parent>:<channel_id>`` convention.
    """
    module_type = ModuleType.objects.create(manufacturer=manufacturer, model=model, part_number=model)
    names = child_names or {channel_id: f"{{module}}:{channel_id}" for channel_id in child_channel_ids}
    _channelized_family(
        module_type, "{module}", {channel_id: names[channel_id] for channel_id in child_channel_ids}, channels=channels
    )
    return module_type


class ChannelizationTestCase(TestCase):
    """Install helpers shared by the channelized module-install test cases."""

    def _install(self, module_type, position, run_rules=True):
        """Install a module into the bay at *position*; run the post-commit rename unless told not to.

        ``run_rules=False`` leaves the freshly instantiated (raw-named) family in place so a test can
        call the engine directly and assert its return value.
        """
        bay = ModuleBay.objects.get(device=self.device, name=f"Bay {position}")
        if run_rules:
            with self.captureOnCommitCallbacks(execute=True):
                module = Module.objects.create(device=self.device, module_bay=bay, module_type=module_type)
        else:
            module = Module.objects.create(device=self.device, module_bay=bay, module_type=module_type)
        return module, bay

    @staticmethod
    def _names(module):
        """Return the sorted interface names of *module*."""
        return sorted(Interface.objects.filter(module=module).values_list("name", flat=True))

    @staticmethod
    def _parent(module):
        """Return the channelized parent interface of *module*."""
        return Interface.objects.get(module=module, channels__isnull=False)

    @staticmethod
    def _child(module, channel_id):
        """Return the channel subinterface of *module* bound to *channel_id*."""
        return Interface.objects.get(module=module, channel_id=channel_id)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedSimpleRuleTest(ChannelizationTestCase):
    """A simple (non-breakout) rule renames a channelized family in lockstep with its parent."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanSimple", ["3", "f4"])
        cls.module_type = _channelized_module_type(manufacturer, "ChanSimple-QSFP")
        # One child carries a free-form name that shares no prefix with the parent template.
        cls.free_form_type = _channelized_module_type(
            manufacturer,
            "ChanSimple-QSFP-FF",
            child_names={1: "{module}:1", 2: "{module}:2", 3: "{module}:3", 4: "mgmt-chan"},
        )
        cls.rule = InterfaceNameRule.objects.create(module_type=cls.module_type, name_template="et-0/0/{bay_position}")
        cls.free_form_rule = InterfaceNameRule.objects.create(
            module_type=cls.free_form_type, name_template="et-0/0/{bay_position}"
        )

    def test_module_install_renames_parent_and_children_in_lockstep(self):
        """Installing the module renames the parent and carries every child's suffix onto the new name."""
        module, _ = self._install(self.module_type, "3")

        self.assertEqual(
            self._names(module),
            ["et-0/0/3", "et-0/0/3:1", "et-0/0/3:2", "et-0/0/3:3", "et-0/0/3:4"],
        )

    def test_installed_family_stays_structurally_channelized(self):
        """Renaming preserves the structure: the parent keeps its channel count, children their bindings."""
        module, _ = self._install(self.module_type, "3")

        parent = self._parent(module)
        self.assertEqual(parent.name, "et-0/0/3")
        self.assertEqual(parent.channels, 4)
        for channel_id in range(1, 5):
            child = self._child(module, channel_id)
            self.assertEqual(child.name, f"et-0/0/3:{channel_id}")
            self.assertEqual(child.type, CHANNEL_TYPE)
            self.assertEqual(child.parent_id, parent.pk)

    def test_rename_count_includes_children(self):
        """The engine reports every interface it renamed, children included."""
        module, bay = self._install(self.module_type, "3", run_rules=False)

        self.assertEqual(apply_interface_name_rules(module, bay), 5)

    def test_reapply_after_install_is_idempotent(self):
        """A second pass over an already-renamed family changes nothing."""
        module, bay = self._install(self.module_type, "3")

        self.assertEqual(apply_interface_name_rules(module, bay), 0)
        self.assertEqual(
            self._names(module),
            ["et-0/0/3", "et-0/0/3:1", "et-0/0/3:2", "et-0/0/3:3", "et-0/0/3:4"],
        )

    def test_template_without_base_does_not_collide_on_children(self):
        """Child targets derive from the parent's new name, so a template without {base} is collision-free."""
        self.assertNotIn("{base}", self.rule.name_template)

        with self.assertNoLogs(PLUGIN_LOGGER, level="WARNING"):
            module, _ = self._install(self.module_type, "3")

        self.assertEqual(Interface.objects.filter(module=module).count(), 5)

    def test_free_form_child_is_left_untouched_and_logged(self):
        """A child whose name shares no prefix with its parent is skipped and reported, not guessed at."""
        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            module, _ = self._install(self.free_form_type, "f4")

        self.assertEqual(
            self._names(module),
            ["et-0/0/f4", "et-0/0/f4:1", "et-0/0/f4:2", "et-0/0/f4:3", "mgmt-chan"],
        )
        self.assertTrue(any("mgmt-chan" in line for line in logs.output), logs.output)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedCollisionTest(ChannelizationTestCase):
    """Name collisions are per-family: a blocked parent stops everything, a blocked child stops only itself."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanCol", ["3"])
        cls.module_type = _channelized_module_type(manufacturer, "ChanCol-QSFP")
        cls.rule = InterfaceNameRule.objects.create(module_type=cls.module_type, name_template="et-0/0/{bay_position}")

    def _occupy(self, name):
        """Create an unrelated device-level interface holding *name*."""
        return Interface.objects.create(device=self.device, name=name, type=PLAIN_TYPE, module=None)

    def test_parent_target_collision_leaves_family_unchanged(self):
        """When the parent target is taken the whole family stays put — no half-renamed children."""
        self._occupy("et-0/0/3")
        module, bay = self._install(self.module_type, "3", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["3", "3:1", "3:2", "3:3", "3:4"])
        self.assertTrue(any("et-0/0/3" in line for line in logs.output), logs.output)
        # Only the parent was attempted: no child was tried against the taken name either.
        self.assertFalse(any("3:" in line for line in logs.output), logs.output)
        # A collision is not evidence that the rule is obsolete.
        self.assertFalse(self.rule.tags.filter(slug="potentially-deprecated").exists())

    def test_child_target_collision_skips_only_that_child(self):
        """A taken child target is recorded and skipped; parent and siblings still rename."""
        self._occupy("et-0/0/3:2")
        module, bay = self._install(self.module_type, "3", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 4)
        self.assertEqual(self._names(module), ["3:2", "et-0/0/3", "et-0/0/3:1", "et-0/0/3:3", "et-0/0/3:4"])
        self.assertTrue(any("et-0/0/3:2" in line for line in logs.output), logs.output)

    def test_force_reapply_heals_stale_child_after_collision_removed(self):
        """Once the blocker is gone, a forced re-apply recovers the stale child's suffix from the templates."""
        occupant = self._occupy("et-0/0/3:2")
        module, bay = self._install(self.module_type, "3", run_rules=False)
        apply_interface_name_rules(module, bay)
        self.assertEqual(self._child(module, 2).name, "3:2")  # the parent prefix is no longer shared
        occupant.delete()

        healed = apply_interface_name_rules(module, bay, force_reapply=True)

        self.assertEqual(healed, 1)
        self.assertEqual(
            self._names(module),
            ["et-0/0/3", "et-0/0/3:1", "et-0/0/3:2", "et-0/0/3:3", "et-0/0/3:4"],
        )


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedBreakoutRuleTest(ChannelizationTestCase):
    """A breakout rule renames the existing channel subinterfaces; it never creates flat siblings."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanBrk", ["3", "b4", "e5", "p6", "m8"])
        cls.module_type = _channelized_module_type(manufacturer, "ChanBrk-QSFP")
        cls.base_type = _channelized_module_type(manufacturer, "ChanBrk-QSFP-BASE")
        cls.empty_type = _channelized_module_type(manufacturer, "ChanBrk-QSFP-EMPTY", child_channel_ids=())
        cls.partial_type = _channelized_module_type(manufacturer, "ChanBrk-QSFP-PART", child_channel_ids=(1, 2))
        cls.mismatch_type = _channelized_module_type(manufacturer, "ChanBrk-QSFP-MM", channels=8)
        for module_type in (cls.module_type, cls.empty_type, cls.partial_type, cls.mismatch_type):
            InterfaceNameRule.objects.create(
                module_type=module_type,
                name_template="xe-0/0/{bay_position}:{channel}",
                channel_count=4,
                channel_start=0,
            )
        InterfaceNameRule.objects.create(
            module_type=cls.base_type,
            name_template="{base}-ch{channel}",
            channel_count=4,
            channel_start=0,
        )

    def test_breakout_maps_channel_id_to_the_channel_variable(self):
        """{channel} is channel_start + channel_id - 1, so channel_id 1 renders as :0 with channel_start=0."""
        module, _ = self._install(self.module_type, "3")

        self.assertEqual(self._parent(module).name, "3")  # Phase A: the parent keeps its raw name
        self.assertEqual(self._names(module), ["3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"])
        for channel_id in range(1, 5):
            self.assertEqual(self._child(module, channel_id).name, f"xe-0/0/3:{channel_id - 1}")
        self.assertEqual(Interface.objects.filter(module=module).count(), 5)  # nothing was created

    def test_breakout_base_variable_resolves_to_the_parent_raw_name(self):
        """{base} on a channelized family is the parent's name, not each child's."""
        module, _ = self._install(self.base_type, "b4")

        self.assertEqual(self._names(module), ["b4", "b4-ch0", "b4-ch1", "b4-ch2", "b4-ch3"])

    def test_channelized_parent_without_children_gets_no_flat_siblings(self):
        """A parent with channels set but no children is still channelized — flat creation stays off."""
        module, _ = self._install(self.empty_type, "e5")

        self.assertEqual(self._names(module), ["e5"])

    def test_partial_family_gets_no_flat_siblings(self):
        """A partially populated family renames the children it has and creates nothing for the rest."""
        module, _ = self._install(self.partial_type, "p6")

        self.assertEqual(self._names(module), ["p6", "xe-0/0/p6:0", "xe-0/0/p6:1"])

    def test_channel_count_mismatch_skips_family_with_log(self):
        """channels=8 against a channel_count=4 rule is a modelling mismatch: skip the family, log it."""
        module, bay = self._install(self.mismatch_type, "m8", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["m8", "m8:1", "m8:2", "m8:3", "m8:4"])
        self.assertTrue(any("m8" in line for line in logs.output), logs.output)

    def test_preview_offers_channel_renames_and_no_new_interfaces(self):
        """The preview mirrors the apply: the parent keeps its name and no channel is invented."""
        self._install(self.module_type, "3", run_rules=False)
        rule = InterfaceNameRule.objects.get(module_type=self.module_type)

        results, total_checked = find_interfaces_for_rule(rule)

        self.assertEqual(total_checked, 1)  # the family is one candidate
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["current_name"], "3")
        self.assertEqual(results[0]["new_names"], ["3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"])
        self.assertEqual(
            [(detail.role, detail.channel_id) for detail in results[0]["name_details"]],
            [("parent", None), ("channel", 1), ("channel", 2), ("channel", 3), ("channel", 4)],
        )

    def test_stale_children_repaired_when_parent_already_correct(self):
        """The parent needs no rename, so the run must still reach — and repair — its children."""
        module, bay = self._install(self.module_type, "3")
        stale = self._child(module, 2)
        stale.name = "xe-0/0/3:9"
        stale.save()

        repaired = apply_interface_name_rules(module, bay)

        self.assertEqual(repaired, 1)
        self.assertEqual(self._child(module, 2).name, "xe-0/0/3:1")


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedEnumerationTest(ChannelizationTestCase):
    """Enumeration and bulk-apply paths treat a family as one candidate keyed on its parent."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanEnum", ["3", "9"])
        cls.module_type = _channelized_module_type(manufacturer, "ChanEnum-QSFP")
        # A standalone interface alongside the family, so "families counted once" stays distinguishable
        # from "interfaces not counted at all".
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="{module}-mgmt", type=PLAIN_TYPE)
        cls.rule = InterfaceNameRule.objects.create(module_type=cls.module_type, name_template="et-{base}")
        # A second family whose template does not feed the current name back in, so "already correctly
        # named" is a state the rule can actually reach (an "et-{base}" rule renames on every pass).
        cls.stable_type = _channelized_module_type(manufacturer, "ChanEnum-QSFP-STABLE")
        cls.stable_rule = InterfaceNameRule.objects.create(
            module_type=cls.stable_type, name_template="et-0/0/{bay_position}"
        )

    def test_find_interfaces_for_rule_excludes_children_and_counts_families_once(self):
        """The scan offers the parent and the standalone interface — never a child on its own."""
        self._install(self.module_type, "3", run_rules=False)

        results, total_checked = find_interfaces_for_rule(self.rule)

        self.assertEqual({entry["current_name"] for entry in results}, {"3", "3-mgmt"})
        self.assertEqual(total_checked, 2)

    def test_preview_annotates_the_family_behind_the_parent_entry(self):
        """The family's channels ride along in the parent's entry, each tagged with its channel."""
        self._install(self.module_type, "3", run_rules=False)

        results, _ = find_interfaces_for_rule(self.rule)
        family = next(entry for entry in results if entry["current_name"] == "3")
        standalone = next(entry for entry in results if entry["current_name"] == "3-mgmt")

        self.assertEqual(family["new_names"], ["et-3", "et-3:1", "et-3:2", "et-3:3", "et-3:4"])
        self.assertEqual(
            [(detail.name, detail.role, detail.channel_id) for detail in family["name_details"]],
            [
                ("et-3", "parent", None),
                ("et-3:1", "channel", 1),
                ("et-3:2", "channel", 2),
                ("et-3:3", "channel", 3),
                ("et-3:4", "channel", 4),
            ],
        )
        self.assertEqual(family["interface"], self._parent(family["module"]))
        self.assertEqual(
            [(detail.name, detail.role, detail.channel_id) for detail in standalone["name_details"]],
            [("et-3-mgmt", "interface", None)],
        )

    def test_has_applicable_interfaces_ignores_a_renamed_family(self):
        """Children must not keep the rule looking 'applicable' after the family is correctly named."""
        module, bay = self._install(self.stable_type, "9", run_rules=False)
        self.assertTrue(has_applicable_interfaces(self.stable_rule))

        apply_interface_name_rules(module, bay)

        self.assertFalse(has_applicable_interfaces(self.stable_rule))

    def test_apply_rule_to_existing_renames_family_without_conflicts(self):
        """A retroactive apply renames the family through its parent, so no child fights another for a name."""
        module, _ = self._install(self.module_type, "3", run_rules=False)

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.changed_count, 6)
        self.assertEqual(outcome.skipped_members, ())
        self.assertEqual(self._names(module), ["et-3", "et-3-mgmt", "et-3:1", "et-3:2", "et-3:3", "et-3:4"])
        # The rename went through the family, so the structure it hangs on is still intact.
        parent = self._parent(module)
        self.assertEqual(parent.channels, 4)
        self.assertEqual([self._child(module, cid).parent_id for cid in range(1, 5)], [parent.pk] * 4)

    def test_apply_rule_to_existing_selected_parent_renames_whole_family(self):
        """Selecting the parent PK (what the Apply view submits) carries the children along."""
        module, _ = self._install(self.module_type, "3", run_rules=False)
        parent = self._parent(module)

        outcome = apply_rule_to_existing(self.rule, interface_ids=[parent.pk])

        self.assertEqual(outcome.changed_count, 5)
        self.assertEqual(self._names(module), ["3-mgmt", "et-3", "et-3:1", "et-3:2", "et-3:3", "et-3:4"])

    def test_apply_rule_to_existing_ignores_a_child_only_selection(self):
        """A child is not an independent candidate, so selecting one alone renames nothing."""
        module, _ = self._install(self.module_type, "3", run_rules=False)
        child = self._child(module, 1)

        outcome = apply_rule_to_existing(self.rule, interface_ids=[child.pk])

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(self._names(module), ["3", "3-mgmt", "3:1", "3:2", "3:3", "3:4"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedDeviceRuleTest(TestCase):
    """Device-level (virtual-chassis) rules follow the same family semantics as the module path."""

    @classmethod
    def setUpTestData(cls):
        vc = VirtualChassis.objects.create(name="chanvc-vc")
        _, cls.device = _build_device("ChanVC", virtual_chassis=vc, vc_position=1)
        cls.device_type = cls.device.device_type
        cls.parent = Interface.objects.create(device=cls.device, name="et0", type=PARENT_TYPE, channels=4, module=None)
        for channel_id in range(1, 5):
            Interface.objects.create(
                device=cls.device,
                name=f"et0:{channel_id}",
                type=CHANNEL_TYPE,
                parent=cls.parent,
                channel_id=channel_id,
                module=None,
            )

    def _names(self):
        """Return the sorted interface names on the fixture device."""
        return sorted(Interface.objects.filter(device=self.device).values_list("name", flat=True))

    def test_children_follow_parent_and_are_not_matched_independently(self):
        """Matched independently every child would compute the parent's name; following it keeps them distinct."""
        InterfaceNameRule.objects.create(applies_to_device_interfaces=True, name_template="eth{vc_position}")

        renamed = apply_device_interface_rules(self.device)

        self.assertEqual(renamed, 5)
        self.assertEqual(self._names(), ["eth1", "eth1:1", "eth1:2", "eth1:3", "eth1:4"])

    def test_a_channel_count_on_a_device_rule_does_not_block_the_family(self):
        """A device rule never builds a family, so a channel count on one says nothing about this one."""
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="eth{vc_position}",
            channel_count=2,
            channel_start=0,
        )

        renamed = apply_device_interface_rules(self.device)

        self.assertEqual(renamed, 5)
        self.assertEqual(self._names(), ["eth1", "eth1:1", "eth1:2", "eth1:3", "eth1:4"])

    def test_rule_matching_only_children_renames_nothing(self):
        """A pattern that hits only channel subinterfaces has no parent to act on."""
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern=r"et0:\d+",
            name_template="ch{vc_position}-{port}",
        )

        renamed = apply_device_interface_rules(self.device)

        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(), ["et0", "et0:1", "et0:2", "et0:3", "et0:4"])

    def test_higher_priority_rule_consumes_the_whole_family(self):
        """The winning rule claims parent and children, so a lower-priority rule cannot take the leftovers."""
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            device_type=self.device_type,
            module_type_pattern=r"et\d+",
            name_template="eth{vc_position}",
        )
        InterfaceNameRule.objects.create(applies_to_device_interfaces=True, name_template="zz{vc_position}-{port}")

        renamed = apply_device_interface_rules(self.device)

        self.assertEqual(renamed, 5)
        self.assertEqual(self._names(), ["eth1", "eth1:1", "eth1:2", "eth1:3", "eth1:4"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedBreakoutStandaloneTest(ChannelizationTestCase):
    """A breakout rule on a channelized module leaves the module's standalone interfaces alone."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanMixed", ["7"])
        cls.module_type = _channelized_module_type(manufacturer, "ChanMixed-QSFP")
        InterfaceTemplate.objects.create(module_type=cls.module_type, name="{module}-mgmt", type=PLAIN_TYPE)
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="{base}-ch{channel}",
            channel_count=4,
            channel_start=0,
        )

    def test_standalone_base_is_not_broken_out_beside_a_family(self):
        """Preview and bulk apply process only the families here — the install path must agree."""
        module, bay = self._install(self.module_type, "7", run_rules=False)

        renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(renamed, 4)  # the four channels; the standalone was neither renamed nor expanded
        self.assertEqual(self._names(module), ["7", "7-ch0", "7-ch1", "7-ch2", "7-ch3", "7-mgmt"])

    def test_skipped_standalone_base_is_logged(self):
        """The skip is a structural decision, so it is traceable in the log."""
        module, bay = self._install(self.module_type, "7", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="DEBUG") as logs:
            apply_interface_name_rules(module, bay)

        self.assertTrue(any("7-mgmt" in line for line in logs.output), logs.output)

    def test_apply_and_preview_agree_on_the_module(self):
        """The retroactive paths already skip the standalone; the automatic one produces the same names."""
        module, bay = self._install(self.module_type, "7", run_rules=False)
        results, _ = find_interfaces_for_rule(self.rule)

        apply_interface_name_rules(module, bay)

        self.assertEqual([entry["current_name"] for entry in results], ["7"])
        self.assertEqual(sorted(results[0]["new_names"]), ["7", "7-ch0", "7-ch1", "7-ch2", "7-ch3"])
        self.assertEqual(self._names(module), ["7", "7-ch0", "7-ch1", "7-ch2", "7-ch3", "7-mgmt"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedPredictionTest(ChannelizationTestCase):
    """predict_rule_output must name what the apply path actually produces on a channelized module type."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanPred", ["3", "5", "8"])
        cls.breakout_type = _channelized_module_type(manufacturer, "ChanPred-QSFP-BRK")
        cls.simple_type = _channelized_module_type(
            manufacturer,
            "ChanPred-QSFP-SMP",
            child_names={1: "{module}:1", 2: "{module}:2", 3: "{module}:3", 4: "mgmt-chan"},
        )
        cls.mismatch_type = _channelized_module_type(manufacturer, "ChanPred-QSFP-MM", channels=8)
        InterfaceNameRule.objects.create(
            module_type=cls.breakout_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )
        InterfaceNameRule.objects.create(module_type=cls.simple_type, name_template="et-0/0/{bay_position}")
        InterfaceNameRule.objects.create(
            module_type=cls.mismatch_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )

    def test_breakout_prediction_matches_the_applied_names(self):
        """The parent keeps its name and each channel is predicted from its channel_id, as apply does."""
        module, bay = self._install(self.breakout_type, "3", run_rules=False)
        raw_names = self._names(module)

        predicted = predict_rule_output(module, bay, raw_names)

        self.assertEqual(predicted, ["3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"])
        apply_interface_name_rules(module, bay)
        self.assertEqual(sorted(predicted), self._names(module))

    def test_simple_prediction_follows_the_parent_and_keeps_child_suffixes(self):
        """A channel follows its parent's predicted name; one that shares no prefix is predicted unchanged."""
        module, bay = self._install(self.simple_type, "5", run_rules=False)
        raw_names = self._names(module)

        predicted = predict_rule_output(module, bay, raw_names)

        self.assertEqual(predicted, ["et-0/0/5", "et-0/0/5:1", "et-0/0/5:2", "et-0/0/5:3", "mgmt-chan"])
        apply_interface_name_rules(module, bay)
        self.assertEqual(sorted(predicted), self._names(module))

    def test_prediction_reports_no_change_on_a_channel_count_mismatch(self):
        """apply skips a family whose channel count disagrees with the rule, so prediction must too."""
        module, bay = self._install(self.mismatch_type, "8", run_rules=False)
        raw_names = self._names(module)

        predicted = predict_rule_output(module, bay, raw_names)

        self.assertEqual(predicted, raw_names)
        apply_interface_name_rules(module, bay)
        self.assertEqual(self._names(module), raw_names)

    def test_prediction_still_touches_no_interfaces(self):
        """Prediction only reads — the family it describes is left exactly as it was."""
        module, bay = self._install(self.breakout_type, "3", run_rules=False)

        predict_rule_output(module, bay, self._names(module))

        self.assertEqual(self._names(module), ["3", "3:1", "3:2", "3:3", "3:4"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedBreakoutTemplateErrorTest(ChannelizationTestCase):
    """A template that fails on a later channel must not leave the family half renamed."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanErr", ["4"])
        cls.module_type = _channelized_module_type(manufacturer, "ChanErr-QSFP")
        InterfaceTemplate.objects.create(
            module_type=cls.module_type,
            name="mgmt-{module}",
            type=PLAIN_TYPE,
        )
        # Valid for channel 0, divides by zero from channel 1 on.
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-{4 // (1 - {channel})}",
            channel_count=4,
            channel_start=0,
        )

    def test_template_failing_on_a_later_channel_renames_no_child(self):
        """Every child name is computed before the first save, so one bad channel aborts the family."""
        module, bay = self._install(self.module_type, "4", run_rules=False)

        plan_set = plan_installed_families(module, self.rule, build_variables(bay, device=self.device))
        outcome = execute_installed_plan_set(plan_set)

        renamed = apply_interface_name_rules(module, bay)

        self.assertEqual(outcome.families[0].status, FamilyStatus.FAILED)
        self.assertEqual(
            {member.status for member in outcome.families[0].members},
            {FamilyStatus.FAILED},
        )
        self.assertEqual(renamed, 0)
        self.assertEqual(self._names(module), ["4", "4:1", "4:2", "4:3", "4:4", "mgmt-4"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedSuffixRecoveryTest(ChannelizationTestCase):
    """Recovering a stranded child's suffix from the templates must not borrow another family's convention."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanRec", ["1", "2"])
        # Two families on one module type, each with its own suffix convention for the same channel_id.
        cls.two_family_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="ChanRec-QSFP-2F", part_number="ChanRec-QSFP-2F"
        )
        _channelized_family(
            cls.two_family_type, "{module}a", {channel_id: f"{{module}}a:{channel_id}" for channel_id in range(1, 5)}
        )
        _channelized_family(
            cls.two_family_type, "{module}b", {channel_id: f"{{module}}b.{channel_id}" for channel_id in range(1, 5)}
        )
        cls.single_family_type = _channelized_module_type(manufacturer, "ChanRec-QSFP-1F")
        for module_type in (cls.two_family_type, cls.single_family_type):
            InterfaceNameRule.objects.create(module_type=module_type, name_template="{base}-x")

    def _strand_child(self, module_type, position, blocked_name, stranded_name):
        """Install *module_type*, let one child collide with *blocked_name*, then free the name again."""
        occupant = Interface.objects.create(device=self.device, name=blocked_name, type=PLAIN_TYPE, module=None)
        module, bay = self._install(module_type, position, run_rules=False)
        apply_interface_name_rules(module, bay)
        stranded = Interface.objects.get(module=module, name=stranded_name)
        occupant.delete()
        return module, bay, stranded

    def test_ambiguous_template_suffix_is_not_borrowed_from_the_other_family(self):
        """Channel 2 means ``:2`` in one family and ``.2`` in the other — so neither may be applied."""
        module, bay, stranded = self._strand_child(self.two_family_type, "1", "1a-x:2", "1a:2")

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            apply_interface_name_rules(module, bay, force_reapply=True)

        stranded.refresh_from_db()
        self.assertEqual(stranded.name, "1a:2")  # left stale rather than renamed into the wrong family's shape
        self.assertTrue(any("1a:2" in line for line in logs.output), logs.output)
        self.assertEqual(
            self._names(module),
            [
                "1a-x-x",
                "1a-x-x:1",
                "1a-x-x:3",
                "1a-x-x:4",
                "1a:2",
                "1b-x-x",
                "1b-x-x.1",
                "1b-x-x.2",
                "1b-x-x.3",
                "1b-x-x.4",
            ],
        )

    def test_single_family_recovery_still_heals_a_stranded_child(self):
        """With one family the suffix for a channel is unambiguous, so the stale child is repaired."""
        module, bay, stranded = self._strand_child(self.single_family_type, "2", "2-x:2", "2:2")

        apply_interface_name_rules(module, bay, force_reapply=True)

        stranded.refresh_from_db()
        self.assertEqual(stranded.name, "2-x-x:2")
        self.assertEqual(self._names(module), ["2-x-x", "2-x-x:1", "2-x-x:2", "2-x-x:3", "2-x-x:4"])


class ChannelizationFeatureDetectionTest(TestCase):
    """Feature detection must track the real Interface model on every NetBox leg (never skipped)."""

    def test_supports_channelization_matches_the_interface_model(self):
        """The probe agrees with the model it probes, whichever NetBox is installed."""
        has_channel_id = any(field.name == "channel_id" for field in Interface._meta.get_fields())

        self.assertEqual(supports_channelization(), has_channel_id)

    @skipUnless(os.environ.get("EXPECT_NETBOX_CHANNELIZATION") == "1", "EXPECT_NETBOX_CHANNELIZATION is not set")
    def test_channelization_leg_reports_support(self):
        """CI guard: on the channelization leg a false probe would silently skip this whole file."""
        self.assertTrue(supports_channelization())
