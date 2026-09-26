# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The model's clean() and the database must refuse the same rules.

Every combination below is one clean() rejects or silently rewrites. A queryset write skips
clean(), so each one must also be unable to reach the table.
"""

import ast
import inspect
from importlib import import_module
from pathlib import Path

from dcim.models import DeviceType, Manufacturer, ModuleType, Platform
from django.apps import apps as global_apps
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, migrations, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

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
                "device rule carrying a parent module type",
                {
                    "applies_to_device_interfaces": True,
                    "module_type": None,
                    "parent_module_type": self.module_type,
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

    def template_grammar_combinations(self):
        """Return the parent-template rejections a check constraint cannot express."""
        return [
            (
                "parent template naming the channel",
                {
                    "module_type": self.module_type,
                    "breakout_mode": BreakoutModeChoices.CHANNELIZED,
                    "channel_count": 4,
                    "parent_name_template": "et-0/0/{bay_position}:{channel}",
                    "name_template": "xe-0/0/{bay_position}:{channel}",
                },
            ),
            (
                "parent template with unbalanced braces",
                {
                    "module_type": self.module_type,
                    "breakout_mode": BreakoutModeChoices.CHANNELIZED,
                    "channel_count": 4,
                    "parent_name_template": "et-0/0/{bay_position",
                    "name_template": "xe-0/0/{bay_position}:{channel}",
                },
            ),
        ]

    def test_clean_refuses_every_template_grammar_combination(self):
        """clean() rejects both parent-template shapes."""
        for label, fields in self.template_grammar_combinations():
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    InterfaceNameRule(**fields).clean()

    def test_the_write_path_refuses_every_template_grammar_combination(self):
        """save() must refuse what no check constraint can express, so a plain create cannot store it."""
        for label, fields in self.template_grammar_combinations():
            with self.subTest(label):
                with self.assertRaises(ValidationError), transaction.atomic():
                    InterfaceNameRule.objects.create(**fields)
                self.assertFalse(
                    InterfaceNameRule.objects.filter(parent_name_template=fields["parent_name_template"]).exists()
                )

    def test_saving_an_unrelated_field_skips_the_topology_check(self):
        """A targeted update_fields save must not re-validate columns it does not write."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_count=4,
            parent_name_template="et-0/0/{bay_position}",
        )
        InterfaceNameRule.objects.filter(pk=rule.pk).update(parent_name_template="et-0/0/{bay_position")
        rule.refresh_from_db()
        rule.enabled = False

        rule.save(update_fields=["enabled"])

        rule.refresh_from_db()
        self.assertFalse(rule.enabled)

    def test_a_targeted_save_does_not_normalise_from_an_unsaved_mode(self):
        rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            module_type_pattern="eth.*",
            name_template="p{port}",
        )
        rule.applies_to_device_interfaces = False
        rule.module_type_pattern = "xe.*"

        rule.save(update_fields=["module_type_pattern"])

        rule.refresh_from_db()
        self.assertTrue(rule.applies_to_device_interfaces)
        self.assertEqual(rule.module_type_pattern, "xe.*")

    def test_a_targeted_save_validates_the_stored_mode_not_an_unsaved_one(self):
        """A flat stored rule must not take a {channel} template from an unsaved channelized mode."""
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="xe-{bay_position}")
        rule.breakout_mode = BreakoutModeChoices.CHANNELIZED
        rule.channel_count = 4
        rule.name_template = "xe-{bay_position}:{channel}"

        with self.assertRaises(ValidationError):
            rule.save(update_fields=["name_template"])

        rule.refresh_from_db()
        self.assertEqual(rule.breakout_mode, BreakoutModeChoices.FLAT)
        self.assertEqual(rule.name_template, "xe-{bay_position}")

    def test_a_targeted_save_ignores_an_invalid_unsaved_mode(self):
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="xe-{bay_position}")
        rule.breakout_mode = BreakoutModeChoices.CHANNELIZED
        rule.channel_count = 0
        rule.name_template = "ge-{bay_position}"

        rule.save(update_fields=["name_template"])

        rule.refresh_from_db()
        self.assertEqual(rule.breakout_mode, BreakoutModeChoices.FLAT)
        self.assertEqual(rule.name_template, "ge-{bay_position}")

    def test_a_targeted_save_of_the_mode_fields_validates_their_new_values(self):
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="xe-{bay_position}")
        rule.breakout_mode = BreakoutModeChoices.CHANNELIZED
        rule.channel_count = 0
        rule.name_template = "xe-{bay_position}:{channel}"

        with self.assertRaises(ValidationError):
            rule.save(update_fields=["breakout_mode", "channel_count", "name_template"])

        rule.channel_count = 4
        rule.save(update_fields=["breakout_mode", "channel_count", "name_template"])

        rule.refresh_from_db()
        self.assertEqual(rule.breakout_mode, BreakoutModeChoices.CHANNELIZED)
        self.assertEqual(rule.name_template, "xe-{bay_position}:{channel}")

    def test_a_targeted_save_of_a_missing_row_keeps_the_django_error(self):
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="xe-{bay_position}")
        InterfaceNameRule.objects.filter(pk=rule.pk).delete()

        with self.assertRaisesMessage(DatabaseError, "did not affect any rows"):
            rule.save(update_fields=["name_template"])

    def test_a_targeted_save_of_an_unsaved_rule_keeps_the_django_error(self):
        rule = InterfaceNameRule(module_type=self.module_type, name_template="xe-{bay_position}")

        with self.assertRaisesMessage(ValueError, "no primary key"):
            rule.save(update_fields=["name_template"])

    def test_a_targeted_save_of_an_unrelated_field_does_not_query_the_rule(self):
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="xe-{bay_position}")
        rule.description = "after"

        with CaptureQueriesContext(connection) as queries:
            rule.save(update_fields=["description"])

        rule_reads = f'SELECT "{InterfaceNameRule._meta.db_table}".'
        self.assertFalse([query["sql"] for query in queries if query["sql"].startswith(rule_reads)])

    def test_a_targeted_save_locks_the_row_it_validates_against(self):
        """A concurrent targeted save must not change the stored fields between this read and this write."""
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="xe-{bay_position}")
        rule.name_template = "xe-0/{bay_position}"

        with CaptureQueriesContext(connection) as queries:
            rule.save(update_fields=["name_template"])

        rule_reads = f'SELECT "{InterfaceNameRule._meta.db_table}".'
        locked = [query["sql"] for query in queries if query["sql"].startswith(rule_reads)]
        self.assertTrue(locked)
        self.assertTrue(all(sql.endswith(" FOR UPDATE") for sql in locked), locked)

    def test_save_refuses_positional_arguments(self):
        """Positional update_fields would skip both save() guards; Django 6.0 removes them anyway."""
        rule = InterfaceNameRule.objects.create(module_type=self.module_type, name_template="p{bay_position}")

        with self.assertRaises(TypeError):
            rule.save(False, False, None, ["description"])

    def test_a_generator_update_fields_still_writes_its_column(self):
        """The topology check must not consume update_fields, which would make Django skip the save."""
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}",
            description="before",
        )
        rule.description = "after"

        rule.save(update_fields=(name for name in ["description"]))

        rule.refresh_from_db()
        self.assertEqual(rule.description, "after")

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
        """bulk_create() skips both clean() and save(), so the constraints must refuse the same rules."""
        for label, fields in self.invalid_combinations():
            with self.subTest(label):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    InterfaceNameRule.objects.bulk_create([InterfaceNameRule(**fields)])

    def test_save_refuses_an_unknown_breakout_mode(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            InterfaceNameRule.objects.create(
                module_type=self.module_type,
                name_template="xe-0/0/{bay_position}",
                breakout_mode="bogus",
            )

    def test_queryset_update_refuses_an_unknown_breakout_mode(self):
        rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="xe-0/0/{bay_position}",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            InterfaceNameRule.objects.filter(pk=rule.pk).update(breakout_mode="bogus")

    def test_queryset_update_refuses_a_parent_module_type_on_a_device_rule(self):
        rule = InterfaceNameRule.objects.create(applies_to_device_interfaces=True, name_template="{base}")
        with self.assertRaises(IntegrityError), transaction.atomic():
            InterfaceNameRule.objects.filter(pk=rule.pk).update(parent_module_type=self.module_type)

    def test_clean_names_the_parent_module_type_field(self):
        rule = InterfaceNameRule(
            applies_to_device_interfaces=True, parent_module_type=self.module_type, name_template="{base}"
        )
        with self.assertRaises(ValidationError) as caught:
            rule.clean()
        self.assertEqual(list(caught.exception.message_dict), ["parent_module_type"])

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


class ModeNormalisationSeamTest(SimpleTestCase):
    """Only _normalise_mode() may change a field, so the API and the web form store the same rule."""

    def test_only_normalise_mode_writes_a_model_field(self):
        fields = {name for field in InterfaceNameRule._meta.concrete_fields for name in (field.name, field.attname)}
        source, first_line = inspect.getsourcelines(InterfaceNameRule)
        (rule_class,) = ast.increment_lineno(ast.parse("".join(source)), first_line - 1).body
        methods = [node for node in rule_class.body if isinstance(node, ast.FunctionDef)]
        self.assertIn("_normalise_mode", [method.name for method in methods])
        for method in methods:
            if method.name == "_normalise_mode":
                continue
            attribute_owners = {id(node.value) for node in ast.walk(method) if isinstance(node, ast.Attribute)}
            for node in ast.walk(method):
                if isinstance(node, ast.Name) and node.id == "self":
                    # A bare self (setattr, an alias, a helper argument) could change a field unseen.
                    self.assertTrue(
                        id(node) in attribute_owners,
                        f"models.py:{node.lineno} {method.name}() uses self, not self.<attr>",
                    )
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                    self.assertFalse(
                        isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in fields,
                        f"{method.name}() writes self.{node.attr}; change a field only in _normalise_mode()",
                    )


class RefuseImplicitMigrationDatabase:
    """Refuse migration queries that do not select a database explicitly."""

    def db_for_read(self, model, **hints):
        raise AssertionError("Migration must select the schema editor database")

    def db_for_write(self, model, **hints):
        raise AssertionError("Migration must select the schema editor database")


# The template audit reports against the live language, so it must not hold a second parser.
LIVE_IMPORT_PERMITS = frozenset(
    {("0017_audit_name_templates.py", "netbox_interface_name_rules.name_template", (("validate_rule", None),))}
)


class RuleNormalizationMigrationTest(TestCase):
    """The 0015 data migration must repair the rows that predate its constraints."""

    CONSTRAINTS = (
        "interfacenamerule_module_type_mode_check",
        "interfacenamerule_breakout_topology_check",
    )

    def test_migrations_only_import_the_shared_template_audit_validator(self):
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        for migration in sorted(migrations.rglob("*.py")):
            with self.subTest(migration=migration.name):
                tree = ast.parse(migration.read_text(encoding="utf-8"))
                imports = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertEqual(
                            node.level,
                            0,
                            f"{migration.name} uses a relative import; migrations must not use relative imports",
                        )
                        permit = (migration.name, node.module, tuple((a.name, a.asname) for a in node.names))
                        if permit in LIVE_IMPORT_PERMITS:
                            continue
                        imports.append(node.module)
                    elif isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                self.assertFalse(
                    [name for name in imports if name and name.startswith("netbox_interface_name_rules")],
                    f"{migration.name} imports live application code",
                )

    def test_router_refuses_queries_without_an_explicit_database(self):
        with override_settings(DATABASE_ROUTERS=[RefuseImplicitMigrationDatabase()]):
            with self.assertRaisesMessage(AssertionError, "Migration must select the schema editor database"):
                InterfaceNameRule.objects.exists()
            with self.assertRaisesMessage(AssertionError, "Migration must select the schema editor database"):
                InterfaceNameRule.objects.filter(pk=-1).update(enabled=False)

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

    def test_it_repairs_unsupported_breakout_modes_before_adding_the_constraint(self):
        self._set_constraints(enabled=False)
        rule = InterfaceNameRule.objects.create(
            applies_to_device_interfaces=True,
            name_template="Gi{vc_position}/{port}",
            breakout_mode="bogus",
        )
        migration = import_module("netbox_interface_name_rules.migrations.0016_restrict_breakout_mode_values")
        with override_settings(DATABASE_ROUTERS=[RefuseImplicitMigrationDatabase()]):
            for operation in migration.Migration.operations:
                if isinstance(operation, migrations.RunPython):
                    operation.code(global_apps, connection.schema_editor())
                elif isinstance(operation, migrations.AddConstraint):
                    rule.refresh_from_db(using=connection.alias)
                    self.assertEqual(rule.breakout_mode, BreakoutModeChoices.FLAT)
                    with connection.cursor() as cursor:
                        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                        cursor.execute(
                            str(operation.constraint.create_sql(InterfaceNameRule, connection.schema_editor()))
                        )

    def test_it_repairs_every_row_the_constraints_now_refuse(self):
        migration = self._set_constraints(enabled=False)
        manufacturer = Manufacturer.objects.create(name="MigMfg", slug="migmfg")
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model="MIG-QSFP", part_number="MIG-QSFP")
        # These rows predate the constraints, so they must reach the table the way they did then:
        # bulk_create() skips both clean() and save().
        device_rule, channelless = InterfaceNameRule.objects.bulk_create(
            [
                InterfaceNameRule(
                    applies_to_device_interfaces=True,
                    module_type_is_regex=True,
                    module_type_pattern="Gi.*",
                    breakout_mode=BreakoutModeChoices.CHANNELIZED,
                    channel_count=4,
                    parent_name_template="et-0/0/{port}",
                    name_template="Gi{vc_position}/{port}",
                ),
                InterfaceNameRule(
                    module_type=module_type,
                    breakout_mode=BreakoutModeChoices.CHANNELIZED,
                    channel_count=0,
                    parent_name_template="et-0/0/{bay_position}",
                    name_template="xe-0/0/{bay_position}:{channel}",
                ),
            ]
        )

        with override_settings(DATABASE_ROUTERS=[RefuseImplicitMigrationDatabase()]):
            migration.normalize_invalid_rules(global_apps, connection.schema_editor())

        device_rule.refresh_from_db()
        channelless.refresh_from_db()
        self.assertFalse(device_rule.module_type_is_regex)
        self.assertEqual(device_rule.breakout_mode, BreakoutModeChoices.FLAT)
        self.assertEqual(device_rule.parent_name_template, "")
        self.assertEqual(channelless.breakout_mode, BreakoutModeChoices.FLAT)
        self.assertEqual(channelless.parent_name_template, "")
        # PostgreSQL validates every existing row here, so this only succeeds if the repair was complete.
        self._set_constraints(enabled=True)


