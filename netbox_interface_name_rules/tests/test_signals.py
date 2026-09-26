# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the librenms predict receiver in ``signals.py``.

The rename-trigger receivers are tested through real saves in ``test_rename_triggers.py``.
"""

import importlib.util
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from dcim.models import DeviceType, Manufacturer, Module, ModuleBay, ModuleBayTemplate, ModuleType
from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.helpers import make_device

_librenms_available = importlib.util.find_spec("netbox_librenms_plugin") is not None


class LibrenmsPredictReceiverTest(TestCase):
    """Receiver bridging netbox-librenms-plugin's predict_module_interface_names signal."""

    @classmethod
    def setUpTestData(cls):
        """Bay + device fixture for predict-receiver tests."""
        manufacturer = Manufacturer.objects.create(name="PredMfg", slug="predmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="PRED-Dev", slug="pred-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="PRED-SFP", part_number="PRED-SFP")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="PredBay 0", position="c9")
        cls.device = make_device("Pred", cls.device_type, name="pred-test-01")
        cls.bay = ModuleBay.objects.get(device=cls.device, name="PredBay 0")

    @skipUnless(_librenms_available, "netbox_librenms_plugin not installed")
    def test_receiver_returns_rewritten_names_via_signal(self):
        """Sending the librenms-plugin signal returns names processed by the rule engine."""
        from netbox_librenms_plugin.signals import predict_module_interface_names

        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="{base}/1",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)

        responses = predict_module_interface_names.send(sender=Module, device=self.device, module=module, names=["c9"])
        # Find our receiver's response by its dispatch_uid match (only one receiver expected).
        returned_lists = [r for _, r in responses if r is not None]
        self.assertEqual(returned_lists, [["c9/1"]])

    @skipUnless(_librenms_available, "netbox_librenms_plugin not installed")
    def test_receiver_returns_none_when_module_bay_missing(self):
        """A module without a module_bay yields None from the receiver."""
        from netbox_librenms_plugin.signals import predict_module_interface_names

        bare_module = MagicMock(spec=[])  # No module_bay attribute
        responses = predict_module_interface_names.send(
            sender=Module, device=self.device, module=bare_module, names=["c9"]
        )
        # Our receiver returns None; any other receivers (none expected) may return something.
        from netbox_interface_name_rules.signals import (
            on_librenms_predict_module_interface_names,
        )

        ours = [r for recv, r in responses if recv is on_librenms_predict_module_interface_names]
        self.assertEqual(ours, [None])

    def test_receiver_returns_none_when_engine_raises(self):
        """Engine exceptions are swallowed and the receiver returns None (no rewrite)."""
        from netbox_interface_name_rules.signals import (
            on_librenms_predict_module_interface_names,
        )

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        with patch(
            "netbox_interface_name_rules.engine.predict_rule_output",
            side_effect=RuntimeError("boom"),
        ):
            result = on_librenms_predict_module_interface_names(
                sender=Module, device=self.device, module=module, names=["c9"]
            )
        self.assertIsNone(result)
