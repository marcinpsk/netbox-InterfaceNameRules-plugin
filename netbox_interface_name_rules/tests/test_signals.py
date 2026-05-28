# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for Django signal handlers: module post_save and device VC membership changes."""

import importlib.util
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    Site,
    VirtualChassis,
)
from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.signals import (
    _apply_rules_deferred,
    _apply_rules_for_device_deferred,
    on_device_pre_save,
    on_device_saved,
    on_module_pre_save,
    on_module_saved,
)

_librenms_available = importlib.util.find_spec("netbox_librenms_plugin") is not None


class SignalModuleHandlerTest(TestCase):
    """Test the module post_save signal handler and deferred rename helper."""

    @classmethod
    def setUpTestData(cls):
        """Create basic fixture for module signal tests."""
        manufacturer = Manufacturer.objects.create(name="SigMfg", slug="sigmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="SIG-Dev", slug="sig-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="SIG-SFP", part_number="SIG-SFP")
        cls.module_type2 = ModuleType.objects.create(
            manufacturer=manufacturer, model="SIG-QSFP", part_number="SIG-QSFP"
        )
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="SigBay 0", position="0")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="SigBay 1", position="1")
        role = DeviceRole.objects.create(name="SigRole", slug="sigrole")
        site = Site.objects.create(name="SigSite", slug="sigsite")
        cls.device = Device.objects.create(name="sig-test-01", device_type=cls.device_type, role=role, site=site)
        cls.bay0 = ModuleBay.objects.get(device=cls.device, name="SigBay 0")
        cls.bay1 = ModuleBay.objects.get(device=cls.device, name="SigBay 1")

    # ------------------------------------------------------------------
    # _apply_rules_deferred
    # ------------------------------------------------------------------

    def test_deferred_apply_renames_interface(self):
        """_apply_rules_deferred calls apply_interface_name_rules and renames."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        _apply_rules_deferred(module.pk, self.bay0.pk)

        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

    def test_deferred_apply_missing_module_no_error(self):
        """_apply_rules_deferred handles Module.DoesNotExist gracefully."""
        _apply_rules_deferred(999999, 999999)  # Non-existent PKs — should not raise

    def test_deferred_apply_no_rule_no_rename(self):
        """_apply_rules_deferred leaves interface unchanged when no rule matches."""
        module = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="1", type="10gbase-x-sfpp")

        _apply_rules_deferred(module.pk, self.bay1.pk)

        iface.refresh_from_db()
        self.assertEqual(iface.name, "1")

    def test_deferred_apply_with_force_reapply(self):
        """_apply_rules_deferred with force_reapply=True re-applies rules."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        # Interface with non-raw name — won't be renamed without force_reapply
        iface = Interface.objects.create(device=self.device, module=module, name="old-et", type="10gbase-x-sfpp")

        _apply_rules_deferred(module.pk, self.bay0.pk, force_reapply=True)

        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

    # ------------------------------------------------------------------
    # on_module_pre_save
    # ------------------------------------------------------------------

    def test_pre_save_new_module_sets_none(self):
        """on_module_pre_save sets _prev_module_type_id=None for new (unsaved) module."""
        module = Module(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        on_module_pre_save(Module, module)
        self.assertIsNone(module._prev_module_type_id)

    def test_pre_save_existing_module_captures_type(self):
        """on_module_pre_save captures existing module_type_id for saved module."""
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        on_module_pre_save(Module, module)
        self.assertEqual(module._prev_module_type_id, self.module_type.pk)

    # ------------------------------------------------------------------
    # on_module_saved
    # ------------------------------------------------------------------

    def test_module_saved_created_returns_early(self):
        """on_module_saved with created=True schedules deferred callback (no error)."""
        module = Module.objects.create(device=self.device, module_bay=self.bay1, module_type=self.module_type)
        # created=True path schedules on_commit; we verify no exception is raised
        on_module_saved(Module, module, created=True)

    def test_module_saved_no_bay_returns_early(self):
        """on_module_saved with no module_bay exits cleanly."""
        module = Module(device=self.device, module_type=self.module_type)
        module.module_bay = None
        on_module_saved(Module, module, created=False)  # Should not raise

    def test_module_saved_type_unchanged_returns_early(self):
        """on_module_saved skips re-apply when module_type is unchanged."""
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        module._prev_module_type_id = module.module_type_id  # Same type
        on_module_saved(Module, module, created=False)  # Should not raise or rename

    def test_module_saved_type_changed_schedules_reapply(self):
        """on_module_saved schedules deferred reapply when module type changes."""
        module = Module.objects.create(device=self.device, module_bay=self.bay0, module_type=self.module_type)
        module._prev_module_type_id = self.module_type2.pk  # Different type
        # Should schedule on_commit callback — no error raised
        on_module_saved(Module, module, created=False)


class SignalDeviceHandlerTest(TestCase):
    """Test the device VC change signal handler and deferred rename helper."""

    @classmethod
    def setUpTestData(cls):
        """Create device with VC and module + interface for device signal tests."""
        manufacturer = Manufacturer.objects.create(name="SigDevMfg", slug="sigdevmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="SigDev-Switch", slug="sigdev-switch")
        ModuleBayTemplate.objects.create(device_type=device_type, name="SDBay 0", position="0")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="SigDev-SFP", part_number="SigDev-SFP"
        )
        role = DeviceRole.objects.create(name="SigDevRole", slug="sigdevrole")
        site = Site.objects.create(name="SigDevSite", slug="sigdevsite")

        cls.vc = VirtualChassis.objects.create(name="sigdev-vc")
        cls.device = Device.objects.create(
            name="sigdev-sw1",
            device_type=device_type,
            role=role,
            site=site,
            virtual_chassis=cls.vc,
            vc_position=1,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="SDBay 0")
        cls.module = Module.objects.create(device=cls.device, module_bay=cls.bay, module_type=cls.module_type)

    # ------------------------------------------------------------------
    # on_device_pre_save
    # ------------------------------------------------------------------

    def test_pre_save_new_device_sets_none(self):
        """on_device_pre_save stores None for a brand-new unsaved device."""
        new_dev = Device(name="new-dev")
        on_device_pre_save(Device, new_dev)
        self.assertIsNone(new_dev._prev_virtual_chassis_id)
        self.assertIsNone(new_dev._prev_vc_position)

    def test_pre_save_existing_device_captures_values(self):
        """on_device_pre_save captures virtual_chassis_id and vc_position from DB."""
        on_device_pre_save(Device, self.device)
        self.assertEqual(self.device._prev_virtual_chassis_id, self.vc.pk)
        self.assertEqual(self.device._prev_vc_position, 1)

    # ------------------------------------------------------------------
    # on_device_saved
    # ------------------------------------------------------------------

    def test_device_saved_created_returns_early(self):
        """on_device_saved with created=True exits immediately."""
        self.device._prev_virtual_chassis_id = None
        on_device_saved(Device, self.device, created=True)  # No error

    def test_device_saved_no_change_returns_early(self):
        """on_device_saved when VC and position are unchanged does nothing."""
        self.device._prev_virtual_chassis_id = self.vc.pk
        self.device._prev_vc_position = 1
        on_device_saved(Device, self.device, created=False)  # No error

    def test_device_saved_removed_from_vc_returns_early(self):
        """on_device_saved when device leaves VC (new_vc=None) does not schedule rename."""
        self.device._prev_virtual_chassis_id = self.vc.pk
        self.device._prev_vc_position = 1
        # Simulate device removed from VC
        self.device.virtual_chassis = None
        self.device.virtual_chassis_id = None
        on_device_saved(Device, self.device, created=False)  # No error, no rename scheduled
        # Restore for other tests
        self.device.virtual_chassis = self.vc
        self.device.virtual_chassis_id = self.vc.pk

    def test_device_saved_vc_position_change_schedules_rename(self):
        """on_device_saved when vc_position changes schedules deferred rename."""
        self.device._prev_virtual_chassis_id = self.vc.pk
        self.device._prev_vc_position = 99  # Was 99, now 1
        # Should schedule on_commit — no error raised
        on_device_saved(Device, self.device, created=False)

    # ------------------------------------------------------------------
    # _apply_rules_for_device_deferred
    # ------------------------------------------------------------------

    def test_deferred_device_missing_no_error(self):
        """_apply_rules_for_device_deferred handles Device.DoesNotExist gracefully."""
        _apply_rules_for_device_deferred(999999)  # Non-existent PK — should not raise

    def test_deferred_device_module_rules_applied(self):
        """_apply_rules_for_device_deferred re-applies module rules with force_reapply."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        iface = Interface.objects.create(device=self.device, module=self.module, name="old-sig", type="10gbase-x-sfpp")

        _apply_rules_for_device_deferred(self.device.pk)

        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

    def test_deferred_device_no_modules_no_error(self):
        """_apply_rules_for_device_deferred with device having no modules runs without error."""
        manufacturer = Manufacturer.objects.create(name="SigDevMfg2", slug="sigdevmfg2")
        dt = DeviceType.objects.create(manufacturer=manufacturer, model="SD2-Switch", slug="sd2-switch")
        role = DeviceRole.objects.create(name="SD2Role", slug="sd2role")
        site = Site.objects.create(name="SD2Site", slug="sd2site")
        vc = VirtualChassis.objects.create(name="sd2-vc")
        device_no_modules = Device.objects.create(
            name="sd2-sw-nomod",
            device_type=dt,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=2,
        )
        _apply_rules_for_device_deferred(device_no_modules.pk)  # Should not raise


# ---------------------------------------------------------------------------
# signals.py — exception paths (lines 39-40, 69, 115-116, 143-145, 214-234)
# ---------------------------------------------------------------------------


class SignalExceptionPathsTest(TestCase):
    """Test exception handling in signal handlers."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="SigXMfg", slug="sigxmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="SigX-Dev", slug="sigx-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="SigX-SFP", part_number="SigX-SFP")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="SigXBay 0", position="0")
        role = DeviceRole.objects.create(name="SigXRole", slug="sigxrole")
        site = Site.objects.create(name="SigXSite", slug="sigxsite")
        cls.vc = VirtualChassis.objects.create(name="sigx-vc")
        cls.device = Device.objects.create(
            name="sigx-sw1",
            device_type=cls.device_type,
            role=role,
            site=site,
            virtual_chassis=cls.vc,
            vc_position=1,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="SigXBay 0")

    def test_pre_save_module_exception_sets_none(self):
        """on_module_pre_save catches DB exceptions and sets _prev_module_type_id=None (lines 39-40)."""
        from netbox_interface_name_rules.signals import on_module_pre_save

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        with patch.object(Module.objects.__class__, "filter", side_effect=Exception("db error")):
            on_module_pre_save(Module, module)
        self.assertIsNone(module._prev_module_type_id)

    def test_module_saved_no_prev_type_returns_early(self):
        """on_module_saved returns early (line 69) when _prev_module_type_id is not set."""
        from netbox_interface_name_rules.signals import on_module_saved

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        # _prev_module_type_id is not set → getattr returns None → line 69 return
        if hasattr(module, "_prev_module_type_id"):
            del module.__dict__["_prev_module_type_id"]
        on_module_saved(Module, module, created=False)  # Should return without error

    def test_pre_save_device_exception_sets_none(self):
        """on_device_pre_save catches DB exceptions and sets attributes to None (lines 143-145)."""
        from netbox_interface_name_rules.signals import on_device_pre_save

        with patch.object(Device.objects.__class__, "filter", side_effect=Exception("db error")):
            on_device_pre_save(Device, self.device)
        self.assertIsNone(self.device._prev_virtual_chassis_id)
        self.assertIsNone(self.device._prev_vc_position)

    def test_deferred_apply_engine_exception_is_logged(self):
        """_apply_rules_deferred catches apply_interface_name_rules exception (lines 115-116)."""
        from netbox_interface_name_rules.signals import _apply_rules_deferred

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        with patch(
            "netbox_interface_name_rules.engine.apply_interface_name_rules", side_effect=Exception("engine fail")
        ):
            _apply_rules_deferred(module.pk, self.bay.pk)  # Should not raise

    def test_deferred_device_module_engine_exception_is_logged(self):
        """_apply_rules_for_device_deferred catches exception from module loop (lines 218-225)."""
        from netbox_interface_name_rules.signals import _apply_rules_for_device_deferred

        Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        with patch("netbox_interface_name_rules.engine.apply_interface_name_rules", side_effect=Exception("loop fail")):
            _apply_rules_for_device_deferred(self.device.pk)  # Should not raise

    def test_deferred_device_device_interface_exception_is_logged(self):
        """_apply_rules_for_device_deferred catches exception from device interface rules (lines 233-234)."""
        from netbox_interface_name_rules.signals import _apply_rules_for_device_deferred

        with patch(
            "netbox_interface_name_rules.engine.apply_device_interface_rules",
            side_effect=Exception("device rule fail"),
        ):
            _apply_rules_for_device_deferred(self.device.pk)  # Should not raise


# ---------------------------------------------------------------------------
# signals.py — module with null bay path (line 214)
# ---------------------------------------------------------------------------


class SignalModuleNullBayPathTest(TestCase):
    """Test _apply_rules_for_device_deferred skips modules with null module_bay (line 214)."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="NullBayMfg", slug="nullbaymfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="NullBay-Dev", slug="nullbay-dev")
        ModuleBayTemplate.objects.create(device_type=device_type, name="NBBay 0", position="0")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="NullBay-SFP", part_number="NullBay-SFP"
        )
        role = DeviceRole.objects.create(name="NullBayRole", slug="nullbayrole")
        site = Site.objects.create(name="NullBaySite", slug="nullbaysite")
        vc = VirtualChassis.objects.create(name="nullbay-vc")
        cls.device = Device.objects.create(
            name="nullbay-sw1",
            device_type=device_type,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=1,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="NBBay 0")

    def test_module_with_null_bay_is_skipped(self):
        """_apply_rules_for_device_deferred continues past modules with module_bay=None.

        The signal function iterates over all modules on a device and calls
        apply_interface_name_rules for each one. When module_bay is None (e.g. due
        to a data inconsistency), the loop must skip that entry via ``continue``
        rather than passing None to the engine. A FakeModule with module_bay=None
        is injected via a patch on Module.objects.filter so the DB doesn't need
        to hold inconsistent data.
        """
        from netbox_interface_name_rules.signals import _apply_rules_for_device_deferred

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)

        # Mock the module queryset to return a module whose module_bay attr is None
        class FakeModule:
            module_bay = None
            module_type = module.module_type

        with patch(
            "dcim.models.Module.objects.filter",
            return_value=MagicMock(
                filter=MagicMock(return_value=MagicMock()),
                select_related=MagicMock(return_value=[FakeModule()]),
            ),
        ):
            _apply_rules_for_device_deferred(self.device.pk)  # Should not raise


# ---------------------------------------------------------------------------
# signals.py — _apply_rules_for_device_deferred outer exception handler (lines 224-225)
# ---------------------------------------------------------------------------


class SignalOuterModuleLoopExceptionTest(TestCase):
    """Test the outer except in _apply_rules_for_device_deferred (lines 224-225).

    Lines 208-225 wrap the entire module loop in a try/except.  Exceptions from
    the inner loop (apply_interface_name_rules) are caught by the inner handler
    (lines 218-223).  The outer handler catches anything raised *outside* the inner
    try — for example, if accessing module.module_bay raises unexpectedly.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="OuterXMfg", slug="outerxmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="OuterX-Dev", slug="outerx-dev")
        ModuleBayTemplate.objects.create(device_type=device_type, name="OXBay 0", position="0")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="OuterX-SFP", part_number="OuterX-SFP"
        )
        role = DeviceRole.objects.create(name="OuterXRole", slug="outerxrole")
        site = Site.objects.create(name="OuterXSite", slug="outerxsite")
        vc = VirtualChassis.objects.create(name="outerx-vc")
        cls.device = Device.objects.create(
            name="outerx-sw1",
            device_type=device_type,
            role=role,
            site=site,
            virtual_chassis=vc,
            vc_position=1,
        )
        cls.bay = ModuleBay.objects.get(device=cls.device, name="OXBay 0")

    def test_module_bay_access_exception_caught_by_outer_handler(self):
        """Exception raised by module.module_bay outside the inner try is caught by outer handler (lines 224-225)."""
        from netbox_interface_name_rules.signals import _apply_rules_for_device_deferred

        # A fake module object whose .module_bay attribute raises — this escapes the inner try
        class _RaisingModule:
            @property
            def module_bay(self):
                raise RuntimeError("outer loop failure")

        Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)

        with patch(
            "dcim.models.Module.objects.filter",
            return_value=MagicMock(select_related=MagicMock(return_value=[_RaisingModule()])),
        ):
            _apply_rules_for_device_deferred(self.device.pk)  # Must not raise


