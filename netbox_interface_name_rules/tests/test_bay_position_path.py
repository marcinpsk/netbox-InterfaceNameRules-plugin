# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""End-to-end tests of ``{bay_position_path}``: a breakout optic in a bay whose position is an interface name.

A Juniper MX10008 line card gives each transceiver bay the position ``et-<FPC>/<PIC>/<port>``. A 4x10G
breakout optic in bay ``et-0/0/3`` becomes the parent ``et-0/0/3`` with the channels ``xe-0/0/3:0`` to
``xe-0/0/3:3``, which only a variable without the ``et-`` prefix can spell.
"""

from unittest import skipUnless

from dcim.models import Interface, InterfaceTemplate, Module, ModuleBay, ModuleBayTemplate, Platform
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_interface_name_rules.engine import (
    apply_rule_to_existing,
    find_interfaces_for_rule,
    predict_rule_output,
    supports_channelization,
)
from netbox_interface_name_rules.family import FamilyStatus
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import (
    CHANNEL_TYPE,
    CHANNELIZED,
    FLAT,
    REQUIRES_CHANNELIZATION,
    REQUIRES_MODULE_PLACEHOLDER,
    TEST_PASSWORD,
    make_device,
    make_device_type,
    make_manufacturer,
    make_module_type,
    names_of,
    slug_for,
    supports_module_placeholder,
)
from netbox_interface_name_rules.tests.trigger_cases import journal

NAME_TEMPLATE = "xe-{bay_position_path}:{channel}"
CHANNELS = ["xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"]
NO_DIGIT = "{bay_position_path} is not available because the module bay position has no digit"


class MX10008Topology:
    """An MX10008 with a JNP10K-LC9600 line card in FPC 0, and a platform-scoped rule for 4x10G optics."""

    breakout_mode = FLAT

    @classmethod
    def setUpTestData(cls):
        prefix = cls.__name__
        manufacturer = make_manufacturer(prefix)
        device_type = make_device_type(manufacturer, prefix, model=f"{prefix} MX10008")
        ModuleBayTemplate.objects.create(device_type=device_type, name="FPC0", position="0")
        ModuleBayTemplate.objects.create(device_type=device_type, name="Bay A", position="A")
        line_card_type = make_module_type(manufacturer, prefix, model=f"{prefix} JNP10K-LC9600")
        ModuleBayTemplate.objects.create(module_type=line_card_type, name="PIC0 Port 3", position="et-{module}/0/3")
        cls.optic_type = make_module_type(manufacturer, prefix, model="QSFPP-4X10GE-SR")
        InterfaceTemplate.objects.create(module_type=cls.optic_type, name="{module}", type="40gbase-x-qsfpp")
        platform = Platform.objects.create(name=f"{prefix} Junos", slug=slug_for(prefix, "junos"))
        cls.device = make_device(prefix, device_type, platform=platform)
        line_card = Module.objects.create(
            device=cls.device,
            module_bay=ModuleBay.objects.get(device=cls.device, name="FPC0"),
            module_type=line_card_type,
        )
        cls.optic_bay = ModuleBay.objects.get(device=cls.device, module=line_card, name="PIC0 Port 3")
        cls.no_digit_bay = ModuleBay.objects.get(device=cls.device, name="Bay A")
        InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFPP-4X10GE-.*",
            platform=platform,
            name_template=NAME_TEMPLATE,
            parent_name_template="{bay_position}" if cls.breakout_mode == CHANNELIZED else "",
            breakout_mode=cls.breakout_mode,
            channel_count=4,
            channel_start=0,
        )

    def _install(self, bay):
        """Install the optic into *bay* and run the rename that NetBox's commit triggers."""
        with self.captureOnCommitCallbacks(execute=True):
            return Module.objects.create(device=self.device, module_bay=bay, module_type=self.optic_type)

    def test_the_transceiver_bay_has_the_composed_position(self):
        """Guard the fixture: without the composed position the other tests assert nothing."""
        self.assertEqual(self.optic_bay.position, "et-0/0/3")


