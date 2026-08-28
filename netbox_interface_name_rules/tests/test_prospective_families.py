# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Integration tests for prospective (rowless) interface-family plans.

A prospective plan says what a rule intends for interfaces described by name alone.  It is the
planner behind rowless prediction and the interactive preview, and it is never executable: the
same planning rules that installed execution follows, without a row to mutate.
"""

from unittest import skipIf, skipUnless

from dcim.models import Interface, InterfaceTemplate, Module
from django.test import SimpleTestCase

from netbox_interface_name_rules.choices import BreakoutModeChoices
from netbox_interface_name_rules.engine import build_variables, predict_rule_output, supports_channelization
from netbox_interface_name_rules.family import (
    FamilyStatus,
    FamilyTopology,
    MemberRole,
    ProspectiveFamilyPlan,
    ProspectiveFamilyPlanSet,
    ProspectiveMember,
    describe_interfaces,
    describe_template_interfaces,
    execute_installed_plan_set,
    execute_structural_family,
    plan_installed_families,
    plan_prospective_families,
    resolved_template_names,
)
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.test_breakout_mode import CHANNELIZED, _plain_module_type
from netbox_interface_name_rules.tests.test_channelization import (
    CHANNEL_TYPE,
    PLAIN_TYPE,
    REQUIRES_CHANNELIZATION,
    ChannelizationTestCase,
    _build_device,
    _channelized_module_type,
)

REQUIRES_NO_CHANNELIZATION = "requires a NetBox that cannot model channelized interfaces (4.6 and older)"


def _projection(plan):
    """Reduce one plan to the facts installed and prospective planning must agree on."""
    return (
        plan.topology,
        tuple(member.target_name for member in plan.members),
        plan.precondition_status,
        plan.precondition_reason,
    )


def _installed_projection(plan):
    """Reduce one installed plan to the same facts, reading targets from its snapshots."""
    return (
        plan.topology,
        tuple(member.target_name or member.snapshot.name for member in plan.members),
        plan.precondition_status,
        plan.precondition_reason,
    )


class ProspectivePlanSetLookupTest(SimpleTestCase):
    """The plan set answers for a name with what its family plan intends for that name."""

    PLAN = ProspectiveFamilyPlan(
        family_id="channelized:et0",
        topology=FamilyTopology.CHANNELIZED,
        base_name=None,
        members=(
            ProspectiveMember(source_name="et0", target_name="xe0", role=MemberRole.PARENT),
            ProspectiveMember(source_name="et0:1", target_name="xe0:1", role=MemberRole.CHANNEL, channel_id=1),
        ),
    )

    def test_a_member_of_an_existing_family_maps_to_its_own_target(self):
        plan_set = ProspectiveFamilyPlanSet(module_id=1, plans=(self.PLAN,))

        self.assertEqual(plan_set.predicted_names("et0"), ("xe0",))
        self.assertEqual(plan_set.predicted_names("et0:1"), ("xe0:1",))
        self.assertEqual(plan_set.predicted_names("et0:2"), ("et0:2",))

    def test_the_plan_lists_the_names_its_members_carry_now(self):
        self.assertEqual(self.PLAN.source_names, ("et0", "et0:1"))
        self.assertEqual(self.PLAN.target_names, ("xe0", "xe0:1"))


class ProspectivePlanTestCase(ChannelizationTestCase):
    """Plan the families a rule intends for a module, from its templates alone."""

    def _prospective(self, module, bay, rule, names=()):
        """Plan the families *rule* intends for the template names of *module*."""
        templates = resolved_template_names(module)
        return plan_prospective_families(
            module,
            rule,
            build_variables(bay, device=self.device),
            describe_template_interfaces(templates, names),
        )


class ProspectiveFlatPlanTest(ProspectivePlanTestCase):
    """A flat breakout rule expands one plain name into the sibling family it creates."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspFlat", ["3", "4"])
        cls.module_type = _plain_module_type(manufacturer, "ProspFlat-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=4,
            channel_start=0,
        )

    def test_a_plain_name_plans_the_flat_family_the_rule_creates(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)

        plan_set = self._prospective(module, bay, self.rule)

        self.assertEqual(len(plan_set.plans), 1)
        plan = plan_set.plans[0]
        self.assertEqual(plan.topology, FamilyTopology.FLAT)
        self.assertEqual(plan.base_name, "3")
        self.assertEqual(
            plan.target_names,
            ("xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"),
        )
        self.assertEqual({member.role for member in plan.members}, {MemberRole.FLAT_MEMBER})
        self.assertEqual(plan_set.predicted_names("3"), plan.target_names)

    def test_a_simple_rule_plans_one_member(self):
        rule = InterfaceNameRule(module_type=self.module_type, name_template="et-0/0/{bay_position}")
        module, bay = self._install(self.module_type, "4", run_rules=False)

        plan_set = self._prospective(module, bay, rule)

        self.assertEqual([plan.target_names for plan in plan_set.plans], [("et-0/0/4",)])
        self.assertEqual(plan_set.predicted_names("4"), ("et-0/0/4",))

    def test_an_unevaluable_template_fails_the_plan_and_keeps_the_name(self):
        rule = InterfaceNameRule(
            module_type=self.module_type,
            name_template="xe-{vc_position}/0/{bay_position}",
        )
        module, bay = self._install(self.module_type, "4", run_rules=False)

        plan_set = self._prospective(module, bay, rule)

        plan = plan_set.plans[0]
        self.assertEqual(plan.precondition_status, FamilyStatus.FAILED)
        self.assertEqual(plan.target_names, ("4",))
        self.assertEqual(plan_set.predicted_names("4"), ("4",))

    def test_a_name_no_plan_claims_predicts_to_itself(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)

        plan_set = self._prospective(module, bay, self.rule)

        self.assertEqual(plan_set.predicted_names("unclaimed"), ("unclaimed",))