# ---------------------------------------------------------------------------
# signals.py — module deletion cascades to interfaces (documents CASCADE behavior)
# ---------------------------------------------------------------------------


class ModuleDeletionCascadeTest(TestCase):
    """Verify that deleting a module also deletes its interfaces (CASCADE on_delete).

    Interface.module uses on_delete=CASCADE, so when a module is removed from a bay
    all its renamed interfaces are deleted rather than orphaned.  This test documents
    the expected behavior so that any inadvertent change in cascade policy is caught.
    """

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="DelCasMfg", slug="delcasmfg")
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model="DelCas-Dev", slug="delcas-dev")
        ModuleBayTemplate.objects.create(device_type=device_type, name="DCBay 0", position="0")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="DelCas-SFP", part_number="DelCas-SFP"
        )
        role = DeviceRole.objects.create(name="DelCasRole", slug="delcasrole")
        site = Site.objects.create(name="DelCasSite", slug="delcassite")
        cls.device = Device.objects.create(name="delcas-sw1", device_type=device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="DCBay 0")

    def test_interfaces_deleted_when_module_removed(self):
        """Deleting a module cascades to its interfaces — renamed interfaces are removed."""
        from netbox_interface_name_rules.engine import apply_interface_name_rules

        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-0/0/{bay_position}",
        )
        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        iface = Interface.objects.create(device=self.device, module=module, name="0", type="10gbase-x-sfpp")

        # Rename the interface
        renamed = apply_interface_name_rules(module, self.bay)
        self.assertEqual(renamed, 1)
        iface.refresh_from_db()
        self.assertEqual(iface.name, "et-0/0/0")

        iface_pk = iface.pk
        module.delete()

        # Interface was cascade-deleted with the module
        self.assertFalse(Interface.objects.filter(pk=iface_pk).exists())


