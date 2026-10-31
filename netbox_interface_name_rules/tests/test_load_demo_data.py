# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""scripts/load_demo_data.py on the test database."""

import importlib.util
import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dcim.models import (
    Device,
    DeviceType,
    Interface,
    InterfaceTemplate,
    Module,
    ModuleBay,
    ModuleType,
    Site,
    VirtualChassis,
)
from django.db.models import QuerySet
from django.test import TestCase, TransactionTestCase

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.models import InterfaceNameRule

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "load_demo_data.py"
_spec = importlib.util.spec_from_file_location("load_demo_data", SCRIPT)
load_demo_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_demo_data)

_REGEX_ROW = """- module_type_pattern: "DEMO-.*"
  module_type_is_regex: true
  name_template: "{name_template}"
"""
_CHANNELIZED_ROW = """- module_type_pattern: "DEMO-.*"
  module_type_is_regex: true
  name_template: "xe-0/0/{bay_position}:{channel}"
  parent_name_template: "et-0/0/{bay_position}"
  breakout_mode: channelized
  channel_count: 4
"""


def run(function, *args):
    """Call a function of the loader, and return its result and what it printed."""
    output = io.StringIO()
    with redirect_stdout(output):
        result = function(*args)
    return result, output.getvalue()


class LoaderTestCase(TestCase):
    def setUp(self):
        (self.site, self.manufacturer, self.role), _ = run(load_demo_data.shared_objects)

    def use_contrib(self, files: dict[str, str]) -> None:
        """Point CONTRIB_DIR at a temporary directory that holds the YAML *files*."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, text in files.items():
            (Path(directory.name) / name).write_text(text, encoding="utf-8")
        patcher = patch.object(load_demo_data, "CONTRIB_DIR", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def load_rules(self) -> tuple[int, str]:
        run(load_demo_data.load_rule_scopes, self.manufacturer)
        return run(load_demo_data.load_rules)


class ContribRowsTest(LoaderTestCase):
    def test_a_row_that_is_not_a_mapping_is_a_skipped_rule(self):
        self.use_contrib({"rules.yaml": _REGEX_ROW.format(name_template="x{bay_position}") + "-\n- stray\n"})

        skipped, output = self.load_rules()

        self.assertEqual(skipped, 2)
        self.assertIn("rules.yaml row 2", output)
        self.assertIn("rules.yaml row 3", output)
        self.assertEqual(InterfaceNameRule.objects.filter(module_type_pattern="DEMO-.*").count(), 1)

    def test_a_row_sets_its_breakout_mode_and_parent_name_template(self):
        self.use_contrib({"rules.yaml": _CHANNELIZED_ROW})

        skipped, _ = self.load_rules()

        rule = InterfaceNameRule.objects.get(module_type_pattern="DEMO-.*")
        self.assertEqual(skipped, 0)
        self.assertEqual(rule.breakout_mode, BreakoutModeChoices.CHANNELIZED)
        self.assertEqual(rule.parent_name_template, "et-0/0/{bay_position}")

    def test_a_row_without_the_breakout_fields_resets_them_to_the_model_defaults(self):
        self.use_contrib({"rules.yaml": _CHANNELIZED_ROW})
        self.load_rules()
        self.use_contrib({"rules.yaml": _REGEX_ROW.format(name_template="x{bay_position}")})

        self.load_rules()

        rule = InterfaceNameRule.objects.get(module_type_pattern="DEMO-.*")
        self.assertEqual(rule.breakout_mode, BreakoutModeChoices.FLAT)
        self.assertEqual(rule.parent_name_template, "")

    def test_an_update_keeps_a_field_that_changed_after_the_loader_read_the_rule(self):
        self.use_contrib({"rules.yaml": _REGEX_ROW.format(name_template="a{bay_position}")})
        self.load_rules()
        self.use_contrib({"rules.yaml": _REGEX_ROW.format(name_template="b{bay_position}")})
        real_save = InterfaceNameRule.save

        def save_after_a_concurrent_edit(rule, **kwargs):
            # An operator disables the rule between the read of the loader and its save.
            InterfaceNameRule.objects.filter(pk=rule.pk).update(enabled=False)
            return real_save(rule, **kwargs)

        with patch.object(InterfaceNameRule, "save", save_after_a_concurrent_edit):
            self.load_rules()

        rule = InterfaceNameRule.objects.get(module_type_pattern="DEMO-.*")
        self.assertEqual(rule.name_template, "b{bay_position}")
        self.assertFalse(rule.enabled)

    def test_a_rule_that_another_writer_creates_after_the_lookup_is_updated(self):
        self.use_contrib({"rules.yaml": _REGEX_ROW.format(name_template="a{bay_position}")})
        real_get = QuerySet.get
        raced = []

        def get_after_a_concurrent_create(queryset, *args, **kwargs):
            # Another writer creates the rule after the lookup of the loader found none.
            if queryset.model is InterfaceNameRule and not raced:
                raced.append(True)
                InterfaceNameRule.objects.create(
                    module_type_pattern="DEMO-.*", module_type_is_regex=True, name_template="other{bay_position}"
                )
                raise InterfaceNameRule.DoesNotExist
            return real_get(queryset, *args, **kwargs)

        with patch.object(QuerySet, "get", get_after_a_concurrent_create):
            skipped, output = self.load_rules()

        self.assertEqual(skipped, 0, output)
        self.assertEqual(
            list(
                InterfaceNameRule.objects.filter(module_type_pattern="DEMO-.*").values_list("name_template", flat=True)
            ),
            ["a{bay_position}"],
        )

    def test_a_second_row_with_the_same_rule_is_a_skipped_rule(self):
        self.use_contrib(
            {
                "a.yaml": _REGEX_ROW.format(name_template="a{bay_position}"),
                "b.yaml": _REGEX_ROW.format(name_template="b{bay_position}"),
            }
        )

        skipped, output = self.load_rules()

        self.assertEqual(skipped, 1)
        self.assertEqual(InterfaceNameRule.objects.get(module_type_pattern="DEMO-.*").name_template, "a{bay_position}")
        skip_line = next(line for line in output.splitlines() if "b.yaml" in line and "a.yaml" in line)
        self.assertIn("DEMO-.*", skip_line)

    def test_a_file_of_alternative_rows_loads_the_first_row_of_each_rule(self):
        self.use_contrib(
            {
                "linux.yaml": _REGEX_ROW.format(name_template="eth{bay_position}")
                + _REGEX_ROW.format(name_template="ens{bay_position}")
            }
        )

        skipped, output = self.load_rules()

        self.assertEqual(skipped, 0)
        self.assertEqual(
            InterfaceNameRule.objects.get(module_type_pattern="DEMO-.*").name_template, "eth{bay_position}"
        )
        self.assertIn("an option of linux.yaml", output)

    def test_the_demo_loads_juniper_yaml_and_not_its_channelized_alternative(self):
        skipped, output = self.load_rules()

        self.assertEqual(skipped, 0, output)
        self.assertNotIn("Loading juniper-channelized.yaml", output)
        notice = next(line for line in output.splitlines() if "juniper-channelized.yaml" in line)
        self.assertIn("juniper.yaml", notice.replace("juniper-channelized.yaml", ""))
        rule = InterfaceNameRule.objects.get(
            module_type_pattern="QSFP-4X10G-.*", device_type__model="ACX7024", applies_to_device_interfaces=False
        )
        self.assertEqual(rule.description, "Juniper ACX7024 QSFP+ 4x10G breakout (all variants)")
        self.assertEqual(rule.breakout_mode, BreakoutModeChoices.FLAT)

    def test_a_row_without_a_name_template_creates_no_type(self):
        self.use_contrib({"rules.yaml": "- device_type: NO-RULE-DT\n  module_type: NO-RULE-MT\n"})

        skipped, _ = self.load_rules()

        self.assertEqual(skipped, 1)
        self.assertFalse(DeviceType.objects.filter(model="NO-RULE-DT").exists())
        self.assertFalse(ModuleType.objects.filter(model="NO-RULE-MT").exists())

    def test_a_row_with_a_manufacturer_loads_the_rule_of_its_module_type(self):
        self.use_contrib(
            {"rules.yaml": "- module_type: VENDOR-MT\n  manufacturer: Demo Vendor\n  name_template: v{bay_position}\n"}
        )

        skipped, output = self.load_rules()

        self.assertEqual(skipped, 0, output)
        rule = InterfaceNameRule.objects.get(module_type__model="VENDOR-MT")
        self.assertEqual(rule.module_type.manufacturer.name, "Demo Vendor")


class ModuleInstallTest(LoaderTestCase):
    def test_the_100_bay_device_gets_a_module_in_each_empty_bay_on_the_next_run(self):
        run(load_demo_data.load_100_bay_device, self.site, self.manufacturer, self.role)
        for module in Module.objects.filter(device__name="test-100ports", module_bay__name__in=["bay1", "bay2"]):
            module.delete()

        _, output = run(load_demo_data.load_100_bay_device, self.site, self.manufacturer, self.role)

        modules = Module.objects.filter(device__name="test-100ports")
        self.assertEqual(modules.count(), 100)
        self.assertEqual(modules.values("module_bay").distinct().count(), 100)
        self.assertIn("Installed 2 TEST-1PORT", output)
        _, output = run(load_demo_data.load_100_bay_device, self.site, self.manufacturer, self.role)
        self.assertNotIn("Installed", output)

    def test_each_module_of_the_100_bay_device_has_an_interface_for_the_batch_limit(self):
        run(load_demo_data.load_100_bay_device, self.site, self.manufacturer, self.role)

        self.assertEqual(Interface.objects.filter(device__name="test-100ports", module__isnull=False).count(), 100)

    def test_a_module_that_an_earlier_run_installed_without_an_interface_gets_one(self):
        run(load_demo_data.load_100_bay_device, self.site, self.manufacturer, self.role)
        # An earlier loader installed the modules before TEST-1PORT had an interface template.
        Interface.objects.filter(device__name="test-100ports", module__module_bay__name__in=["bay1", "bay2"]).delete()

        run(load_demo_data.load_100_bay_device, self.site, self.manufacturer, self.role)

        names = Interface.objects.filter(device__name="test-100ports", module__module_bay__name__in=["bay1", "bay2"])
        self.assertEqual(sorted(names.values_list("name", flat=True)), ["1", "2"])

    def test_the_apply_demo_device_gets_a_module_in_each_empty_bay_on_the_next_run(self):
        run(load_demo_data.load_apply_demo_device, self.site, self.manufacturer, self.role)
        Module.objects.get(device__name=load_demo_data.APPLY_DEMO_DEVICE, module_bay__position="2").delete()

        _, output = run(load_demo_data.load_apply_demo_device, self.site, self.manufacturer, self.role)

        positions = Module.objects.filter(device__name=load_demo_data.APPLY_DEMO_DEVICE).values_list(
            "module_bay__position", flat=True
        )
        self.assertEqual(sorted(positions), [str(bay) for bay in load_demo_data.APPLY_DEMO_BAYS])
        self.assertIn(f"Installed 1 {load_demo_data.APPLY_DEMO_MODULE_TYPE}", output)
        _, output = run(load_demo_data.load_apply_demo_device, self.site, self.manufacturer, self.role)
        self.assertNotIn("Installed", output)

    def test_a_numeric_device_type_in_yaml_names_a_device_type_by_its_text(self):
        self.use_contrib({"numeric.yaml": "- device_type: 7609\n  name_template: x\n"})

        run(load_demo_data.load_rule_scopes, self.manufacturer)
        run(load_demo_data.load_rule_scopes, self.manufacturer)

        self.assertEqual(list(DeviceType.objects.filter(model="7609").values_list("slug", flat=True)), ["7609"])

    def test_a_module_type_that_exists_gets_its_module_interface_template(self):
        ModuleType.objects.create(manufacturer=self.manufacturer, model=load_demo_data.APPLY_DEMO_MODULE_TYPE)

        run(load_demo_data.load_apply_demo_device, self.site, self.manufacturer, self.role)
        run(load_demo_data.load_apply_demo_device, self.site, self.manufacturer, self.role)

        templates = InterfaceTemplate.objects.filter(module_type__model=load_demo_data.APPLY_DEMO_MODULE_TYPE)
        self.assertEqual(list(templates.values_list("name", flat=True)), ["{module}"])


class OtherSiteDeviceTest(LoaderTestCase):
    def test_a_device_of_the_same_name_at_another_site_is_not_reused(self):
        other_site = Site.objects.create(name="Other Site", slug="other-site")
        device_type = DeviceType.objects.create(manufacturer=self.manufacturer, model="USER-DT", slug="user-dt")
        user_device = Device.objects.create(name="demo-x", site=other_site, device_type=device_type, role=self.role)

        demo_device, _ = run(load_demo_data.device, "demo-x", self.site, device_type, self.role)

        self.assertEqual(demo_device.site, self.site)
        self.assertNotEqual(demo_device.pk, user_device.pk)

    def test_a_device_of_the_demo_site_with_another_device_type_stops_the_load(self):
        user_type = DeviceType.objects.create(manufacturer=self.manufacturer, model="USER-DT", slug="user-dt")
        demo_type = DeviceType.objects.create(manufacturer=self.manufacturer, model="DEMO-DT", slug="demo-dt")
        Device.objects.create(name="demo-x", site=self.site, device_type=user_type, role=self.role)

        with self.assertRaisesMessage(LookupError, "demo-x"):
            run(load_demo_data.device, "demo-x", self.site, demo_type, self.role)

    def test_the_stale_module_cleanup_keeps_the_modules_of_a_device_at_another_site(self):
        other_site = Site.objects.create(name="Other Site", slug="other-site")
        device_type = DeviceType.objects.create(manufacturer=self.manufacturer, model="USER-DT", slug="user-dt")
        user_device = Device.objects.create(name="demo-vc-1", site=other_site, device_type=device_type, role=self.role)
        bay = ModuleBay.objects.create(device=user_device, name="sfp0")
        module_type = ModuleType.objects.create(manufacturer=self.manufacturer, model="USER-SFP")
        Module.objects.create(device=user_device, module_bay=bay, module_type=module_type)

        run(load_demo_data.load_demo_vc_stacks, self.site, self.manufacturer, self.role)

        self.assertTrue(Module.objects.filter(device=user_device, module_type=module_type).exists())


class ReapplyDeviceRulesTest(LoaderTestCase):
    def test_the_demo_chassis_are_the_chassis_that_the_loader_creates(self):
        run(load_demo_data.load_demo_vc_stacks, self.site, self.manufacturer, self.role)
        run(load_demo_data.load_vendor_stacks, self.site, self.manufacturer, self.role)

        self.assertEqual(set(VirtualChassis.objects.values_list("name", flat=True)), set(load_demo_data.DEMO_CHASSIS))

    def test_the_device_rules_rename_only_the_members_of_the_demo_chassis(self):
        run(load_demo_data.load_vendor_stacks, self.site, self.manufacturer, self.role)
        demo_member = Device.objects.get(name="cisco-sw-2")
        user_member = Device.objects.create(
            name="user-sw-3", site=self.site, device_type=demo_member.device_type, role=self.role
        )
        user_member.virtual_chassis = VirtualChassis.objects.create(name="user-stack", master=None)
        user_member.vc_position = 3
        user_member.save()
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            device_type=demo_member.device_type,
            module_type_pattern="GigabitEthernet[0-9]+/[0-9]+/[0-9]+",
            name_template="GigabitEthernet{vc_position}/0/{port}",
        )

        run(load_demo_data.reapply_device_rules)

        self.assertTrue(Interface.objects.filter(device=demo_member, name="GigabitEthernet2/0/1").exists())
        self.assertTrue(Interface.objects.filter(device=user_member, name="GigabitEthernet1/0/1").exists())


class LoaderTransactionTest(TestCase):
    def test_a_run_that_stops_leaves_no_partial_demo_data(self):
        with (
            patch.object(load_demo_data, "load_vendor_stacks", side_effect=RuntimeError("stopped")),
            self.assertRaisesMessage(RuntimeError, "stopped"),
        ):
            run(load_demo_data.main)

        self.assertFalse(Device.objects.filter(name="test-100ports").exists())

    def test_the_demo_loads_with_status_0_and_a_second_run_changes_nothing(self):
        first, _ = run(load_demo_data.main)
        before = list(InterfaceNameRule.objects.order_by("pk").values_list("pk", "last_updated"))
        second, output = run(load_demo_data.main)
        after = list(InterfaceNameRule.objects.order_by("pk").values_list("pk", "last_updated"))

        self.assertEqual((first, second), (0, 0))
        self.assertEqual(after, before)
        self.assertNotIn("✓", output)


class LoaderCommitTest(TransactionTestCase):
    def test_the_apply_demo_device_keeps_the_interface_names_of_netbox_after_the_commit(self):
        status, _ = run(load_demo_data.main)

        names = Interface.objects.filter(device__name=load_demo_data.APPLY_DEMO_DEVICE).values_list("name", flat=True)
        self.assertEqual(status, 0)
        self.assertEqual(sorted(names), [str(position) for position in load_demo_data.APPLY_DEMO_BAYS])
