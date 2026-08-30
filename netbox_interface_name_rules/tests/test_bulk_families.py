# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for bulk family application and virtual-chassis reapplication."""

from unittest import skipUnless
from unittest.mock import MagicMock, patch

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
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from netbox_interface_name_rules import engine as engine_module
from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.engine import (
    apply_interface_name_rules,
    apply_rule_to_existing,
    build_variables,
    find_interfaces_for_rule,
    supports_channelization,
)
from netbox_interface_name_rules.family import (
    FamilyStatus,
    FamilyTopology,
    InstalledFamilyPlan,
    MemberRole,
    PlannedMember,
    StructuralFamilyPlan,
    describe_interfaces,
    execute_family_plan,
    execute_flat_family,
    execute_module_families,
    pinned_template_cache,
    plan_flat_family,
    plan_prospective_families,
    template_names,
)
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.signals import _apply_rules_for_device_deferred
from netbox_interface_name_rules.tests.out_of_band import rename_out_of_band
from netbox_interface_name_rules.tests.test_channelization import _channelized_module_type

PLAIN_TYPE = InterfaceTypeChoices.TYPE_10GE_SFP_PLUS
REQUIRES_CHANNELIZATION = "requires a NetBox that models channelized interfaces"


class BulkTestCase(TestCase):
    """One device with eight bays, so a batch can hold eight modules of one type."""

    POSITIONS = ("1", "2", "3", "4", "5", "6", "7", "8")

    @classmethod
    def setUpTestData(cls):
        cls.manufacturer = Manufacturer.objects.create(name="BulkMfg", slug="bulk-mfg")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer,
            model="BULK-DEVICE",
            slug="bulk-device",
        )
        for position in cls.POSITIONS:
            ModuleBayTemplate.objects.create(device_type=cls.device_type, name=f"Bay {position}", position=position)
        cls.role = DeviceRole.objects.create(name="BulkRole", slug="bulk-role")
        cls.site = Site.objects.create(name="BulkSite", slug="bulk-site")
        cls.device = Device.objects.create(
            name="bulk-device-01",
            device_type=cls.device_type,
            role=cls.role,
            site=cls.site,
        )
        cls.module_type = cls._module_type("BULK-QSFP", ("{module}",))

    @classmethod
    def _module_type(cls, model, template_names):
        """Create a module type whose templates resolve to one interface each."""
        module_type = ModuleType.objects.create(manufacturer=cls.manufacturer, model=model, part_number=model)
        for name in template_names:
            InterfaceTemplate.objects.create(module_type=module_type, name=name, type=PLAIN_TYPE)
        return module_type

    def _install(self, position, module_type=None):
        """Install one module without letting the deferred install callback run."""
        return Module.objects.create(
            device=self.device,
            module_bay=ModuleBay.objects.get(device=self.device, name=f"Bay {position}"),
            module_type=module_type or self.module_type,
        )

    @staticmethod
    def _module_refetches(queries):
        """Return the queries that read one whole module row back by primary key."""
        return [
            query["sql"]
            for query in queries
            if query["sql"].startswith('SELECT "dcim_module"."id"') and 'WHERE "dcim_module"."id" = ' in query["sql"]
        ]

    def _names(self, module):
        """Return the module's interface names in stable order."""
        return sorted(Interface.objects.filter(module=module).values_list("name", flat=True))

    @staticmethod
    def _flat_rule(module_type, **kwargs):
        """Create a flat breakout rule that names four siblings per port."""
        fields = {
            "module_type": module_type,
            "name_template": "xe-0/0/{bay_position}:{channel}",
            "channel_count": 4,
            "channel_start": 0,
        }
        fields.update(kwargs)
        return InterfaceNameRule.objects.create(**fields)