@skipUnless(supports_module_placeholder(), REQUIRES_MODULE_PLACEHOLDER)
class BayPositionPathFlatTest(MX10008Topology, TestCase):
    """A flat breakout rule names the four siblings from the path of the bay position."""

    breakout_mode = FLAT

    def test_the_optic_becomes_four_flat_xe_siblings(self):
        module = self._install(self.optic_bay)

        self.assertEqual(names_of(module), CHANNELS)

    def test_apply_rules_names_the_variable_a_position_without_a_digit_lacks(self):
        module = self._install(self.no_digit_bay)
        rule = InterfaceNameRule.objects.get()
        user = get_user_model().objects.create_superuser(
            username="bay-path-applier", password=TEST_PASSWORD, email="bay-path-applier@example.com"
        )
        self.client.force_login(user)
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_apply_detail", args=[rule.pk])

        preview = self.client.get(url)
        with self.assertLogs("netbox_interface_name_rules.family", level="WARNING") as logs:
            self.client.post(url, {"action": "apply", "interface_ids": [Interface.objects.get(module=module).pk]})

        self.assertContains(preview, f"&lt;error: {NO_DIGIT}&gt;")
        self.assertIn(f"failed to evaluate the family names: {NO_DIGIT}", "\n".join(logs.output))
        self.assertEqual(names_of(module), ["A"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class BayPositionPathChannelizedTest(MX10008Topology, TestCase):
    """A channelized rule keeps the et- parent and names its channels from the path of the bay position."""

    breakout_mode = CHANNELIZED

    def test_the_optic_becomes_an_et_parent_with_four_xe_channels(self):
        module = self._install(self.optic_bay)

        parent = Interface.objects.get(module=module, channels__isnull=False)
        self.assertEqual((parent.name, parent.channels), ("et-0/0/3", 4))
        self.assertEqual(
            list(
                Interface.objects.filter(module=module, parent=parent)
                .order_by("channel_id")
                .values_list("channel_id", "name", "type")
            ),
            [(channel_id, name, CHANNEL_TYPE) for channel_id, name in enumerate(CHANNELS, start=1)],
        )

    def test_a_position_without_a_digit_keeps_the_name_and_reports_the_variable(self):
        module = self._install(self.no_digit_bay)

        self.assertEqual(names_of(module), ["A"])
        (entry,) = journal(module)
        self.assertIn(f"- `A`: {NO_DIGIT} (unresolved variable)", entry.comments)

    def test_the_preview_prediction_and_batch_apply_give_a_position_without_a_digit_no_name(self):
        module = self._install(self.no_digit_bay)
        rule = InterfaceNameRule.objects.get()

        (entry,), _total = find_interfaces_for_rule(rule)
        predicted = predict_rule_output(module, self.no_digit_bay, ["A"])
        applied = apply_rule_to_existing(rule)

        self.assertEqual(entry["new_names"], [f"<error: {NO_DIGIT}>"])
        self.assertEqual(predicted, ["A"])
        (family,) = applied.families
        self.assertEqual(family.status, FamilyStatus.FAILED)
        self.assertEqual(family.reason, f"failed to evaluate the family names: {NO_DIGIT}")
        self.assertEqual(names_of(module), ["A"])

    def test_the_rule_tester_previews_the_family_and_reports_a_position_without_a_digit(self):
        user = get_user_model().objects.create_superuser(
            username="bay-path-tester", password=TEST_PASSWORD, email="bay-path-tester@example.com"
        )
        self.client.force_login(user)
        form = {
            "name_template": NAME_TEMPLATE,
            "parent_name_template": "{bay_position}",
            "breakout_mode": CHANNELIZED,
            "channel_count": "4",
            "channel_start": "0",
        }
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")

        previewed = self.client.post(url, {**form, "var_bay_position": "et-0/0/3"})
        refused = self.client.post(url, {**form, "var_bay_position": "A"})

        self.assertEqual(
            [row["result"] for row in previewed.context["preview_results"]],
            ["et-0/0/3", *CHANNELS],
        )
        self.assertIsNone(refused.context["preview_results"])
        self.assertEqual(refused.context["error"], NO_DIGIT)
        self.assertContains(refused, NO_DIGIT)