class ModulePreSaveExceptionLoggingTest(TestCase):
    """Test on_module_pre_save logs exceptions instead of silently swallowing them."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="PreSaveLogMfg", slug="presavelogmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=mfg, model="PSL-Dev", slug="psl-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=mfg, model="PSL-SFP", part_number="PSL-SFP")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="Bay 0", position="0")
        role = DeviceRole.objects.create(name="PSLRole", slug="pslrole")
        site = Site.objects.create(name="PSLSite", slug="pslsite")
        cls.device = Device.objects.create(name="psl-dev-01", device_type=cls.device_type, role=role, site=site)
        cls.bay = ModuleBay.objects.get(device=cls.device, name="Bay 0")

    def test_pre_save_db_error_logs_warning(self):
        """on_module_pre_save logs a warning when DB lookup fails."""
        from django.db import DatabaseError

        module = Module.objects.create(device=self.device, module_bay=self.bay, module_type=self.module_type)
        with patch.object(Module.objects, "filter", side_effect=DatabaseError("db error")):
            with self.assertLogs("netbox_interface_name_rules", level="WARNING") as cm:
                on_module_pre_save(Module, module)
        self.assertIsNone(module._prev_module_type_id)
        self.assertTrue(any("db error" in msg for msg in cm.output))

    def test_device_pre_save_db_error_logs_warning(self):
        """on_device_pre_save logs a warning when DB lookup fails."""
        from django.db import DatabaseError

        with patch.object(Device.objects, "filter", side_effect=DatabaseError("db error")):
            with self.assertLogs("netbox_interface_name_rules", level="WARNING") as cm:
                on_device_pre_save(Device, self.device)
        self.assertIsNone(self.device._prev_virtual_chassis_id)
        self.assertIsNone(self.device._prev_vc_position)
        self.assertTrue(any("db error" in msg for msg in cm.output))


class LibrenmsPredictReceiverTest(TestCase):
    """Receiver bridging netbox-librenms-plugin's predict_module_interface_names signal."""

    @classmethod
    def setUpTestData(cls):
        """Bay + device fixture for predict-receiver tests."""
        manufacturer = Manufacturer.objects.create(name="PredMfg", slug="predmfg")
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="PRED-Dev", slug="pred-dev")
        cls.module_type = ModuleType.objects.create(manufacturer=manufacturer, model="PRED-SFP", part_number="PRED-SFP")
        ModuleBayTemplate.objects.create(device_type=cls.device_type, name="PredBay 0", position="c9")
        role = DeviceRole.objects.create(name="PredRole", slug="predrole")
        site = Site.objects.create(name="PredSite", slug="predsite")
        cls.device = Device.objects.create(name="pred-test-01", device_type=cls.device_type, role=role, site=site)
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