class BulkApplyBuildsFlatFamiliesTest(BulkTestCase):
    """A retroactive apply builds the family a rule describes on every module it matches."""

    def setUp(self):
        self.rule = self._flat_rule(self.module_type)
        self.modules = [self._install(position) for position in ("1", "2")]

    def test_the_outcome_reports_every_interface_the_batch_renamed_or_created(self):
        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.changed_count, 8)
        self.assertEqual(self._names(self.modules[0]), ["xe-0/0/1:0", "xe-0/0/1:1", "xe-0/0/1:2", "xe-0/0/1:3"])
        self.assertEqual(self._names(self.modules[1]), ["xe-0/0/2:0", "xe-0/0/2:1", "xe-0/0/2:2", "xe-0/0/2:3"])

    def test_the_outcome_carries_one_family_per_module(self):
        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(len(outcome.families), 2)
        self.assertEqual({family.topology for family in outcome.families}, {FamilyTopology.FLAT})
        self.assertEqual({family.status for family in outcome.families}, {FamilyStatus.CHANGED})

    def test_no_interface_belongs_to_two_families(self):
        outcome = apply_rule_to_existing(self.rule)

        claimed = [member.interface_pk for family in outcome.families for member in family.members]
        self.assertEqual(len(claimed), len(set(claimed)))

    def test_applying_the_rule_again_changes_nothing(self):
        apply_rule_to_existing(self.rule)

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(self._names(self.modules[0]), ["xe-0/0/1:0", "xe-0/0/1:1", "xe-0/0/1:2", "xe-0/0/1:3"])

    def test_a_family_the_batch_completes_is_reported_once(self):
        """A half-built family is finished through the base it already named, not through each sibling."""
        apply_rule_to_existing(self.rule)
        Interface.objects.filter(module=self.modules[0], name__in=("xe-0/0/1:2", "xe-0/0/1:3")).delete()

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.changed_count, 2)
        self.assertEqual(self._names(self.modules[0]), ["xe-0/0/1:0", "xe-0/0/1:1", "xe-0/0/1:2", "xe-0/0/1:3"])


class BulkApplyRenamesPlainInterfacesTest(BulkTestCase):
    """A rule that names no family still reports what it renamed as an explicit outcome."""

    def setUp(self):
        self.rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-{bay_position}/0/0",
        )
        self.module = self._install("1")

    def test_a_simple_rename_is_reported_as_a_one_member_family(self):
        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.changed_count, 1)
        self.assertEqual(self._names(self.module), ["et-1/0/0"])
        self.assertEqual(len(outcome.families), 1)
        self.assertEqual(outcome.families[0].members[0].current_name, "1")
        self.assertEqual(outcome.families[0].members[0].target_name, "et-1/0/0")

    def test_a_name_another_interface_owns_is_reported_as_a_skip(self):
        Interface.objects.create(device=self.device, name="et-1/0/0", type=PLAIN_TYPE)

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual([member.status for member in outcome.families[0].members], [FamilyStatus.BLOCKED])
        self.assertEqual(len(outcome.skipped_members), 1)

    def test_every_interface_on_the_module_is_renamed(self):
        """A simple rule owns no family, so a second port is a second candidate, not a duplicate."""
        module_type = self._module_type("BULK-DUAL", ("{module}/a", "{module}/b"))
        module = self._install("2", module_type=module_type)
        InterfaceNameRule.objects.create(module_type=module_type, name_template="et-{base}")

        apply_rule_to_existing(InterfaceNameRule.objects.get(module_type=module_type))

        self.assertEqual(self._names(module), ["et-2/a", "et-2/b"])


class BulkApplySelectionTest(BulkTestCase):
    """The Apply view submits interface primary keys, and only their families are applied."""

    def setUp(self):
        self.rule = self._flat_rule(self.module_type)
        self.modules = [self._install(position) for position in ("1", "2")]

    def test_selecting_one_base_applies_only_its_family(self):
        base = Interface.objects.get(module=self.modules[0])

        outcome = apply_rule_to_existing(self.rule, interface_ids=[base.pk])

        self.assertEqual(outcome.changed_count, 4)
        self.assertEqual(self._names(self.modules[1]), ["2"])

    def test_an_empty_selection_touches_nothing(self):
        outcome = apply_rule_to_existing(self.rule, interface_ids=[])

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(outcome.families, ())

    def test_the_limit_stops_the_batch_after_the_module_that_reached_it(self):
        outcome = apply_rule_to_existing(self.rule, limit=1)

        self.assertEqual(outcome.changed_count, 4)
        self.assertEqual(self._names(self.modules[1]), ["2"])

    def test_a_disabled_rule_applies_nothing(self):
        self.rule.enabled = False
        self.rule.save()

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual(outcome.families, ())