@skipIf(supports_channelization(), REQUIRES_NO_CHANNELIZATION)
class ProspectiveUnsupportedTopologyTest(ProspectivePlanTestCase):
    """A release that cannot model channels plans an explicit unsupported family."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspUnsup", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ProspUnsup-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def test_the_plan_reports_unsupported_and_the_name_is_unchanged(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)

        plan_set = self._prospective(module, bay, self.rule)

        plan = plan_set.plans[0]
        self.assertEqual(plan.topology, FamilyTopology.CHANNELIZED)
        self.assertEqual(plan.precondition_status, FamilyStatus.UNSUPPORTED)
        self.assertEqual(plan.target_names, ("3",))
        self.assertEqual(plan_set.predicted_names("3"), ("3",))

    def test_prediction_leaves_the_name_alone(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)

        self.assertEqual(predict_rule_output(module, bay, ["3"]), ["3"])


class ProspectivePlansAreNotExecutableTest(ProspectivePlanTestCase):
    """A prospective plan describes intent; the executors refuse to take one."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspExec", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ProspExec-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            channel_count=2,
            channel_start=0,
        )

    def test_the_installed_executor_refuses_a_prospective_plan_set(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)
        plan_set = self._prospective(module, bay, self.rule)

        with self.assertRaises(TypeError):
            execute_installed_plan_set(plan_set)

        self.assertEqual(self._names(module), ["3"])

    def test_the_structural_executor_refuses_a_prospective_plan(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)
        plan = self._prospective(module, bay, self.rule).plans[0]

        with self.assertRaises(TypeError):
            execute_structural_family(plan)

        self.assertEqual(self._names(module), ["3"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ProspectiveChannelizedPlanTest(ProspectivePlanTestCase):
    """A rule on a channelized module type plans the family the templates already describe."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspChan", ["3", "5", "8", "9"])
        cls.breakout_type = _channelized_module_type(manufacturer, "ProspChan-BRK")
        cls.incomplete_type = _channelized_module_type(manufacturer, "ProspChan-INC", child_channel_ids=(1, 2, 3))
        cls.mismatch_type = _channelized_module_type(manufacturer, "ProspChan-MM", channels=8)
        cls.simple_type = _channelized_module_type(
            manufacturer,
            "ProspChan-SMP",
            child_names={1: "{module}:1", 2: "{module}:2", 3: "{module}:3", 4: "mgmt-chan"},
        )
        for module_type in (cls.breakout_type, cls.incomplete_type, cls.mismatch_type):
            InterfaceNameRule.objects.create(
                module_type=module_type,
                name_template="xe-0/0/{bay_position}:{channel}",
                channel_count=4,
                channel_start=0,
            )
        InterfaceNameRule.objects.create(module_type=cls.simple_type, name_template="et-0/0/{bay_position}")

    def _rule(self, module_type):
        return InterfaceNameRule.objects.get(module_type=module_type)

    def test_an_existing_family_is_renamed_in_place(self):
        module, bay = self._install(self.breakout_type, "3", run_rules=False)

        plan_set = self._prospective(module, bay, self._rule(self.breakout_type))

        self.assertEqual(len(plan_set.plans), 1)
        plan = plan_set.plans[0]
        self.assertEqual(plan.topology, FamilyTopology.CHANNELIZED)
        self.assertIsNone(plan.base_name)
        self.assertEqual(
            plan.target_names,
            ("3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"),
        )
        self.assertEqual(plan_set.predicted_names("3"), ("3",))
        self.assertEqual(plan_set.predicted_names("3:2"), ("xe-0/0/3:1",))

    def test_an_incomplete_family_stays_channelized(self):
        module, bay = self._install(self.incomplete_type, "5", run_rules=False)

        plan_set = self._prospective(module, bay, self._rule(self.incomplete_type))

        plan = plan_set.plans[0]
        self.assertEqual(plan.topology, FamilyTopology.CHANNELIZED)
        self.assertIsNone(plan.precondition_status)
        self.assertEqual(plan.target_names, ("5", "xe-0/0/5:0", "xe-0/0/5:1", "xe-0/0/5:2"))

    def test_a_channel_count_mismatch_blocks_the_family(self):
        module, bay = self._install(self.mismatch_type, "8", run_rules=False)

        plan_set = self._prospective(module, bay, self._rule(self.mismatch_type))

        plan = plan_set.plans[0]
        self.assertEqual(plan.precondition_status, FamilyStatus.BLOCKED)
        self.assertIn("8 channels", plan.precondition_reason)
        self.assertEqual(plan.target_names, ("8", "8:1", "8:2", "8:3", "8:4"))

    def test_an_ambiguous_channel_suffix_leaves_that_member_alone(self):
        module, bay = self._install(self.simple_type, "9", run_rules=False)

        plan_set = self._prospective(module, bay, self._rule(self.simple_type))

        plan = plan_set.plans[0]
        self.assertEqual(
            plan.target_names,
            ("et-0/0/9", "et-0/0/9:1", "et-0/0/9:2", "et-0/0/9:3", "mgmt-chan"),
        )
        stranded = [member for member in plan.members if member.source_name == "mgmt-chan"]
        self.assertEqual(stranded[0].reason, "channel suffix is ambiguous or unavailable")


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ProspectiveStructuralPlanTest(ProspectivePlanTestCase):
    """A channelized rule on a plain name plans the family it would build there."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspStruct", ["3", "4"])
        cls.module_type = _plain_module_type(manufacturer, "ProspStruct-QSFP")
        cls.collision_type = _plain_module_type(manufacturer, "ProspStruct-COL")
        InterfaceTemplate.objects.create(module_type=cls.collision_type, name="et-0/0/{module}", type=PLAIN_TYPE)
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )
        cls.collision_rule = InterfaceNameRule.objects.create(
            module_type=cls.collision_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            parent_name_template="et-0/0/{bay_position}",
            breakout_mode=CHANNELIZED,
            channel_count=4,
            channel_start=0,
        )

    def test_a_plain_name_plans_the_parent_and_every_channel(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)

        plan_set = self._prospective(module, bay, self.rule)

        plan = plan_set.plans[0]
        self.assertEqual(plan.topology, FamilyTopology.CHANNELIZED)
        self.assertEqual(plan.base_name, "3")
        self.assertEqual(
            plan.target_names,
            ("et-0/0/3", "xe-0/0/3:0", "xe-0/0/3:1", "xe-0/0/3:2", "xe-0/0/3:3"),
        )
        self.assertEqual([member.role for member in plan.members][0], MemberRole.PARENT)
        self.assertEqual(plan_set.predicted_names("3"), plan.target_names)

    def test_a_planned_name_another_interface_owns_blocks_the_family(self):
        """The structural executor refuses a family whose names are taken, so the plan says so first."""
        module, bay = self._install(self.collision_type, "4", run_rules=False)

        plan_set = self._prospective(module, bay, self.collision_rule)

        blocked = [plan for plan in plan_set.plans if plan.base_name == "4"]
        self.assertEqual(blocked[0].precondition_status, FamilyStatus.BLOCKED)
        self.assertIn("et-0/0/4", blocked[0].precondition_reason)
        self.assertEqual(plan_set.predicted_names("4"), ("4",))


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class ProspectiveMatchesInstalledPlanningTest(ProspectivePlanTestCase):
    """Equivalent installed and prospective inputs describe the same intended family."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspSame", ["3", "5"])
        cls.breakout_type = _channelized_module_type(manufacturer, "ProspSame-BRK")
        cls.mismatch_type = _channelized_module_type(manufacturer, "ProspSame-MM", channels=8)
        for module_type in (cls.breakout_type, cls.mismatch_type):
            InterfaceNameRule.objects.create(
                module_type=module_type,
                name_template="xe-0/0/{bay_position}:{channel}",
                channel_count=4,
                channel_start=0,
            )

    def _compare(self, module_type, position):
        """Plan the same module twice, from live rows and from its templates alone."""
        module, bay = self._install(module_type, position, run_rules=False)
        rule = InterfaceNameRule.objects.get(module_type=module_type)
        variables = build_variables(bay, device=self.device)
        installed = plan_installed_families(module, rule, variables)
        interfaces = list(Interface.objects.filter(module=module).order_by("pk"))
        prospective = plan_prospective_families(module, rule, variables, describe_interfaces(interfaces))
        return installed, prospective

    def test_a_channelized_family_plans_the_same_names_either_way(self):
        installed, prospective = self._compare(self.breakout_type, "3")

        self.assertEqual(
            [_installed_projection(plan) for plan in installed.plans],
            [_projection(plan) for plan in prospective.plans],
        )

    def test_a_blocked_family_gives_the_same_reason_either_way(self):
        installed, prospective = self._compare(self.mismatch_type, "5")

        self.assertEqual(
            [_installed_projection(plan) for plan in installed.plans],
            [_projection(plan) for plan in prospective.plans],
        )
        self.assertEqual(prospective.plans[0].precondition_status, FamilyStatus.BLOCKED)


class ProspectivePreviewIsNotAppliedTest(ChannelizationTestCase):
    """Interactive apply replans from live rows instead of executing an earlier preview."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspReplan", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ProspReplan-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="et-0/0/{bay_position}",
        )

    def test_a_rename_between_preview_and_apply_is_replanned(self):
        from netbox_interface_name_rules.engine import apply_rule_to_existing, find_interfaces_for_rule

        module, _bay = self._install(self.module_type, "3", run_rules=False)
        preview, _checked = find_interfaces_for_rule(self.rule)
        self.assertEqual(preview[0]["current_name"], "3")

        interface = Interface.objects.get(module=module)
        interface.name = "renamed-by-someone-else"
        interface.save()

        self.assertEqual(apply_rule_to_existing(self.rule, interface_ids=[interface.pk]).changed_count, 1)
        interface.refresh_from_db()
        self.assertEqual(interface.name, "et-0/0/3")

    def test_a_previewed_interface_that_disappeared_is_not_recreated(self):
        from netbox_interface_name_rules.engine import apply_rule_to_existing, find_interfaces_for_rule

        module, _bay = self._install(self.module_type, "3", run_rules=False)
        preview, _checked = find_interfaces_for_rule(self.rule)
        previewed_pk = preview[0]["interface"].pk

        Interface.objects.filter(pk=previewed_pk).delete()

        self.assertEqual(apply_rule_to_existing(self.rule, interface_ids=[previewed_pk]).changed_count, 0)
        self.assertEqual(Interface.objects.filter(module=module).count(), 0)


