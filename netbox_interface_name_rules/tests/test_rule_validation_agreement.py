# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The model's clean() and the database must refuse the same rules.

Every combination below is one clean() rejects or silently rewrites. A queryset write skips
clean(), so each one must also be unable to reach the table.
"""

from importlib import import_module

from dcim.models import DeviceType, Manufacturer, ModuleType, Platform
from django.apps import apps as global_apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.models import InterfaceNameRule


class RuleValidationAgreementTest(TestCase):
    """Pin clean() and the check constraints to the same set of invalid rules."""

    @classmethod
    def setUpTestData(cls):
        manufacturer = Manufacturer.objects.create(name="AgreeMfg", slug="agreemfg")
        cls.module_type = ModuleType.objects.create(
            manufacturer=manufacturer, model="AGREE-QSFP", part_number="AGREE-QSFP"
        )
        cls.device_type = DeviceType.objects.create(manufacturer=manufacturer, model="AGREE-SW", slug="agree-sw")
        cls.platform = Platform.objects.create(name="AgreePlatform", slug="agree-platform")

    def invalid_combinations(self):
        """Return every field combination clean() refuses to leave as written."""
        return [
            (
                "device rule carrying regex mode",
                {
                    "applies_to_device_interfaces": True,
                    "module_type": None,
                    "module_type_is_regex": True,
                    "module_type_pattern": "Gi.*",
                    "device_type": self.device_type,
                    "name_template": "Gi{vc_position}/{port}",
                },
            ),
            (
                "device rule building a channelized family",
                {
                    "applies_to_device_interfaces": True,
                    "module_type": None,
                    "breakout_mode": BreakoutModeChoices.CHANNELIZED,
                    "channel_count": 2,
                    "platform": self.platform,
                    "name_template": "Gi{vc_position}/{port}",
                },
            ),
            (
                "device rule carrying a parent template",
                {
                    "applies_to_device_interfaces": True,
                    "module_type": None,
                    "parent_name_template": "et-0/0/{port}",
                    "device_type": self.device_type,
                    "platform": self.platform,
                    "name_template": "Gi{vc_position}/{port}",
                },
            ),
            (
                "parent template without the channelized mode",
                {
                    "module_type": self.module_type,
                    "breakout_mode": BreakoutModeChoices.FLAT,
                    "parent_name_template": "et-0/0/{bay_position}",
                    "name_template": "xe-0/0/{bay_position}:{channel}",
                },
            ),
            (
                "channelized rule defining no channels",
                {
                    "module_type": self.module_type,
                    "device_type": self.device_type,
                    "breakout_mode": BreakoutModeChoices.CHANNELIZED,
                    "channel_count": 0,
                    "name_template": "xe-0/0/{bay_position}:{channel}",
                },
            ),
        ]

    def test_clean_refuses_or_rewrites_every_combination(self):
        """clean() must never leave one of these rules as the caller wrote it."""
        for label, fields in self.invalid_combinations():
            with self.subTest(label):
                rule = InterfaceNameRule(**fields)
                try:
                    rule.clean()
                except ValidationError:
                    continue
                rewritten = {
                    name: getattr(rule, name) for name, value in fields.items() if getattr(rule, name) != value
                }
                self.assertTrue(rewritten, f"clean() accepted {label} unchanged")

    def test_the_database_refuses_every_combination(self):
        """A queryset write skips clean(), so the constraints must refuse the same rules."""
        for label, fields in self.invalid_combinations():
            with self.subTest(label):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    InterfaceNameRule.objects.create(**fields)

    def test_a_valid_rule_of_each_shape_still_saves(self):
        """The constraints must not refuse the rules the plugin is built to store."""
        InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_count=4,
            parent_name_template="et-0/0/{bay_position}",
        )
        InterfaceNameRule.objects.create(
            module_type_is_regex=True,
            module_type_pattern="QSFP.*",
            name_template="xe-0/0/{bay_position}",
        )
        InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern="Gi.*",
            device_type=self.device_type,
            name_template="Gi{vc_position}/{port}",
        )
        self.assertEqual(InterfaceNameRule.objects.count(), 3)


class RuleNormalizationMigrationTest(TestCase):
    """The 0015 data migration must repair the rows that predate its constraints."""

    CONSTRAINTS = (
        "interfacenamerule_module_type_mode_check",
        "interfacenamerule_breakout_topology_check",
    )

    def _set_constraints(self, enabled):
        """Drop or restore the check constraints inside this test's transaction."""
        migration = import_module("netbox_interface_name_rules.migrations.0015_align_rule_constraints_with_clean")
        with connection.cursor() as cursor:
            # Rows inserted in this transaction leave deferred FK events that block any ALTER TABLE.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for operation in migration.Migration.operations:
                constraint = getattr(operation, "constraint", None)
                if constraint is None or constraint.name not in self.CONSTRAINTS:
                    continue
                if enabled:
                    sql = constraint.create_sql(InterfaceNameRule, connection.schema_editor())
                else:
                    sql = constraint.remove_sql(InterfaceNameRule, connection.schema_editor())
                cursor.execute(str(sql))
        return migration

    def test_it_repairs_every_row_the_constraints_now_refuse(self):
        migration = self._set_constraints(enabled=False)
        manufacturer = Manufacturer.objects.create(name="MigMfg", slug="migmfg")
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model="MIG-QSFP", part_number="MIG-QSFP")
        device_rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_is_regex=True,
            module_type_pattern="Gi.*",
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_count=4,
            parent_name_template="et-0/0/{port}",
            name_template="Gi{vc_position}/{port}",
        )
        channelless = InterfaceNameRule.objects.create(
            module_type=module_type,
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_count=0,
            parent_name_template="et-0/0/{bay_position}",
            name_template="xe-0/0/{bay_position}:{channel}",
        )

        migration.normalize_invalid_rules(global_apps, None)

        device_rule.refresh_from_db()
        channelless.refresh_from_db()
        self.assertFalse(device_rule.module_type_is_regex)
        self.assertEqual(device_rule.breakout_mode, BreakoutModeChoices.FLAT)
        self.assertEqual(device_rule.parent_name_template, "")
        self.assertEqual(channelless.breakout_mode, BreakoutModeChoices.FLAT)
        self.assertEqual(channelless.parent_name_template, "")
        # PostgreSQL validates every existing row here, so this only succeeds if the repair was complete.
        self._set_constraints(enabled=True)