class BulkApplyContinuesPastOneBlockedFamilyTest(BulkTestCase):
    """One module that cannot take its names must not cost the rest of the batch its own."""

    def setUp(self):
        self.rule = self._flat_rule(self.module_type)
        self.modules = [self._install(position) for position in ("1", "2")]

    def test_a_collision_on_the_first_module_leaves_the_second_renamed(self):
        Interface.objects.create(device=self.device, name="xe-0/0/1:0", type=PLAIN_TYPE)

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(self._names(self.modules[1]), ["xe-0/0/2:0", "xe-0/0/2:1", "xe-0/0/2:2", "xe-0/0/2:3"])
        self.assertEqual(len(outcome.skipped_members), 1)
        self.assertEqual(outcome.skipped_members[0].target_name, "xe-0/0/1:0")

    def test_an_unexpected_family_failure_is_logged_and_the_batch_continues(self):
        """The boundary keeps going: one family's failure is visible, never the batch's end."""
        from netbox_interface_name_rules.family import batch

        execute = batch.execute_family_plan

        def fail_on_the_first_module(plan):
            if plan.module_id == self.modules[0].pk:
                raise ValueError("family boom")
            return execute(plan)

        with (
            patch.object(batch, "execute_family_plan", side_effect=fail_on_the_first_module),
            self.assertLogs("netbox_interface_name_rules.family.batch", level="ERROR"),
        ):
            outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(self._names(self.modules[0]), ["1"])
        self.assertEqual(self._names(self.modules[1]), ["xe-0/0/2:0", "xe-0/0/2:1", "xe-0/0/2:2", "xe-0/0/2:3"])
        self.assertEqual(outcome.changed_count, 4)
        self.assertEqual([family.status for family in outcome.families], [FamilyStatus.FAILED, FamilyStatus.CHANGED])
        self.assertEqual([member.target_name for member in outcome.skipped_members], ["xe-0/0/1:0"])


class BulkApplyQueryScalingTest(BulkTestCase):
    """The Apply button runs this over a fleet, so its cost cannot grow per module or per family."""

    def setUp(self):
        self.rule = self._flat_rule(self.module_type)

    def _apply_queries(self):
        """Return the queries one full batch runs."""
        with CaptureQueriesContext(connection) as captured:
            apply_rule_to_existing(self.rule)
        return captured.captured_queries

    def test_the_batch_reads_the_interface_templates_once_for_the_module_type(self):
        for position in ("1", "2", "3", "4"):
            self._install(position)

        queries = self._apply_queries()

        template_queries = [query for query in queries if "dcim_interfacetemplate" in query["sql"]]
        self.assertEqual(len(template_queries), 1, [query["sql"] for query in queries])

    def test_the_batch_never_refetches_a_module_it_already_holds(self):
        for position in ("1", "2", "3", "4"):
            self._install(position)

        queries = self._apply_queries()

        self.assertEqual(self._module_refetches(queries), [])

    def test_eight_same_type_families_cost_no_more_per_module_than_two(self):
        for position in ("1", "2"):
            self._install(position)
        two_modules = len(self._apply_queries())
        for position in ("3", "4", "5", "6", "7", "8"):
            self._install(position)

        eight_modules = len(self._apply_queries())

        per_module = (eight_modules - two_modules) / 6
        self.assertLessEqual(per_module, (two_modules / 2), [per_module, two_modules])


class PinnedTemplateCacheBalanceTest(BulkTestCase):
    """The batch template cache has to be released even when setting it up fails.

    A stranded cache outlives the batch that opened it, so every later batch on that thread reads
    the failed batch's resolved template names instead of its own.  On a long-lived worker that is
    a rename against names the modules no longer carry.
    """

    def _setup_failure(self):
        """Enter the cache with a batch that raises while it is being pinned."""
        broken = MagicMock()
        type(broken).pk = property(lambda _self: (_ for _ in ()).throw(RuntimeError("module went away")))
        return pinned_template_cache([broken])

    def test_a_failed_setup_releases_the_cache(self):
        """Anything left behind here is served to the next batch as its own template names."""
        with self.assertRaises(RuntimeError), self._setup_failure():
            pass  # pragma: no cover - the block never runs

        self.assertNotIn("resolved", template_names._pin.__dict__)
        self.assertNotIn("chained", template_names._pin.__dict__)
        self.assertEqual(getattr(template_names._pin, "depth", 0), 0)

    def test_a_later_batch_still_resolves_its_own_names(self):
        """The damage the balance prevents: one batch's names outliving it and serving the next one."""
        self._install("1")
        rule = self._flat_rule(self.module_type)
        with self.assertRaises(RuntimeError), self._setup_failure():
            pass  # pragma: no cover - the block never runs
        apply_rule_to_existing(rule)  # fills a cache that a stranded pin would keep

        with CaptureQueriesContext(connection) as captured:
            apply_rule_to_existing(rule)

        template_queries = [query for query in captured.captured_queries if "dcim_interfacetemplate" in query["sql"]]
        self.assertEqual(len(template_queries), 1, [query["sql"] for query in captured.captured_queries])