class ProspectivePlanningIsReadOnlyTest(ChannelizationTestCase):
    """Planning a prospective family never writes a row."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspRead", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ProspRead-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=BreakoutModeChoices.FLAT,
            channel_count=4,
            channel_start=0,
        )

    def test_planning_leaves_every_interface_exactly_as_it_was(self):
        module, bay = self._install(self.module_type, "3", run_rules=False)
        before = list(Interface.objects.filter(module=module).values_list("pk", "name"))

        plan_prospective_families(
            module,
            self.rule,
            build_variables(bay, device=self.device),
            describe_template_interfaces(resolved_template_names(module), ["3"]),
        )

        self.assertEqual(list(Interface.objects.filter(module=module).values_list("pk", "name")), before)
        self.assertEqual(Module.objects.filter(pk=module.pk).count(), 1)


class PreviewComesFromTheFamilyPlanTest(ChannelizationTestCase):
    """The interactive preview reports the plan's intended names and writes nothing."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspPrev", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ProspPrev-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="xe-0/0/{bay_position}:{channel}",
            breakout_mode=BreakoutModeChoices.FLAT,
            channel_count=4,
            channel_start=0,
        )

    def test_the_preview_names_are_the_planned_names(self):
        from netbox_interface_name_rules.engine import find_interfaces_for_rule

        module, bay = self._install(self.module_type, "3", run_rules=False)
        interfaces = list(Interface.objects.filter(module=module).order_by("pk"))
        plan = plan_prospective_families(
            module,
            self.rule,
            build_variables(bay, device=self.device),
            describe_interfaces(interfaces),
        ).plans[0]

        preview, checked = find_interfaces_for_rule(self.rule)

        self.assertEqual(checked, 1)
        self.assertEqual(preview[0]["new_names"], list(plan.target_names))
        self.assertEqual([detail.role for detail in preview[0]["name_details"]], ["channel"] * 4)

    def test_the_preview_changes_no_interface(self):
        from netbox_interface_name_rules.engine import find_interfaces_for_rule

        module, _bay = self._install(self.module_type, "3", run_rules=False)
        before = list(Interface.objects.filter(module=module).values_list("pk", "name"))

        find_interfaces_for_rule(self.rule)

        self.assertEqual(list(Interface.objects.filter(module=module).values_list("pk", "name")), before)


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class PreviewFollowsTheApplyClassificationTest(ChannelizationTestCase):
    """A channel row is never an independent candidate, however its parent is modelled."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspClass", ["3"])
        cls.module_type = _plain_module_type(manufacturer, "ProspClass-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="et-0/0/{bay_position}",
        )

    def _family(self):
        """Install a module and bind a channel to a parent that declares no channel count."""
        module, _bay = self._install(self.module_type, "3", run_rules=False)
        parent = Interface.objects.get(module=module)
        child = Interface.objects.create(
            device=self.device, module=module, name="3:1", type=CHANNEL_TYPE, parent=parent, channel_id=1
        )
        return module, parent, child

    def test_such_a_channel_is_previewed_with_its_parent_and_never_on_its_own(self):
        """The apply path carries such a child along with its parent, so the preview must too."""
        from netbox_interface_name_rules.engine import find_interfaces_for_rule

        _module, parent, _child = self._family()

        preview, checked = find_interfaces_for_rule(self.rule)

        self.assertEqual(checked, 1)
        self.assertEqual([entry["interface"].pk for entry in preview], [parent.pk])
        self.assertEqual(preview[0]["new_names"], ["et-0/0/3", "et-0/0/3:1"])

    def test_applying_to_the_channel_alone_changes_nothing(self):
        """A previewed name always belongs to a parent, so selecting the channel is not a candidate."""
        from netbox_interface_name_rules.engine import apply_rule_to_existing

        module, _parent, child = self._family()

        self.assertEqual(apply_rule_to_existing(self.rule, interface_ids=[child.pk]).changed_count, 0)
        self.assertEqual(self._names(module), ["3", "3:1"])


@skipUnless(supports_channelization(), REQUIRES_CHANNELIZATION)
class PreviewReadsNoTemplatesForDerivableSuffixesTest(ChannelizationTestCase):
    """A channel whose own name carries its parent's prefix needs no template lookup."""

    @classmethod
    def setUpTestData(cls):
        manufacturer, cls.device = _build_device("ProspQuery", ["3"])
        cls.module_type = _channelized_module_type(manufacturer, "ProspQuery-QSFP")
        cls.rule = InterfaceNameRule.objects.create(
            module_type=cls.module_type,
            name_template="et-0/0/{bay_position}",
        )

    def test_previewing_a_channelized_family_reads_no_interface_templates(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from netbox_interface_name_rules.engine import find_interfaces_for_rule

        self._install(self.module_type, "3", run_rules=False)

        with CaptureQueriesContext(connection) as queries:
            preview, _checked = find_interfaces_for_rule(self.rule)

        self.assertEqual(len(preview), 1)
        template_reads = [
            query["sql"] for query in queries.captured_queries if "dcim_interfacetemplate" in query["sql"]
        ]
        self.assertEqual(template_reads, [])
