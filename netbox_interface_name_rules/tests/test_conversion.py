# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the assisted flat → channelized conversion, on NetBox 4.7+.

Earlier flat applies (and older plugin versions) left families of N sibling interfaces where
NetBox now models a channelized parent with N channel subinterfaces.  Converting one is a rewrite
of rows an operator owns — cables, addresses, tags — so it is never a side effect of applying a
rule: the operator asks for it per family, from the Apply page, after reading what each family
would become.

The conversion keeps the physical row: the old ch-0 interface becomes the parent (same pk, cable,
type and module link) and its *logical* identity — addresses, VLANs, description, tags — moves to a
newly created channel-1 child that takes over its name.  Every family is preflighted whole,
including a dry-run ``full_clean()`` of the parent and of every prospective child, so a family that
cannot become a valid channelized family is reported as blocked instead of half converted.
"""

import uuid
from unittest import skipIf, skipUnless

from core.models import Job, ObjectType
from dcim.choices import InterfaceModeChoices
from dcim.models import Cable, Interface
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils.html import escape
from extras.models import CustomField, Tag
from ipam.models import VLAN, FHRPGroup, FHRPGroupAssignment, IPAddress

from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    apply_rule_to_existing,
    convert_flat_families,
    find_convertible_families,
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
    PLAIN_TYPE,
    PLUGIN_LOGGER,
    REQUIRES_CHANNELIZATION,
    ChannelizationTestCase,
    _build_device,
)

User = get_user_model()

REQUIRES_NO_CHANNELIZATION = "requires a NetBox that cannot model channelized interfaces (4.6 and older)"

# The upstream validation a sibling that is itself channelized fails as a prospective channel.
CHANNELIZED_CHANNEL_ERROR = "cannot be both channelized and bound to a channel"


class ConversionTestCase(ChannelizationTestCase):
    """Installs flat families with a flat rule, then points that rule at the channelized topology."""

    NAME_TEMPLATE = "xe-0/0/{bay_position}:{channel}"
    PARENT_TEMPLATE = "et-0/0/{bay_position}"

    @classmethod
    def _flat_rule(cls, module_type, **kwargs):
        """Create the flat breakout rule that installs the families these tests convert."""
        fields = {
            "module_type": module_type,
            "name_template": cls.NAME_TEMPLATE,
            "breakout_mode": FLAT,
            "channel_count": 4,
            "channel_start": 0,
        }
        fields.update(kwargs)
        return InterfaceNameRule.objects.create(**fields)

    def _switch_to_channelized(self, rule=None, parent_name_template=None):
        """Point *rule* at the channelized topology, the way an operator would before converting."""
        rule = rule or self.rule
        rule.breakout_mode = CHANNELIZED
        rule.parent_name_template = self.PARENT_TEMPLATE if parent_name_template is None else parent_name_template
        rule.save()
        return rule

    def _iface(self, name):
        """Return the device interface called *name*."""
        return Interface.objects.get(device=self.device, name=name)

    @staticmethod
    def _flat_names(position):
        """Return the flat family's names for the module in the bay at *position*."""
        return [f"xe-0/0/{position}:{channel}" for channel in range(4)]

    @classmethod
    def _channelized_names(cls, position):
        """Return the names the same family carries once converted (sorted, as _names returns them)."""
        return [f"et-0/0/{position}", *cls._flat_names(position)]

    def _assert_still_flat(self, module, position):
        """Assert the family is untouched: four plain siblings, no parent and no channel bindings."""
        self.assertEqual(self._names(module), self._flat_names(position))
        self.assertFalse(Interface.objects.filter(module=module, channels__isnull=False).exists())
        self.assertFalse(Interface.objects.filter(module=module, channel_id__isnull=False).exists())


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ConversionVerdictTest(ConversionTestCase):
    """The per-family verdicts an operator reads before confirming anything."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ConvVerdict", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ConvVerdict-QSFP")
        cls.rule = cls._flat_rule(cls.module_type)

    def setUp(self):
        """Install the flat family, then switch the rule to the topology it should become."""
        self.module, self.bay = self._install(self.module_type, "3")
        self._switch_to_channelized()

    def test_the_flat_family_is_offered_for_conversion(self):
        """A family an earlier flat apply installed is exactly what this feature exists to convert."""
        verdicts = find_convertible_families(self.rule)

        self.assertEqual(len(verdicts), 1)
        self.assertTrue(verdicts[0]["convertible"])
        self.assertEqual(verdicts[0]["reason"], "")

    def test_the_verdict_is_keyed_on_the_ch0_row(self):
        """The ch-0 interface is the physical row and the pk the confirm form submits."""
        verdict = find_convertible_families(self.rule)[0]

        self.assertEqual(verdict["interface"].pk, self._iface("xe-0/0/3:0").pk)
        self.assertEqual(verdict["current_name"], "xe-0/0/3:0")
        self.assertEqual(verdict["module"].pk, self.module.pk)

    def test_the_verdict_names_the_family_as_it_is_and_as_it_would_be(self):
        """An operator confirms a rewrite of named rows, so both name sets have to be on the page."""
        verdict = find_convertible_families(self.rule)[0]

        self.assertEqual(verdict["current_names"], self._flat_names("3"))
        self.assertEqual(verdict["new_names"], ["et-0/0/3", *self._flat_names("3")])

    def test_the_verdict_describes_the_parent_and_its_channels(self):
        """The page must show which row becomes the parent and which channel each name binds to."""
        verdict = find_convertible_families(self.rule)[0]

        self.assertEqual(
            [(detail["role"], detail["channel_id"]) for detail in verdict["name_details"]],
            [("parent", None), ("channel", 1), ("channel", 2), ("channel", 3), ("channel", 4)],
        )

    def test_the_verdict_says_where_the_ch0_configuration_lands(self):
        """The ch-0 row keeps its pk but loses its addresses — the operator has to be told which row gets them."""
        verdict = find_convertible_families(self.rule)[0]

        self.assertIn("xe-0/0/3:0", verdict["metadata_note"])

    def test_the_verdict_warns_that_the_ch0_interface_id_becomes_the_parent(self):
        """Automation keyed on the ch-0 interface id will address the parent afterwards, silently."""
        verdict = find_convertible_families(self.rule)[0]

        self.assertRegex(verdict["metadata_note"], r"(?i)\bid\b")

    def test_finding_the_verdicts_converts_nothing(self):
        """The preflight really performs the conversion to validate it, so the rollback is the feature."""
        pks = dict(Interface.objects.filter(module=self.module).values_list("name", "pk"))

        find_convertible_families(self.rule)

        self._assert_still_flat(self.module, "3")
        self.assertEqual(dict(Interface.objects.filter(module=self.module).values_list("name", "pk")), pks)

    def test_a_rule_without_a_parent_template_offers_no_conversion(self):
        """In a flat family the ch-0 row is the base: without a parent name there is nowhere to put it."""
        self._switch_to_channelized(parent_name_template="")

        self.assertEqual(find_convertible_families(self.rule), [])

    def test_a_flat_rule_offers_no_conversion(self):
        """The rule still describes the flat topology, so its families are not the wrong shape."""
        self.rule.breakout_mode = FLAT
        self.rule.parent_name_template = ""
        self.rule.save()

        self.assertEqual(find_convertible_families(self.rule), [])

    def test_a_rule_without_channels_offers_no_conversion(self):
        """No channel count means no family to identify — there is nothing to convert against."""
        self.rule.channel_count = 0
        self.rule.save()

        self.assertEqual(find_convertible_families(self.rule), [])

    def test_a_port_the_rule_never_touched_is_not_a_conversion_candidate(self):
        """A raw port is an ordinary apply, not a conversion: no flat family exists on it yet."""
        for iface in Interface.objects.filter(module=self.module).exclude(name="xe-0/0/3:0"):
            iface.delete()
        Interface.objects.filter(pk=self._iface("xe-0/0/3:0").pk).update(name="3")

        self.assertEqual(find_convertible_families(self.rule), [])

    def test_a_converted_family_is_not_offered_again(self):
        """It is a native family now; offering it again would invite a second, duplicate conversion."""
        base = self._iface("xe-0/0/3:0")
        convert_flat_families(self.rule, [base.pk])

        self.assertEqual(find_convertible_families(self.rule), [])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ConversionTest(ConversionTestCase):
    """Converting a confirmed family produces the topology NetBox models, row for row."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ConvApply", ["3", "4"])
        cls.module_type = _plain_module_type(manufacturer, "ConvApply-QSFP")
        cls.rule = cls._flat_rule(cls.module_type)

    def setUp(self):
        """Install two flat families so selection can be told apart from 'convert everything'."""
        self.module, self.bay = self._install(self.module_type, "3")
        self.other_module, self.other_bay = self._install(self.module_type, "4")
        self._switch_to_channelized()
        self.base = self._iface("xe-0/0/3:0")
        self.sibling_pks = {name: self._iface(name).pk for name in self._flat_names("3")[1:]}

    def _convert(self, *bases):
        """Convert the families behind *bases* (all convertible families when none are named)."""
        pks = [base.pk for base in bases] if bases else None
        return convert_flat_families(self.rule, pks)

    def test_conversion_builds_the_channelized_family(self):
        """The names stay the ones the rule describes; only the structure under them changes."""
        self._convert(self.base)

        self.assertEqual(self._names(self.module), self._channelized_names("3"))

    def test_the_ch0_row_becomes_the_parent(self):
        """A new parent row would drop the cable, the module link and every reference to that pk."""
        self._convert(self.base)

        parent = self._parent(self.module)
        self.assertEqual(parent.pk, self.base.pk)
        self.assertEqual(parent.name, "et-0/0/3")
        self.assertEqual(parent.channels, 4)
        self.assertEqual(parent.type, self.base.type)
        self.assertEqual(parent.module_id, self.module.pk)

    def test_the_channel_one_child_is_a_new_row_carrying_the_freed_name(self):
        """The ch-0 name describes a channel, not the physical port, so a channel row has to take it."""
        self._convert(self.base)

        child = self._child(self.module, 1)
        self.assertEqual(child.name, "xe-0/0/3:0")
        self.assertNotEqual(child.pk, self.base.pk)
        self.assertEqual(child.type, CHANNEL_TYPE)
        self.assertEqual(child.parent_id, self.base.pk)

    def test_the_new_channel_belongs_to_the_module(self):
        """It is part of what that module installed; an unlinked row would survive the module's removal."""
        self._convert(self.base)

        self.assertEqual(self._child(self.module, 1).module_id, self.module.pk)

    def test_the_other_siblings_are_retyped_in_place(self):
        """Recreating them would discard the descriptions, addresses and tags they carry."""
        self._convert(self.base)

        for channel_id, name in enumerate(self._flat_names("3")[1:], start=2):
            child = self._child(self.module, channel_id)
            self.assertEqual(child.name, name)
            self.assertEqual(child.pk, self.sibling_pks[name])
            self.assertEqual(child.type, CHANNEL_TYPE)
            self.assertEqual(child.parent_id, self.base.pk)

    def test_the_converted_family_validates(self):
        """Whatever the conversion writes has to be a family NetBox itself would accept."""
        self._convert(self.base)

        for iface in Interface.objects.filter(module=self.module):
            iface.full_clean()

    def test_conversion_reports_how_many_families_it_converted(self):
        """The Apply view reports the count back to the operator, so it counts families, not rows."""
        self.assertEqual(self._convert(self.base), 1)

    def test_only_the_selected_family_is_converted(self):
        """Conversion is per family and confirmed per family; an unselected one must not move."""
        self._convert(self.base)

        self._assert_still_flat(self.other_module, "4")

    def test_converting_without_a_selection_converts_every_convertible_family(self):
        """This is the batch the background job runs after the operator confirms the whole rule."""
        self.assertEqual(self._convert(), 2)
        self.assertEqual(self._names(self.module), self._channelized_names("3"))
        self.assertEqual(self._names(self.other_module), self._channelized_names("4"))

    def test_converting_an_empty_selection_does_nothing(self):
        """An empty confirmation is not 'convert everything' — that would be the worst possible default."""
        self.assertEqual(convert_flat_families(self.rule, []), 0)
        self._assert_still_flat(self.module, "3")

    def test_a_second_conversion_run_leaves_the_family_alone(self):
        """Re-running a batch must not build a second family or re-shuffle the first one's rows."""
        self._convert(self.base)
        child_pks = dict(Interface.objects.filter(module=self.module).values_list("name", "pk"))

        self.assertEqual(convert_flat_families(self.rule, [self.base.pk]), 0)
        self.assertEqual(dict(Interface.objects.filter(module=self.module).values_list("name", "pk")), child_pks)

    def test_applying_the_rule_afterwards_changes_nothing(self):
        """The family is native now, so ordinary apply is back to renaming it in place — a no-op here."""
        self._convert(self.base)

        self.assertEqual(apply_rule_to_existing(self.rule), 0)
        self.assertEqual(self._names(self.module), self._channelized_names("3"))

    def test_force_reapply_afterwards_changes_nothing(self):
        """Forcing re-evaluation re-derives the same names against the channel ids the conversion set."""
        self._convert(self.base)

        self.assertEqual(apply_interface_name_rules(self.module, self.bay, force_reapply=True), 0)
        self.assertEqual(self._names(self.module), self._channelized_names("3"))
        self.assertEqual(self._parent(self.module).channels, 4)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class Ch0MetadataSplitTest(ConversionTestCase):
    """The ch-0 row splits in two: the physical port stays put, its logical identity moves to channel 1.

    Everything an operator configured on ``xe-0/0/3:0`` described a 10G channel, not the QSFP cage
    that carries it — so addresses, VLANs, description and tags belong on the new channel-1 child,
    while cable, type, module link and mark_connected stay on the row that is now the parent.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ConvMeta", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ConvMeta-QSFP")
        cls.rule = cls._flat_rule(cls.module_type)
        cls.custom_field = CustomField.objects.create(name="conv_note", type="text")
        cls.custom_field.object_types.set([ObjectType.objects.get_for_model(Interface)])
        cls.untagged = VLAN.objects.create(vid=100, name="ConvMeta-100", site=cls.device.site)
        cls.tagged = VLAN.objects.create(vid=200, name="ConvMeta-200", site=cls.device.site)
        cls.tag = Tag.objects.create(name="ConvMetaTag", slug="convmeta-tag")
        cls.fhrp_group = FHRPGroup.objects.create(group_id=71, protocol="vrrp2")

    def setUp(self):
        """Install the flat family and load ch-0 with the configuration an operator would have put there."""
        self.module, self.bay = self._install(self.module_type, "3")
        self._switch_to_channelized()
        self.base = self._iface("xe-0/0/3:0")
        self.base.description = "uplink to spine"
        self.base.mtu = 9000
        self.base.mode = InterfaceModeChoices.MODE_TAGGED
        self.base.untagged_vlan = self.untagged
        self.base.mark_connected = True
        self.base.custom_field_data = {"conv_note": "keep me"}
        self.base.save()
        self.base.tagged_vlans.set([self.tagged])
        self.base.tags.set([self.tag])
        self.address = IPAddress.objects.create(address="192.0.2.1/24", assigned_object=self.base)
        self.assignment = FHRPGroupAssignment.objects.create(group=self.fhrp_group, interface=self.base, priority=10)

        convert_flat_families(self.rule, [self.base.pk])
        self.parent = self._parent(self.module)
        self.channel = self._child(self.module, 1)

    def test_the_addresses_move_to_the_channel_one_child(self):
        """The address was reachable over one 10G channel, and still is — over that channel's row."""
        self.address.refresh_from_db()

        self.assertEqual(self.address.assigned_object_id, self.channel.pk)
        self.assertEqual(list(self.parent.ip_addresses.all()), [])

    def test_the_description_and_mtu_move_to_the_child(self):
        """They describe the link the channel carries, not the cage the transceiver sits in."""
        self.assertEqual(self.channel.description, "uplink to spine")
        self.assertEqual(self.channel.mtu, 9000)
        self.assertEqual(self.parent.description, "")
        self.assertIsNone(self.parent.mtu)

    def test_the_vlans_and_the_802_1q_mode_move_to_the_child(self):
        """A channelized parent carries no traffic of its own, so a VLAN left on it would be a lie."""
        self.assertEqual(self.channel.mode, InterfaceModeChoices.MODE_TAGGED)
        self.assertEqual(self.channel.untagged_vlan_id, self.untagged.pk)
        self.assertEqual(list(self.channel.tagged_vlans.all()), [self.tagged])
        self.assertEqual(self.parent.mode, "")
        self.assertIsNone(self.parent.untagged_vlan_id)
        self.assertEqual(list(self.parent.tagged_vlans.all()), [])

    def test_the_tags_move_to_the_child(self):
        """Tags drive saved filters and automation aimed at the channel, not at the physical port."""
        self.assertEqual([tag.slug for tag in self.channel.tags.all()], ["convmeta-tag"])
        self.assertEqual(list(self.parent.tags.all()), [])

    def test_the_fhrp_group_assignment_moves_to_the_child(self):
        """The group is a first-hop gateway on the addressed interface, which is now the channel."""
        self.assignment.refresh_from_db()

        self.assertEqual(self.assignment.interface_id, self.channel.pk)

    def test_the_custom_field_values_are_copied_to_the_child(self):
        """Custom fields can mean either thing, so they are copied rather than taken from the parent."""
        self.assertEqual(self.channel.custom_field_data.get("conv_note"), "keep me")
        self.assertEqual(self.parent.custom_field_data.get("conv_note"), "keep me")

    def test_the_parent_keeps_the_physical_facts(self):
        """mark_connected and the interface type describe the cage; moving them would misreport the hardware."""
        self.assertTrue(self.parent.mark_connected)
        self.assertEqual(self.parent.type, self.base.type)
        self.assertEqual(self.parent.module_id, self.module.pk)
        self.assertFalse(self.channel.mark_connected)

    def test_the_split_family_validates(self):
        """The whole point of the split is that both halves are legal rows afterwards."""
        for iface in Interface.objects.filter(module=self.module):
            iface.full_clean()


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ConversionPreflightTest(ConversionTestCase):
    """A family that cannot become a valid channelized family is reported, never half converted."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ConvBlock", ["3", "4"])
        cls.module_type = _plain_module_type(manufacturer, "ConvBlock-QSFP")
        cls.rule = cls._flat_rule(cls.module_type)

    def setUp(self):
        """Install a family to block plus a healthy one beside it, then switch the rule."""
        self.module, self.bay = self._install(self.module_type, "3")
        self.other_module, self.other_bay = self._install(self.module_type, "4")
        self._switch_to_channelized()
        self.base = self._iface("xe-0/0/3:0")

    def _blocked_verdict(self):
        """Return the verdict for the family in bay 3, asserting it is blocked."""
        verdicts = {verdict["current_name"]: verdict for verdict in find_convertible_families(self.rule)}
        verdict = verdicts["xe-0/0/3:0"]
        self.assertFalse(verdict["convertible"], verdict)
        return verdict

    def _cable_up(self, iface):
        """Attach a real cable between *iface* and a spare device interface."""
        peer = Interface.objects.create(device=self.device, name=f"peer-{iface.name}", type=PLAIN_TYPE)
        Cable.objects.create(a_terminations=[iface], b_terminations=[peer])

    def test_a_cabled_sibling_blocks_the_family(self):
        """A channel derives its cable from the parent, so a cabled sibling cannot become one."""
        self._cable_up(self._iface("xe-0/0/3:2"))

        verdict = self._blocked_verdict()

        self.assertIn("xe-0/0/3:2", verdict["reason"])
        self.assertIn("cable", verdict["reason"].lower())

    def test_a_cabled_sibling_is_not_partially_converted(self):
        """Blocking after the parent is written would leave the family in neither topology."""
        self._cable_up(self._iface("xe-0/0/3:2"))

        self.assertEqual(convert_flat_families(self.rule, [self.base.pk]), 0)
        self._assert_still_flat(self.module, "3")

    def test_an_occupied_parent_target_blocks_the_family(self):
        """The parent needs its own name; taking one already in use would just fail the save."""
        Interface.objects.create(device=self.device, name="et-0/0/3", type=PLAIN_TYPE)

        verdict = self._blocked_verdict()

        self.assertIn("et-0/0/3", verdict["reason"])

    def test_an_occupied_parent_target_is_not_partially_converted(self):
        """Nothing is written before every name the family needs is known to be free."""
        Interface.objects.create(device=self.device, name="et-0/0/3", type=PLAIN_TYPE)

        self.assertEqual(convert_flat_families(self.rule, [self.base.pk]), 0)
        self._assert_still_flat(self.module, "3")

    def test_a_missing_sibling_blocks_the_family(self):
        """A partial family is someone else's edit; guessing the rest of it is not the plugin's call."""
        self._iface("xe-0/0/3:2").delete()

        verdict = self._blocked_verdict()

        self.assertIn("xe-0/0/3:2", verdict["reason"])

    def test_a_missing_sibling_is_not_partially_converted(self):
        """Converting three of four rows would silently drop a channel from the family."""
        self._iface("xe-0/0/3:2").delete()

        self.assertEqual(convert_flat_families(self.rule, [self.base.pk]), 0)
        self.assertFalse(Interface.objects.filter(module=self.module, channels__isnull=False).exists())
        self.assertFalse(Interface.objects.filter(module=self.module, channel_id__isnull=False).exists())

    def test_a_sibling_that_cannot_be_a_channel_blocks_the_family(self):
        """The dry-run full_clean() is what inherits upstream's rules, including ones added later."""
        sibling = self._iface("xe-0/0/3:1")
        sibling.channels = 2
        sibling.save()

        verdict = self._blocked_verdict()

        self.assertIn(CHANNELIZED_CHANNEL_ERROR, verdict["reason"])

    def test_a_sibling_that_cannot_be_a_channel_is_not_partially_converted(self):
        """The dry run happens inside a rolled-back transaction, so a late failure still writes nothing."""
        sibling = self._iface("xe-0/0/3:1")
        sibling.channels = 2
        sibling.save()

        self.assertEqual(convert_flat_families(self.rule, [self.base.pk]), 0)
        self.assertEqual(self._names(self.module), self._flat_names("3"))
        self.assertIsNone(self._iface("xe-0/0/3:0").channels)

    def test_a_blocked_family_is_reported_to_the_caller(self):
        """The Apply view tells the operator how many families it refused, not just that nothing happened."""
        self._cable_up(self._iface("xe-0/0/3:2"))
        conflicts: list = []

        convert_flat_families(self.rule, [self.base.pk], conflicts=conflicts)

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["interface_pk"], self.base.pk)
        self.assertEqual(conflicts[0]["attempted_name"], "et-0/0/3")

    def test_a_blocked_family_does_not_stop_the_batch(self):
        """One unconvertible family must not cost the operator every other family in the run."""
        self._cable_up(self._iface("xe-0/0/3:2"))

        self.assertEqual(convert_flat_families(self.rule), 1)
        self._assert_still_flat(self.module, "3")
        self.assertEqual(self._names(self.other_module), self._channelized_names("4"))


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ConversionApplyViewTest(ConversionTestCase):
    """The Apply page offers conversion as its own, separately confirmed action."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="convview", password=TEST_PASSWORD, email="convview@example.com"
        )
        manufacturer, cls.device = _build_device("ConvView", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ConvView-QSFP")
        cls.rule = cls._flat_rule(cls.module_type)

    def setUp(self):
        """Install the flat family, switch the rule and log in for the interactive flow."""
        self.module, self.bay = self._install(self.module_type, "3")
        self._switch_to_channelized()
        self.base = self._iface("xe-0/0/3:0")
        self.client.force_login(self.superuser)

    def _url(self):
        """Return the Apply detail URL for the rule under test."""
        return reverse(
            "plugins:netbox_interface_name_rules:interfacenamerule_apply_detail", kwargs={"pk": self.rule.pk}
        )

    @staticmethod
    def _messages(response):
        """Return (level_tag, text) for every message the request produced."""
        return [(message.level_tag, str(message)) for message in get_messages(response.wsgi_request)]

    def test_the_page_offers_the_conversion(self):
        """The operator has no other way to discover that these families can be converted at all."""
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        verdicts = response.context["conversions"]
        self.assertEqual([verdict["current_name"] for verdict in verdicts], ["xe-0/0/3:0"])

    def test_the_page_renders_a_confirm_action_of_its_own(self):
        """Conversion rewrites rows; it must never ride along on the ordinary apply button."""
        content = self.client.get(self._url()).content.decode()

        self.assertIn('value="convert"', content)
        self.assertIn('name="convert_ids"', content)

    def test_the_page_shows_the_family_it_would_convert(self):
        """Names on both sides of the change are what makes the confirmation informed."""
        content = self.client.get(self._url()).content.decode()

        self.assertIn("xe-0/0/3:0", content)
        self.assertIn("et-0/0/3", content)

    def test_the_page_states_where_the_ch0_configuration_goes(self):
        """The surprising part of the conversion is the one the page has to spell out."""
        response = self.client.get(self._url())
        note = response.context["conversions"][0]["metadata_note"]

        self.assertIn(escape(note), response.content.decode())

    def test_a_flat_rule_is_offered_no_conversion(self):
        """The rule still describes the flat topology; converting away from it was never asked for."""
        self.rule.breakout_mode = FLAT
        self.rule.parent_name_template = ""
        self.rule.save()

        response = self.client.get(self._url())

        self.assertFalse(response.context["conversions"])
        self.assertNotIn('value="convert"', response.content.decode())

    def test_a_rule_without_a_parent_template_is_offered_no_conversion(self):
        """Without a parent name there is no row for the ch-0 interface to become."""
        self._switch_to_channelized(parent_name_template="")

        response = self.client.get(self._url())

        self.assertFalse(response.context["conversions"])
        self.assertNotIn('value="convert"', response.content.decode())

    def test_the_ordinary_apply_action_never_converts(self):
        """Applying a rule is a rename; it must not rewrite an installed family behind the operator."""
        response = self.client.post(self._url(), {"action": "apply", "interface_ids": [str(self.base.pk)]})

        self.assertEqual(response.status_code, 302)
        self._assert_still_flat(self.module, "3")

    def test_confirming_the_conversion_converts_the_selected_family(self):
        """The confirm action is the only path that rewrites the family, and it does rewrite it."""
        response = self.client.post(self._url(), {"action": "convert", "convert_ids": [str(self.base.pk)]})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._names(self.module), self._channelized_names("3"))
        self.assertTrue(any(level == "success" for level, _ in self._messages(response)))

    def test_confirming_nothing_converts_nothing(self):
        """A submit with every box cleared is not a request to convert the whole rule."""
        response = self.client.post(self._url(), {"action": "convert"})

        self._assert_still_flat(self.module, "3")
        self.assertTrue(any(level == "warning" for level, _ in self._messages(response)))

    def test_a_blocked_family_is_reported_on_screen(self):
        """Silence would read as success; the operator has to learn the family was refused."""
        peer = Interface.objects.create(device=self.device, name="convview-peer", type=PLAIN_TYPE)
        Cable.objects.create(a_terminations=[self._iface("xe-0/0/3:2")], b_terminations=[peer])

        response = self.client.post(self._url(), {"action": "convert", "convert_ids": [str(self.base.pk)]})

        self._assert_still_flat(self.module, "3")
        self.assertTrue(any(level == "warning" for level, _ in self._messages(response)))

    def test_the_background_action_enqueues_a_conversion_job(self):
        """A large confirmed batch runs on the worker, the same way a large apply does."""
        before = set(Job.objects.values_list("pk", flat=True))

        response = self.client.post(self._url(), {"action": "convert_background"})

        self.assertEqual(response.status_code, 302)
        enqueued = Job.objects.exclude(pk__in=before)
        self.assertEqual(len(enqueued), 1)
        self.assertIn("onvert", enqueued[0].name)

    def test_the_conversion_needs_the_interface_change_permission(self):
        """It rewrites interfaces, so it is gated exactly like the apply it sits next to."""
        User.objects.create_user(username="convview-noperm", password=TEST_PASSWORD)
        self.client.login(username="convview-noperm", password=TEST_PASSWORD)

        response = self.client.post(self._url(), {"action": "convert", "convert_ids": [str(self.base.pk)]})

        self.assertEqual(response.status_code, 403)
        self._assert_still_flat(self.module, "3")


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ConversionJobTest(ConversionTestCase):
    """The background job runs the batch an operator confirmed for the whole rule."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ConvJob", ["3", "4"])
        cls.module_type = _plain_module_type(manufacturer, "ConvJob-QSFP")
        cls.rule = cls._flat_rule(cls.module_type)

    def setUp(self):
        """Install two flat families and switch the rule to the channelized topology."""
        self.module, self.bay = self._install(self.module_type, "3")
        self.other_module, self.other_bay = self._install(self.module_type, "4")
        self._switch_to_channelized()

    def _run_job(self, **kwargs):
        """Run the conversion job against a real Job row, the way the worker does."""
        from netbox_interface_name_rules.jobs import ConvertFlatFamiliesJob

        job = Job.objects.create(name="Convert flat families (test)", job_id=uuid.uuid4())
        ConvertFlatFamiliesJob(job).run(rule_id=self.rule.pk, **kwargs)
        return job

    def test_the_job_converts_every_convertible_family_of_the_rule(self):
        """That is the batch the page offers when the operator asks for all of them."""
        self._run_job()

        self.assertEqual(self._names(self.module), self._channelized_names("3"))
        self.assertEqual(self._names(self.other_module), self._channelized_names("4"))

    def test_the_job_leaves_a_blocked_family_flat(self):
        """A batch is still per family: one refusal converts the others and reports the one it skipped."""
        peer = Interface.objects.create(device=self.device, name="convjob-peer", type=PLAIN_TYPE)
        Cable.objects.create(a_terminations=[self._iface("xe-0/0/3:2")], b_terminations=[peer])

        self._run_job()

        self._assert_still_flat(self.module, "3")
        self.assertEqual(self._names(self.other_module), self._channelized_names("4"))

    def test_a_missing_rule_is_logged_rather_than_raised(self):
        """A rule deleted between enqueue and execution must not fail the worker's job."""
        from netbox_interface_name_rules.jobs import ConvertFlatFamiliesJob

        job = Job.objects.create(name="Convert flat families (test)", job_id=uuid.uuid4())
        rule_id = self.rule.pk
        self.rule.delete()

        ConvertFlatFamiliesJob(job).run(rule_id=rule_id)

        self._assert_still_flat(self.module, "3")


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class FlatToChannelizedJuniperE2ETest(ConversionTestCase):
    """The scenario the feature exists for: a cabled, addressed Juniper 4x10G breakout, converted.

    ``xe-0/0/3:0`` is the QSFP+ port itself — cabled, addressed and described — with three sibling
    rows beside it.  After conversion it is ``et-0/0/3``, still the same cabled row, and the
    ``xe-0/0/3:0`` name (with everything logical that hung off it) belongs to channel 1.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ConvJnpr", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "QSFP-4X10G-LR-CONV")
        cls.rule = cls._flat_rule(cls.module_type, device_type=cls.device.device_type)
        cls.tag = Tag.objects.create(name="ConvJnprTag", slug="convjnpr-tag")

    def setUp(self):
        """Install the flat family the old rule produced, then cable and configure its ch-0 row."""
        self.module, self.bay = self._install(self.module_type, "3")
        self.base = self._iface("xe-0/0/3:0")
        self.base_pk = self.base.pk
        self.sibling_pks = {name: self._iface(name).pk for name in self._flat_names("3")[1:]}
        peer = Interface.objects.create(device=self.device, name="convjnpr-peer", type=PLAIN_TYPE)
        self.cable = Cable.objects.create(a_terminations=[self.base], b_terminations=[peer])
        self.base.refresh_from_db()
        self.base.description = "to spine1 xe-0/0/9"
        self.base.save()
        self.base.tags.set([self.tag])
        self.address = IPAddress.objects.create(address="198.51.100.9/31", assigned_object=self.base)

    def test_the_family_starts_out_flat_and_cabled(self):
        """The precondition the scenario rests on: four plain rows, the cable on the ch-0 one."""
        self._assert_still_flat(self.module, "3")
        self.assertEqual(self._iface("xe-0/0/3:0").cable_id, self.cable.pk)

    def test_the_operator_is_offered_the_conversion_after_switching_the_rule(self):
        """Switching the rule alone changes nothing — the page is where the conversion is offered."""
        self._switch_to_channelized()

        verdicts = find_convertible_families(self.rule)

        self.assertEqual(len(verdicts), 1)
        self.assertTrue(verdicts[0]["convertible"], verdicts[0]["reason"])
        self._assert_still_flat(self.module, "3")

    def test_the_conversion_produces_the_juniper_family(self):
        """One et- parent over four xe- channels: the names Juniper uses, the structure NetBox models."""
        self._switch_to_channelized()

        convert_flat_families(self.rule, [self.base_pk])

        self.assertEqual(self._names(self.module), ["et-0/0/3", *self._flat_names("3")])
        parent = self._parent(self.module)
        self.assertEqual(parent.pk, self.base_pk)
        self.assertEqual(parent.name, "et-0/0/3")
        self.assertEqual(parent.channels, 4)
        self.assertEqual(parent.cable_id, self.cable.pk)

    def test_the_channels_map_onto_the_old_sibling_rows(self):
        """channel_id 1..4 over :0..:3, with :0 the new row and :1..:3 the ones that already existed."""
        self._switch_to_channelized()

        convert_flat_families(self.rule, [self.base_pk])

        children = [self._child(self.module, channel_id) for channel_id in range(1, 5)]
        self.assertEqual([child.name for child in children], self._flat_names("3"))
        self.assertNotEqual(children[0].pk, self.base_pk)
        self.assertEqual(
            [child.pk for child in children[1:]],
            [self.sibling_pks[name] for name in self._flat_names("3")[1:]],
        )
        self.assertFalse(any(child.cable_id for child in children))

    def test_the_ch0_configuration_follows_the_name_onto_channel_one(self):
        """The address and description described the 10G link, and that link is now channel 1."""
        self._switch_to_channelized()

        convert_flat_families(self.rule, [self.base_pk])

        channel_one = self._child(self.module, 1)
        self.address.refresh_from_db()
        self.assertEqual(self.address.assigned_object_id, channel_one.pk)
        self.assertEqual(channel_one.description, "to spine1 xe-0/0/9")
        self.assertEqual([tag.slug for tag in channel_one.tags.all()], ["convjnpr-tag"])

    def test_the_converted_family_validates(self):
        """A conversion that leaves rows NetBox would reject is not a conversion at all."""
        self._switch_to_channelized()

        convert_flat_families(self.rule, [self.base_pk])

        for iface in Interface.objects.filter(module=self.module):
            iface.full_clean()


@skipIf(supports_channelization(), REQUIRES_NO_CHANNELIZATION)
class ConversionWithoutSupportTest(ConversionTestCase):
    """Where NetBox has no channel model there is no topology to convert to, so nothing is offered."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="convnosup", password=TEST_PASSWORD, email="convnosup@example.com"
        )
        manufacturer, cls.device = _build_device("ConvNoSup", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ConvNoSup-QSFP")
        cls.rule = cls._flat_rule(cls.module_type)

    def setUp(self):
        """Install the flat family and switch the rule, as an operator on an older release might."""
        self.module, self.bay = self._install(self.module_type, "3")
        self._switch_to_channelized()
        self.base = self._iface("xe-0/0/3:0")

    def test_no_family_is_offered_for_conversion(self):
        """There is no channelized topology on this release, so no family can be converted into one."""
        self.assertEqual(find_convertible_families(self.rule), [])

    def test_converting_refuses_gracefully_and_says_so(self):
        """A refusal has to be visible: a silent zero looks exactly like 'nothing needed converting'."""
        with self.assertLogs(PLUGIN_LOGGER, level="WARNING") as logs:
            converted = convert_flat_families(self.rule, [self.base.pk])

        self.assertEqual(converted, 0)
        self.assertEqual(self._names(self.module), self._flat_names("3"))
        self.assertTrue(any("channeliz" in line.lower() for line in logs.output), logs.output)

    def test_the_apply_page_has_no_conversion_section(self):
        """Offering an action this release cannot perform is worse than not mentioning it."""
        self.client.force_login(self.superuser)
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_apply_detail", kwargs={"pk": self.rule.pk})

        response = self.client.get(url)

        self.assertFalse(response.context["conversions"])
        self.assertNotIn('value="convert"', response.content.decode())