class VirtualChassisReapplyTestCase(BulkTestCase):
    """A position change reaches every module-attached family through the plugin's own signals."""

    def setUp(self):
        self.rule = InterfaceNameRule.objects.create(
            module_type=self.module_type,
            name_template="et-{vc_position}/0/{bay_position}",
        )
        self.virtual_chassis = VirtualChassis.objects.create(name="BulkVC")

    def _install_and_name(self, positions):
        """Install one module per bay position and give it the name the rule produces now."""
        modules = []
        for position in positions:
            module = self._install(position)
            apply_interface_name_rules(module, module.module_bay)
            modules.append(module)
        return modules

    def _join(self, position):
        """Move the device to *position* in the virtual chassis through a real save."""
        with self.captureOnCommitCallbacks(execute=True):
            self.device.virtual_chassis = self.virtual_chassis
            self.device.vc_position = position
            self.device.save()


class VirtualChassisReapplyTest(VirtualChassisReapplyTestCase):
    """Every module family the position names has to move with it."""

    def test_every_module_family_follows_the_new_position(self):
        modules = self._install_and_name(("1", "2", "3"))

        self._join(4)

        for module, position in zip(modules, ("1", "2", "3"), strict=True):
            self.assertEqual(self._names(module), [f"et-4/0/{position}"])

    def test_a_device_level_rule_still_runs_beside_the_module_families(self):
        module = self._install_and_name(("1",))[0]
        Interface.objects.create(device=self.device, name="mgmt0", type=PLAIN_TYPE)
        InterfaceNameRule.objects.create(
            name_template="mgmt-{vc_position}",
            applies_to_device_interfaces=True,
            module_type_pattern="mgmt0",
        )

        self._join(4)

        self.assertEqual(self._names(module), ["et-4/0/1"])
        self.assertTrue(Interface.objects.filter(device=self.device, module=None, name="mgmt-4").exists())

    def test_one_failing_module_leaves_the_others_reapplied(self):
        modules = self._install_and_name(("1", "2"))
        real_apply = engine_module.apply_interface_name_rules

        def fail_on_the_first_module(module, module_bay, force_reapply=False):
            if module.pk == modules[0].pk:
                raise RuntimeError("module boom")
            return real_apply(module, module_bay, force_reapply=force_reapply)

        with (
            patch.object(engine_module, "apply_interface_name_rules", side_effect=fail_on_the_first_module),
            self.assertLogs("netbox_interface_name_rules", level="ERROR"),
        ):
            self._join(4)

        self.assertEqual(self._names(modules[0]), ["1"])
        self.assertEqual(self._names(modules[1]), ["et-4/0/2"])