class DeviceRuleScopeMigrationTest(TestCase):
    """The 0018 data migration must clear and report each device rule parent module type."""

    MIGRATION = "netbox_interface_name_rules.migrations.0018_refuse_device_rule_parent_module_type"

    def _set_constraint(self, migration, enabled):
        """Drop or restore the 0018 check constraint inside this test's transaction."""
        (operation,) = [op for op in migration.Migration.operations if isinstance(op, migrations.AddConstraint)]
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            if enabled:
                sql = operation.constraint.create_sql(InterfaceNameRule, connection.schema_editor())
            else:
                sql = operation.constraint.remove_sql(InterfaceNameRule, connection.schema_editor())
            cursor.execute(str(sql))

    def test_it_clears_and_logs_each_device_rule_parent_module_type(self):
        migration = import_module(self.MIGRATION)
        self._set_constraint(migration, enabled=False)
        manufacturer = Manufacturer.objects.create(name="ScopeMfg", slug="scopemfg")
        parent = ModuleType.objects.create(manufacturer=manufacturer, model="SCOPE-CVR", part_number="SCOPE-CVR")
        module_type = ModuleType.objects.create(manufacturer=manufacturer, model="SCOPE-SFP", part_number="SCOPE-SFP")
        device_rule, other_device_rule, module_rule = InterfaceNameRule.objects.bulk_create(
            [
                InterfaceNameRule(
                    applies_to_device_interfaces=True,
                    module_type_pattern="Gi.*",
                    parent_module_type=parent,
                    name_template="Gi{vc_position}/{port}",
                ),
                InterfaceNameRule(
                    applies_to_device_interfaces=True,
                    module_type_pattern="Te.*",
                    parent_module_type=parent,
                    name_template="Te{vc_position}/{port}",
                ),
                InterfaceNameRule(module_type=module_type, parent_module_type=parent, name_template="{base}"),
            ]
        )

        with (
            override_settings(DATABASE_ROUTERS=[RefuseImplicitMigrationDatabase()]),
            self.assertLogs(migration.logger, "WARNING") as logs,
        ):
            migration.clear_device_rule_parent_module_types(global_apps, connection.schema_editor())

        self.assertCountEqual(
            logs.output,
            [
                f"WARNING:{migration.__name__}:InterfaceNameRule ID {rule.pk}: cleared parent module type "
                f"{parent.pk} (SCOPE-CVR) from a device rule"
                for rule in (device_rule, other_device_rule)
            ],
        )
        for rule in (device_rule, other_device_rule, module_rule):
            rule.refresh_from_db()
        self.assertIsNone(device_rule.parent_module_type)
        self.assertIsNone(other_device_rule.parent_module_type)
        self.assertEqual(module_rule.parent_module_type, parent)
        # PostgreSQL validates every existing row here, so this only succeeds if the clearing was complete.
        self._set_constraint(migration, enabled=True)
