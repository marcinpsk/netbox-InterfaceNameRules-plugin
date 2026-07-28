# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for breakout rules that build a channelized family, on NetBox 4.7+.

A ``breakout_mode=channelized`` rule turns one plain port into the topology NetBox models: the
base row becomes the physical parent (``channels`` set, keeping its pk, type and module link) and
N channel subinterfaces are created under it (``type='channel'``, ``parent``, ``channel_id``
1..N).  Because that replaces rows an operator may already have cabled or addressed, creation is
preflighted: if the parent target or any channel name is taken, the whole family is skipped and
nothing at all is mutated.

Everything runs against real module installs, so the structure under test is the one NetBox's own
instantiation and validation produce.  Mode-independent behaviour lives in test_breakout_mode.py.
"""

from unittest import skipUnless

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.urls import reverse

from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    apply_rule_to_existing,
    find_interfaces_for_rule,
    predict_rule_output,
    supports_channelization,
)
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.test_breakout_mode import (
    CHANNELIZED,
    FLAT,
    TEST_PASSWORD,
    _plain_module_type,
)
from netbox_interface_name_rules.tests.test_channelization import (
    CHANNEL_TYPE,
    PARENT_TYPE,
    PLAIN_TYPE,
    PLUGIN_LOGGER,
    REQUIRES_CHANNELIZATION,
    ChannelizationTestCase,
    _build_device,
    _channelized_module_type,
)

User = get_user_model()


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedModeInstallTest(ChannelizationTestCase):
    """Installing a module under a channelized rule builds the parent/child family."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanMode", ["3", "4", "5", "6", "7"])
        cls.named_type = _plain_module_type(manufacturer, "ChanMode-QSFP")
        cls.bare_type = _plain_module_type(manufacturer, "ChanMode-QSFP-BARE")
        cls.offset_type = _plain_module_type(manufacturer, "ChanMode-QSFP-OFF")
        cls.vars_type = _plain_module_type(manufacturer, "ChanMode-QSFP-VARS")
        cls.base_type = _plain_module_type(manufacturer, "ChanMode-QSFP-BASE")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.named_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )
        InterfaceNameRule.objects.create(
            module_type=cls.bare_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )
        InterfaceNameRule.objects.create(
            module_type=cls.offset_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=1,
        )
        InterfaceNameRule.objects.create(
            module_type=cls.vars_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-{slot}/0/{bay_position_num}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )
        InterfaceNameRule.objects.create(
            module_type=cls.base_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="{base}-parent",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def test_install_builds_a_parent_and_its_channels(self):
        """One physical port in, one channelized family out — the shape NetBox itself would model."""
        module, _ = self._install(self.named_type, "3")

        self.assertEqual(
            self._names(module),
            ["et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"],
        )
        parent = self._parent(module)
        self.assertEqual(parent.name, "et-0/0/3")
        self.assertEqual(parent.channels, 4)
        for channel_id in range(1, 5):
            child = self._child(module, channel_id)
            self.assertEqual(child.name, f"xe-0/0/3:{channel_id - 1}")
            self.assertEqual(child.type, CHANNEL_TYPE)
            self.assertEqual(child.parent_id, parent.pk)

    def test_the_installed_port_becomes_the_parent_row(self):
        """The physical row keeps its identity — a new row would drop its cable and module link."""
        module, bay = self._install(self.named_type, "3", run_rules=False)
        base = Interface.objects.get(module=module)

        apply_interface_name_rules(module, bay)

        parent = self._parent(module)
        self.assertEqual(parent.pk, base.pk)
        self.assertEqual(parent.type, PARENT_TYPE)
        self.assertEqual(parent.module_id, module.pk)

    def test_apply_counts_the_parent_rename_and_every_created_channel(self):
        """The return value is what the install/apply UI reports, so it covers the whole family."""
        module, bay = self._install(self.named_type, "3", run_rules=False)

        self.assertEqual(apply_interface_name_rules(module, bay), 5)

    def test_a_blank_parent_template_keeps_the_port_name(self):
        """Blank means 'leave the parent alone': only the four channels are new."""
        module, bay = self._install(self.bare_type, "4", run_rules=False)

        created = apply_interface_name_rules(module, bay)

        self.assertEqual(created, 4)
        parent = self._parent(module)
        self.assertEqual(parent.name, "4")
        self.assertEqual(parent.channels, 4)
        self.assertEqual(self._names(module), ["4", "xe-0/0/4:0", "xe-0/0/4:1", "xe-0/0/4:2", "xe-0/0/4:3"])

    def test_channel_start_offsets_the_channel_names(self):
        """{channel} is channel_start + channel_id - 1, so channel_start=1 makes channel_id 1 read :1."""
        module, _ = self._install(self.offset_type, "5")

        for channel_id in range(1, 5):
            self.assertEqual(self._child(module, channel_id).name, f"xe-0/0/5:{channel_id}")

    def test_the_parent_template_sees_the_rule_variables(self):
        """The parent name is built from the same variables as the channels, minus {channel}."""
        module, _ = self._install(self.vars_type, "6")

        self.assertEqual(self._parent(module).name, "et-6/0/6")

    def test_the_parent_template_can_reference_the_current_name(self):
        """{base} is the name NetBox gave the port, so a parent can be derived from it."""
        module, _ = self._install(self.base_type, "7")

        self.assertEqual(self._parent(module).name, "7-parent")

    def test_reapply_after_install_changes_nothing(self):
        """The family already exists; a second pass must not rename or duplicate any of it."""
        module, bay = self._install(self.named_type, "3")

        self.assertEqual(apply_interface_name_rules(module, bay), 0)
        self.assertEqual(
            self._names(module),
            ["et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"],
        )

    def test_force_reapply_creates_no_second_family(self):
        """Forcing re-evaluation re-derives the same names against the same channel ids."""
        module, bay = self._install(self.named_type, "3")

        self.assertEqual(apply_interface_name_rules(module, bay, force_reapply=True), 0)
        self.assertEqual(Interface.objects.filter(module=module).count(), 5)
        self.assertEqual(self._parent(module).channels, 4)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedModePreflightTest(ChannelizationTestCase):
    """A family is built only when every name it needs is free; otherwise nothing moves."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanPre", ["3", "4", "5"])
        cls.module_type = _plain_module_type(manufacturer, "ChanPre-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def _occupy(self, name):
        """Create an unrelated device-level interface holding *name*."""
        return Interface.objects.create(device=self.device, name=name, type=PLAIN_TYPE, module=None)

    def _assert_nothing_was_built(self, module, raw_name):
        """Assert the port is still a plain, raw-named interface with no channels anywhere on it."""
        self.assertEqual(Interface.objects.filter(module=module).count(), 1)
        base = Interface.objects.get(module=module)
        self.assertEqual(base.name, raw_name)
        self.assertIsNone(base.channels)
        self.assertFalse(Interface.objects.filter(module=module, channel_id__isnull=False).exists())

    def test_an_occupied_parent_target_skips_the_family(self):
        """Half a family is worse than none: the channels are not created either."""
        self._occupy("et-0/0/3")
        module, bay = self._install(self.module_type, "3", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            built = apply_interface_name_rules(module, bay)

        self.assertEqual(built, 0)
        self._assert_nothing_was_built(module, "3")
        self.assertTrue(any("et-0/0/3" in line for line in logs.output), logs.output)

    def test_an_occupied_channel_name_skips_the_family_before_anything_is_mutated(self):
        """The check runs before ``channels`` is set, so a blocked family leaves no channelized parent."""
        self._occupy("xe-0/0/4:2")
        module, bay = self._install(self.module_type, "4", run_rules=False)

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            built = apply_interface_name_rules(module, bay)

        self.assertEqual(built, 0)
        self._assert_nothing_was_built(module, "4")
        self.assertTrue(any("xe-0/0/4:2" in line for line in logs.output), logs.output)

    def test_a_blocked_family_is_not_read_as_an_obsolete_rule(self):
        """A collision says the names are taken, not that the rule stopped being needed."""
        self._occupy("et-0/0/3")
        module, bay = self._install(self.module_type, "3", run_rules=False)

        apply_interface_name_rules(module, bay)

        self.assertFalse(self.rule.tags.filter(slug="potentially-deprecated").exists())

    def test_the_interactive_apply_records_the_collision(self):
        """The Apply view counts what it skipped, so the collision is reported, not just logged."""
        self._occupy("et-0/0/5")
        self._install(self.module_type, "5", run_rules=False)
        conflicts: list = []

        built = apply_rule_to_existing(self.rule, conflicts=conflicts)

        self.assertEqual(built, 0)
        self.assertEqual([conflict["attempted_name"] for conflict in conflicts], ["et-0/0/5"])

    def test_the_family_is_built_once_the_blocker_is_gone(self):
        """The skip is a state of the device, not a decision about the rule."""
        occupant = self._occupy("xe-0/0/3:0")
        module, bay = self._install(self.module_type, "3", run_rules=False)
        apply_interface_name_rules(module, bay)
        occupant.delete()

        built = apply_interface_name_rules(module, bay, force_reapply=True)

        self.assertEqual(built, 5)
        self.assertEqual(
            self._names(module),
            ["et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"],
        )


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedModeFlatFamilyTest(ChannelizationTestCase):
    """Switching a rule to channelized never converts the flat family it already installed."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanFlat", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ChanFlat-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=FLAT,
            channel_count=4,
            channel_start=0,
        )

    FLAT_NAMES = ["xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"]

    def setUp(self):
        """Install the flat family the way an earlier apply would have left it."""
        self.module, self.bay = self._install(self.module_type, "3")

    def _switch_to_channelized(self):
        """Point the existing rule at the channelized topology, as an operator would."""
        self.rule.breakout_mode = CHANNELIZED
        self.rule.parent_name_template = "et-0/0/{bay_position}"
        self.rule.save()

    def _assert_still_flat(self):
        """Assert the installed family is untouched and carries no channelized structure."""
        self.assertEqual(self._names(self.module), self.FLAT_NAMES)
        self.assertFalse(Interface.objects.filter(module=self.module, channels__isnull=False).exists())
        self.assertFalse(Interface.objects.filter(module=self.module, channel_id__isnull=False).exists())

    def test_the_installed_family_is_flat_to_begin_with(self):
        """The precondition the rest of this case rests on: four plain siblings, no parent."""
        self._assert_still_flat()

    def test_force_apply_does_not_convert_the_flat_family(self):
        """Conversion rewrites rows an operator owns; it is never a side effect of applying a rule."""
        self._switch_to_channelized()

        changed = apply_interface_name_rules(self.module, self.bay, force_reapply=True)

        self.assertEqual(changed, 0)
        self._assert_still_flat()

    def test_the_refused_conversion_is_reported(self):
        """The operator has to be able to find out why the rule appears to do nothing."""
        self._switch_to_channelized()

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            apply_interface_name_rules(self.module, self.bay, force_reapply=True)

        self.assertTrue(any("xe-0/0/3:" in line for line in logs.output), logs.output)

    def test_the_bulk_apply_path_refuses_it_too(self):
        """Both entry points share the preflight, so neither can convert a family behind the other's back."""
        self._switch_to_channelized()
        conflicts: list = []

        changed = apply_rule_to_existing(self.rule, conflicts=conflicts)

        self.assertEqual(changed, 0)
        self._assert_still_flat()
        self.assertTrue(conflicts, "the skipped family was not reported to the Apply view")


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedModeRetemplatedFlatFamilyTest(ChannelizationTestCase):
    """An installed flat family is never converted, not even when the rule's new names clear its way.

    Switching the mode *and* the name template at once frees every name the channelized family
    would need, so nothing collides: only the module's own structure — more interfaces than its
    module type describes — still says an earlier apply installed a flat family here.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanReTpl", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ChanReTpl-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=FLAT,
            channel_count=4,
            channel_start=0,
        )

    FLAT_NAMES = ["xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"]

    def setUp(self):
        """Install the flat family, then switch the rule to a channelized one with new names."""
        self.module, self.bay = self._install(self.module_type, "3")
        self.rule.breakout_mode = CHANNELIZED
        self.rule.name_template = "et-0/0/{bay_position}:{channel}"
        self.rule.parent_name_template = "et-0/0/{bay_position}"
        self.rule.save()

    def _assert_untouched(self):
        """Assert the installed flat family is exactly as it was, with nothing added beside it."""
        self.assertEqual(self._names(self.module), self.FLAT_NAMES)
        self.assertFalse(Interface.objects.filter(module=self.module, channels__isnull=False).exists())
        self.assertFalse(Interface.objects.filter(module=self.module, channel_id__isnull=False).exists())

    def test_force_apply_builds_no_family_beside_the_flat_one(self):
        """A parent built on one sibling would strand the other three — the hybrid the docs rule out."""
        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            changed = apply_interface_name_rules(self.module, self.bay, force_reapply=True)

        self.assertEqual(changed, 0)
        self._assert_untouched()
        self.assertTrue(any(str(self.module) in line for line in logs.output), logs.output)

    def test_the_bulk_apply_path_refuses_it_too(self):
        """Both entry points share the refusal, so neither can convert a family behind the other's back."""
        conflicts: list = []

        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            changed = apply_rule_to_existing(self.rule, conflicts=conflicts)

        self.assertEqual(changed, 0)
        self._assert_untouched()
        self.assertTrue(conflicts, "the skipped module was not reported to the Apply view")
        self.assertTrue(any(str(self.module) in line for line in logs.output), logs.output)

    def test_the_preview_offers_no_family_it_would_not_build(self):
        """The Apply page must not promise a family the apply path refuses to create."""
        results, total_checked = find_interfaces_for_rule(self.rule)

        self.assertEqual(results, [])
        self.assertEqual(total_checked, len(self.FLAT_NAMES))


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedModeExistingFamilyTest(ChannelizationTestCase):
    """A family NetBox's templates already created is renamed in place, whatever the rule's mode."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanExist", ["3", "4"])
        cls.channelized_type = _channelized_module_type(manufacturer, "ChanExist-QSFP-CH")
        cls.flat_type = _channelized_module_type(manufacturer, "ChanExist-QSFP-FL")
        InterfaceNameRule.objects.create(
            module_type=cls.channelized_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )
        InterfaceNameRule.objects.create(
            module_type=cls.flat_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=FLAT,
            channel_count=4,
            channel_start=0,
        )

    def test_a_channelized_rule_renames_the_existing_family_without_creating_anything(self):
        """The family is already the right shape; only its names are the rule's business."""
        module, _ = self._install(self.channelized_type, "3")

        self.assertEqual(self._names(module), ["et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"])
        self.assertEqual(Interface.objects.filter(module=module).count(), 5)

    def test_the_parent_template_renames_a_template_created_parent(self):
        """A rule that names its parent names it wherever the family came from — one rule, one result."""
        module, _ = self._install(self.channelized_type, "3")

        self.assertEqual(self._parent(module).name, "et-0/0/3")

    def test_a_flat_rule_renames_the_existing_family_the_same_way(self):
        """Structure wins over mode: a family that exists is never given flat siblings."""
        module, _ = self._install(self.flat_type, "4")

        self.assertEqual(self._names(module), ["4", "xe-0/0/4:0", "xe-0/0/4:1", "xe-0/0/4:2", "xe-0/0/4:3"])
        self.assertEqual(self._parent(module).channels, 4)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedModePreviewTest(ChannelizationTestCase):
    """Preview and retroactive apply describe and build the same family."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="chanmodeview", password=TEST_PASSWORD, email="chanmodeview@example.com"
        )
        manufacturer, cls.device = _build_device("ChanPrev", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ChanPrev-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    FAMILY_NAMES = ["et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"]

    def setUp(self):
        """Install the raw module and log in for the view-level preview."""
        self.module, self.bay = self._install(self.module_type, "3", run_rules=False)
        self.client.force_login(self.superuser)

    def test_preview_describes_the_family_it_would_build(self):
        """The Apply page has to show the parent and every channel before anything is created."""
        results, total_checked = find_interfaces_for_rule(self.rule)

        self.assertEqual(total_checked, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["current_name"], "3")
        self.assertEqual(results[0]["new_names"], self.FAMILY_NAMES)
        self.assertEqual(
            [(detail["role"], detail["channel_id"]) for detail in results[0]["name_details"]],
            [("parent", None), ("channel", 1), ("channel", 2), ("channel", 3), ("channel", 4)],
        )

    def test_preview_creates_nothing(self):
        """A preview that builds the family it previews is not a preview."""
        find_interfaces_for_rule(self.rule)

        self.assertEqual(self._names(self.module), ["3"])

    def test_preview_matches_what_the_apply_produces(self):
        """The names an operator confirms are the names they get."""
        results, _ = find_interfaces_for_rule(self.rule)

        apply_rule_to_existing(self.rule)

        self.assertEqual(sorted(results[0]["new_names"]), self._names(self.module))

    def test_retroactive_apply_builds_the_family(self):
        """Applying a rule to already-installed hardware creates the family the preview promised."""
        built = apply_rule_to_existing(self.rule)

        self.assertEqual(built, 5)
        self.assertEqual(self._names(self.module), self.FAMILY_NAMES)
        self.assertEqual(self._parent(self.module).channels, 4)

    def test_applying_the_selected_port_builds_its_family(self):
        """The Apply view submits the base pk; that one pk stands for the whole family."""
        base = Interface.objects.get(module=self.module)

        built = apply_rule_to_existing(self.rule, interface_ids=[base.pk])

        self.assertEqual(built, 5)
        self.assertEqual(self._names(self.module), self.FAMILY_NAMES)

    def test_the_rule_test_view_previews_the_family(self):
        """The interactive builder previews through the same engine call, so it must carry the mode."""
        response = self.client.post(
            reverse("plugins:netbox_interface_name_rules:interfacenamerule_test"),
            {
                "name_template": "xe-0/0/{bay_position}:{channel}",
                "parent_name_template": "et-0/0/{bay_position}",
                "breakout_mode": CHANNELIZED,
                "channel_count": "4",
                "channel_start": "0",
                "module_type": str(self.module_type.pk),
            },
        )

        self.assertEqual(response.status_code, 200)
        db_preview = response.context["db_preview"]
        self.assertEqual([entry["new_names"] for entry in db_preview], [self.FAMILY_NAMES])
        self.assertEqual(self._names(self.module), ["3"])  # still a preview


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedModePredictionTest(ChannelizationTestCase):
    """Prediction names the family a channelized rule builds, so integrations see what apply produces.

    ``predict_rule_output`` is what external tooling asks before it syncs; a prediction that leaves
    out the parent, or that describes flat siblings the rule never creates, is a wrong answer.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanPredMode", ["3", "4", "5"])
        cls.named_type = _plain_module_type(manufacturer, "ChanPredMode-QSFP")
        cls.bare_type = _plain_module_type(manufacturer, "ChanPredMode-QSFP-BARE")
        cls.family_type = _channelized_module_type(manufacturer, "ChanPredMode-QSFP-FAM")
        InterfaceNameRule.objects.create(
            module_type=cls.named_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )
        InterfaceNameRule.objects.create(
            module_type=cls.bare_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )
        InterfaceNameRule.objects.create(
            module_type=cls.family_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def _assert_predicts_what_it_applies(self, module_type, position, expected):
        """Assert the prediction for a raw module is *expected*, and that applying produces the same set."""
        module, bay = self._install(module_type, position, run_rules=False)
        raw_names = self._names(module)

        predicted = predict_rule_output(module, bay, raw_names)

        self.assertEqual(predicted, expected)
        apply_interface_name_rules(module, bay)
        self.assertEqual(sorted(predicted), self._names(module))

    def test_prediction_names_the_parent_and_every_channel(self):
        """The parent is a row the apply path creates, so it belongs in the predicted names."""
        self._assert_predicts_what_it_applies(
            self.named_type, "3", ["et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"]
        )

    def test_a_blank_parent_template_predicts_the_ports_own_name(self):
        """Blank keeps the port's name, so the parent is predicted as the raw name it already has."""
        self._assert_predicts_what_it_applies(
            self.bare_type, "4", ["4", "xe-0/0/4:0", "xe-0/0/4:1", "xe-0/0/4:2", "xe-0/0/4:3"]
        )

    def test_prediction_renames_the_parent_of_a_template_created_family(self):
        """A channelized rule names that family's parent when it applies; prediction must say so."""
        self._assert_predicts_what_it_applies(
            self.family_type, "5", ["et-0/0/5", "xe-0/0/5:0", "xe-0/0/5:1", "xe-0/0/5:2", "xe-0/0/5:3"]
        )

    def test_prediction_still_creates_nothing(self):
        """Prediction reads templates only — the port it describes a family for is left alone."""
        module, bay = self._install(self.named_type, "3", run_rules=False)

        predict_rule_output(module, bay, self._names(module))

        self.assertEqual(self._names(module), ["3"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ChannelizedJuniperE2ETest(ChannelizationTestCase):
    """Juniper 4x10G breakout: one QSFP+ port installed, one et- parent with four xe- channels."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ChanJnpr", ["5"])
        cls.module_type = _plain_module_type(manufacturer, "QSFP-4X10G-LR-CHAN")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            device_type=cls.device.device_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def test_installing_the_transceiver_produces_the_juniper_family(self):
        """The names a Juniper operator expects, over the structure NetBox expects."""
        module, _ = self._install(self.module_type, "5")

        self.assertEqual(
            self._names(module),
            ["et-0/0/5", "xe-0/0/5:0", "xe-0/0/5:1", "xe-0/0/5:2", "xe-0/0/5:3"],
        )

    def test_the_channels_are_bound_to_the_physical_port(self):
        """channel_id 1..4 map onto :0..:3 — the 1-based binding NetBox stores, the 0-based names Juniper uses."""
        module, _ = self._install(self.module_type, "5")
        parent = self._parent(module)
        children = [self._child(module, channel_id) for channel_id in range(1, 5)]

        self.assertEqual(parent.channels, 4)
        self.assertEqual(
            [(child.name, child.parent_id, child.type) for child in children],
            [(f"xe-0/0/5:{channel_id - 1}", parent.pk, CHANNEL_TYPE) for channel_id in range(1, 5)],
        )