class VirtualChassisReapplyCostTest(VirtualChassisReapplyTestCase):
    """A chassis-wide position change must not read one module type's templates once per module."""

    def _reapply_queries(self, position):
        """Return the queries the deferred reapplication runs for one position change."""
        self.device.virtual_chassis = self.virtual_chassis
        self.device.vc_position = position
        self.device.save()
        with CaptureQueriesContext(connection) as captured:
            _apply_rules_for_device_deferred(self.device.pk)
        return captured.captured_queries

    def test_the_reapply_reads_the_interface_templates_once_for_the_module_type(self):
        self._install_and_name(("1", "2", "3", "4"))

        queries = self._reapply_queries(4)

        template_queries = [query for query in queries if "dcim_interfacetemplate" in query["sql"]]
        self.assertEqual(len(template_queries), 1, [query["sql"] for query in queries])

    def test_the_reapply_never_refetches_a_module_it_already_holds(self):
        self._install_and_name(("1", "2", "3", "4"))

        queries = self._reapply_queries(4)

        self.assertEqual(self._module_refetches(queries), [])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class BulkApplyChannelizedFamiliesTest(BulkTestCase):
    """Eight channelized families in one batch stay eight families, each renamed as a unit."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.channelized_type = _channelized_module_type(cls.manufacturer, "BULK-CHANNELIZED")

    def setUp(self):
        self.rule = self._flat_rule(self.channelized_type, name_template="xe-0/0/{bay_position}:{channel}")

    def _install_families(self, positions):
        """Install one channelized module per bay position."""
        return [self._install(position, module_type=self.channelized_type) for position in positions]

    def test_every_installed_family_is_renamed_as_one_family(self):
        modules = self._install_families(self.POSITIONS)

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(len(outcome.families), len(self.POSITIONS))
        self.assertEqual({family.topology for family in outcome.families}, {FamilyTopology.CHANNELIZED})
        self.assertEqual(
            self._names(modules[0]),
            ["1", "xe-0/0/1:0", "xe-0/0/1:1", "xe-0/0/1:2", "xe-0/0/1:3"],
        )

    def test_no_channel_belongs_to_two_families(self):
        self._install_families(self.POSITIONS)

        outcome = apply_rule_to_existing(self.rule)

        claimed = [member.interface_pk for family in outcome.families for member in family.members]
        self.assertEqual(len(claimed), len(set(claimed)))

    def test_a_family_missing_a_channel_still_names_the_channels_it_has(self):
        """A channel is named from its own channel id, so a family that lost one is still repaired."""
        modules = self._install_families(("1",))
        Interface.objects.filter(module=modules[0], channel_id=2).delete()

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(self._names(modules[0]), ["1", "xe-0/0/1:0", "xe-0/0/1:2", "xe-0/0/1:3"])
        self.assertEqual(outcome.skipped_members, ())

    def _apply_queries(self, positions):
        """Return the queries one batch runs over freshly installed families at *positions*."""
        self._install_families(positions)
        with CaptureQueriesContext(connection) as captured:
            apply_rule_to_existing(self.rule)
        return captured.captured_queries

    def test_the_batch_reads_the_interface_templates_once_for_the_module_type(self):
        queries = self._apply_queries(self.POSITIONS)

        template_queries = [query for query in queries if "dcim_interfacetemplate" in query["sql"]]
        self.assertEqual(len(template_queries), 1, [query["sql"] for query in queries])

    def test_eight_channelized_families_cost_no_more_per_module_than_the_first(self):
        """Renaming a fleet stays linear in its modules: no family pays for the ones beside it."""
        added = self.POSITIONS[1:]
        one_module = len(self._apply_queries(("1",)))

        eight_modules = len(self._apply_queries(added))

        per_module = (eight_modules - one_module) / len(added)
        self.assertLessEqual(per_module, one_module, [per_module, one_module, eight_modules])

    def test_selecting_a_channel_on_its_own_applies_nothing(self):
        modules = self._install_families(("1",))
        channel = Interface.objects.filter(module=modules[0], channel_id__isnull=False).first()

        outcome = apply_rule_to_existing(self.rule, interface_ids=[channel.pk])

        self.assertEqual(outcome.changed_count, 0)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class BulkApplyBuildsChannelizedFamiliesTest(BulkTestCase):
    """A channelized rule builds one family per module, and reports a refusal as one outcome."""

    def setUp(self):
        self.rule = self._flat_rule(
            self.module_type,
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            parent_name_template="et-0/0/{bay_position}",
        )
        self.modules = [self._install(position) for position in ("1", "2")]

    def test_each_module_gains_the_family_the_rule_describes(self):
        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(len(outcome.families), 2)
        self.assertEqual(
            self._names(self.modules[0]),
            ["et-0/0/1", "xe-0/0/1:0", "xe-0/0/1:1", "xe-0/0/1:2", "xe-0/0/1:3"],
        )

    def test_a_module_whose_parent_name_is_taken_is_reported_and_the_next_still_builds(self):
        Interface.objects.create(device=self.device, name="et-0/0/1", type=PLAIN_TYPE)

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(self._names(self.modules[0]), ["1"])
        self.assertEqual(
            self._names(self.modules[1]),
            ["et-0/0/2", "xe-0/0/2:0", "xe-0/0/2:1", "xe-0/0/2:2", "xe-0/0/2:3"],
        )
        self.assertEqual(len(outcome.skipped_members), 1)


class FlatFamilyCreationTest(BulkTestCase):
    """The rows a flat breakout family adds, and what stops it from adding them."""

    def setUp(self):
        self.rule = self._flat_rule(self.module_type)
        self.module = self._install("1")
        self.base = Interface.objects.get(module=self.module)
        self.variables = build_variables(self.module.module_bay, device=self.device)

    def _plan(self):
        """Plan the flat family the rule builds on this module's only interface."""
        return plan_flat_family(self.module, self.rule, self.variables, self.base)

    def test_a_sibling_whose_name_is_taken_is_skipped_and_the_rest_are_created(self):
        """One sibling's collision is that sibling's own; the family keeps the names it can take."""
        Interface.objects.create(device=self.device, name="xe-0/0/1:2", type=PLAIN_TYPE)

        outcome = execute_flat_family(self._plan())

        self.assertEqual(outcome.status, FamilyStatus.CHANGED)
        self.assertEqual(outcome.changed_count, 3)
        blocked = [member for member in outcome.members if member.status == FamilyStatus.BLOCKED]
        self.assertEqual([member.target_name for member in blocked], ["xe-0/0/1:2"])
        self.assertEqual(self._names(self.module), ["xe-0/0/1:0", "xe-0/0/1:1", "xe-0/0/1:3"])

    def test_a_base_renamed_after_planning_is_refused_as_stale(self):
        """Planning is not a lock: the row it described has to still be the row it finds."""
        plan = self._plan()
        rename_out_of_band(Interface.objects.get(pk=self.base.pk), "moved")

        outcome = execute_flat_family(plan)

        self.assertEqual(outcome.status, FamilyStatus.STALE)
        self.assertEqual(self._names(self.module), ["moved"])

    def test_a_name_netbox_rejects_leaves_the_whole_family_unbuilt(self):
        """A name the model refuses is not a partial family: nothing is written at all."""
        self.rule.name_template = "x" * 70 + "{channel}"
        self.rule.save()

        outcome = execute_flat_family(self._plan())

        self.assertEqual(outcome.status, FamilyStatus.BLOCKED)
        self.assertEqual(self._names(self.module), ["1"])

    def test_a_template_the_rule_cannot_evaluate_builds_nothing(self):
        """The plan carries the failure, so the executor writes nothing and says why."""
        self.rule.name_template = "{undefined_var}:{channel}"
        self.rule.save()

        outcome = execute_flat_family(self._plan())

        self.assertEqual(outcome.status, FamilyStatus.FAILED)
        self.assertEqual(self._names(self.module), ["1"])

    def test_a_simple_rule_the_engine_cannot_evaluate_is_reported_as_a_failure(self):
        """A one-interface rename carries its template failure the same way a family does."""
        rule = InterfaceNameRule.objects.create(
            module_type=self._module_type("BULK-SIMPLE-FAIL", ("{module}",)),
            name_template="{undefined_var}",
        )
        module = self._install("2", module_type=rule.module_type)

        outcome = apply_rule_to_existing(rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual([member.status for member in outcome.skipped_members], [FamilyStatus.FAILED])
        self.assertEqual(self._names(module), ["2"])


class OnlyLivePlansAreExecutableTest(BulkTestCase):
    """A plan without live rows cannot reach an executor, whichever door it arrives at."""

    def setUp(self):
        self.rule = self._flat_rule(self.module_type)
        self.module = self._install("1")

    def _prospective_plan(self):
        """Return one prospective plan for this module: the object an interactive preview holds."""
        plan_set = plan_prospective_families(
            self.module,
            self.rule,
            build_variables(self.module.module_bay, device=self.device),
            describe_interfaces(Interface.objects.filter(module=self.module)),
        )
        return plan_set.plans[0]

    def test_the_batch_executor_refuses_a_prospective_plan(self):
        with self.assertRaises(TypeError):
            execute_family_plan(self._prospective_plan())

        self.assertEqual(self._names(self.module), ["1"])

    def test_the_flat_executor_refuses_a_prospective_plan(self):
        with self.assertRaises(TypeError):
            execute_flat_family(self._prospective_plan())

        self.assertEqual(self._names(self.module), ["1"])

    def test_every_plan_kind_retains_its_live_members_after_an_executor_failure(self):
        base = Interface.objects.get(module=self.module)
        flat = plan_flat_family(
            self.module,
            self.rule,
            build_variables(self.module.module_bay, device=self.device),
            base,
        )
        installed = InstalledFamilyPlan(
            family_id=f"installed:{base.pk}",
            topology=FamilyTopology.FLAT,
            device_id=self.device.pk,
            module_id=self.module.pk,
            db_alias=flat.db_alias,
            members=(PlannedMember(flat.base, "installed-target", MemberRole.FLAT_MEMBER),),
        )
        structural = StructuralFamilyPlan(
            family_id=f"structural:{base.pk}",
            device_id=self.device.pk,
            module_id=self.module.pk,
            module_type_id=self.module_type.pk,
            db_alias=flat.db_alias,
            base=flat.base,
            parent_target_name="parent-target",
            channel_count=0,
            channels=(),
        )

        with (
            patch("netbox_interface_name_rules.family.batch.execute_family_plan", side_effect=ValueError("boom")),
            self.assertLogs("netbox_interface_name_rules.family.batch", level="ERROR"),
        ):
            outcomes = execute_module_families(self.rule, self.module, (installed, structural, flat))

        self.assertEqual([outcome.status for outcome in outcomes], [FamilyStatus.FAILED] * 3)
        self.assertEqual(
            [outcome.members[0].target_name for outcome in outcomes],
            ["installed-target", "parent-target", "xe-0/0/1:0"],
        )


class BulkApplyReportsSkipsToItsCallersTest(BulkTestCase):
    """The Apply view and the background job both read their counts from the batch outcome."""

    def setUp(self):
        self.rule = self._flat_rule(self.module_type)
        self.module = self._install("1")
        Interface.objects.create(device=self.device, name="xe-0/0/1:0", type=PLAIN_TYPE)
        for channel in (1, 2, 3):
            Interface.objects.create(
                device=self.device, module=self.module, name=f"xe-0/0/1:{channel}", type=PLAIN_TYPE
            )

    def test_a_family_that_can_take_no_name_is_reported_blocked(self):
        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.changed_count, 0)
        self.assertEqual([family.status for family in outcome.families], [FamilyStatus.BLOCKED])
        self.assertEqual([member.target_name for member in outcome.skipped_members], ["xe-0/0/1:0"])

    def test_the_background_job_warns_about_what_it_skipped(self):
        from netbox_interface_name_rules.jobs import ApplyRuleJob

        job = ApplyRuleJob.__new__(ApplyRuleJob)
        job.logger = MagicMock()

        job.run(rule_id=self.rule.pk)

        job.logger.info.assert_called_once()
        job.logger.warning.assert_called_once()
        self.assertEqual(job.logger.warning.call_args.args[1], 1)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class BulkApplyBuildsOneFamilyPerPortTest(BulkTestCase):
    """Every port a channelized rule names gets its own family, however many the module carries."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.dual_type = cls._module_type("BULK-DUAL-CH", ("{module}/a", "{module}/b"))

    def setUp(self):
        self.rule = self._flat_rule(
            self.dual_type,
            name_template="{base}:{channel}",
            parent_name_template="{base}",
            breakout_mode=BreakoutModeChoices.CHANNELIZED,
            channel_start=1,
        )
        self.module = self._install("1", module_type=self.dual_type)

    def test_both_ports_gain_the_family_the_rule_describes(self):
        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual(outcome.skipped_members, ())
        self.assertEqual(
            self._names(self.module),
            ["1/a", "1/a:1", "1/a:2", "1/a:3", "1/a:4", "1/b", "1/b:1", "1/b:2", "1/b:3", "1/b:4"],
        )

    def test_the_preview_offers_exactly_the_families_the_apply_builds(self):
        previewed, _total = find_interfaces_for_rule(self.rule)

        outcome = apply_rule_to_existing(self.rule)

        self.assertEqual([entry["current_name"] for entry in previewed], ["1/a", "1/b"])
        self.assertEqual(len(outcome.families), len(previewed))
        self.assertEqual({family.status for family in outcome.families}, {FamilyStatus.CHANGED})
