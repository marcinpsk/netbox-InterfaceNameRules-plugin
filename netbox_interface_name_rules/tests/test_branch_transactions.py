# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The plugin's write scope and blocks across ``default`` and the connection of a real netbox-branching branch."""

from contextlib import ExitStack

from dcim.models import Interface, Site
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DataError, IntegrityError, InternalError, OperationalError, connections, router, transaction
from django.test import override_settings
from extras.models import SavedFilter, Webhook
from netbox.context import events_queue

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.tests.branch_cases import BRANCH_BEFORE, DEFAULT_BEFORE, SERVER_DEFAULT, BranchTestCase
from netbox_interface_name_rules.tests.helpers import (
    activate,
    lock_timeout,
    make_manufacturer,
    make_module_type,
    request_context,
    set_lock_timeout,
)
from netbox_interface_name_rules.transactions import (
    LOCK_TIMEOUT,
    SET_LOCK_TIMEOUT,
    atomic_with_events,
    on_commit,
    write_scope,
)

User = get_user_model()


def abort_the_transaction(alias):
    """Make PostgreSQL refuse every later statement of the open transaction on *alias*."""
    with connections[alias].cursor() as cursor:
        try:
            cursor.execute("SELECT 1 / 0")
        except DataError:
            return
    raise AssertionError("the statement did not fail")


def refuse_the_restore(execute, sql, params, many, context):
    """Fail the statement that gives a connection its earlier ``lock_timeout`` back, before PostgreSQL runs it."""
    if sql == SET_LOCK_TIMEOUT and params != [LOCK_TIMEOUT]:
        raise OperationalError("injected restore failure")
    return execute(sql, params, many, context)


class _ScopeCase(BranchTestCase):
    """One branch, and a distinct ``lock_timeout`` on each connection."""

    def setUp(self):
        user = User.objects.create_user(username=f"{type(self).__name__.lower()}-operator")
        self.branch = self.provision_branch(type(self).__name__[:40], user)
        self.alias = self.branch.connection_name
        self.addCleanup(set_lock_timeout, "default", SERVER_DEFAULT)
        set_lock_timeout("default", DEFAULT_BEFORE)
        set_lock_timeout(self.alias, BRANCH_BEFORE)

    def alias_of(self, name):
        return self.alias if name == "branch" else "default"

    def timeouts(self):
        return lock_timeout("default"), lock_timeout(self.alias)


class WriteScopeInABranchTest(_ScopeCase):
    def test_the_scope_pins_default_and_the_branch_alias(self):
        with activate(self.branch), write_scope() as aliases:
            self.assertEqual(aliases, ("default", self.alias))

    def test_an_unexpected_write_alias_raises_before_any_query(self):
        with activate(self.branch), self.assertNumQueries(0), self.assertNumQueries(0, using=self.alias):
            with self.assertRaisesMessage(RuntimeError, f"'{self.alias}'"), write_scope(expected_alias="default"):
                self.fail("the scope opened")

    def test_a_nested_scope_on_another_alias_raises(self):
        with write_scope(), activate(self.branch), self.assertRaisesMessage(RuntimeError, f"'{self.alias}'"):
            with write_scope():
                self.fail("the nested scope opened")
        with activate(self.branch), write_scope(), activate(None), self.assertRaisesMessage(RuntimeError, "'default'"):
            with write_scope():
                self.fail("the nested scope opened")


class LockTimeoutScopeTest(_ScopeCase):
    def test_the_scope_sets_the_timeout_on_both_connections_and_restores_each_value(self):
        with activate(self.branch):
            with write_scope():
                self.assertEqual(self.timeouts(), (LOCK_TIMEOUT, LOCK_TIMEOUT))
            self.assertEqual(self.timeouts(), (DEFAULT_BEFORE, BRANCH_BEFORE))

    def test_a_block_opens_the_scope_and_restores_after_its_commit_callbacks(self):
        seen = []
        with activate(self.branch):
            with atomic_with_events():
                on_commit(lambda: seen.append(self.timeouts()))
            self.assertEqual(self.timeouts(), (DEFAULT_BEFORE, BRANCH_BEFORE))

        self.assertEqual(seen, [(LOCK_TIMEOUT, LOCK_TIMEOUT)])

    def test_a_nested_scope_does_not_restore(self):
        with activate(self.branch), write_scope():
            with self.assertNumQueries(0), self.assertNumQueries(0, using=self.alias), write_scope():
                pass
            self.assertEqual(self.timeouts(), (LOCK_TIMEOUT, LOCK_TIMEOUT))

    def test_on_main_the_scope_changes_nothing(self):
        with write_scope():
            self.assertEqual(lock_timeout("default"), DEFAULT_BEFORE)

    def test_a_restore_that_fails_discards_its_connection_and_the_other_is_still_restored(self):
        default = connections["default"]
        with activate(self.branch), default.execute_wrapper(refuse_the_restore):
            with self.assertRaisesMessage(RuntimeError, "'default'") as raised, write_scope():
                session = default.connection

        self.assertEqual(str(raised.exception.__cause__), "injected restore failure")
        self.assertTrue(session.closed)
        self.assertIsNone(default.connection)
        self.assertEqual(self.timeouts(), (SERVER_DEFAULT, BRANCH_BEFORE))

    def test_inside_a_caller_transaction_the_set_and_the_restore_run_in_it(self):
        for rolls_back in (False, True):
            with self.subTest(rolls_back=rolls_back), activate(self.branch):
                with transaction.atomic(using="default"), transaction.atomic(using=self.alias):
                    with write_scope():
                        self.assertEqual(self.timeouts(), (LOCK_TIMEOUT, LOCK_TIMEOUT))
                    self.assertEqual(self.timeouts(), (DEFAULT_BEFORE, BRANCH_BEFORE))
                    if rolls_back:
                        transaction.set_rollback(True, using="default")
                        transaction.set_rollback(True, using=self.alias)
                self.assertEqual(self.timeouts(), (DEFAULT_BEFORE, BRANCH_BEFORE))

    def test_a_restore_that_fails_inside_a_caller_transaction_discards_its_connection(self):
        branch_connection = connections[self.alias]
        with activate(self.branch), self.assertRaisesMessage(RuntimeError, f"'{self.alias}'"):
            with transaction.atomic(using=self.alias), branch_connection.execute_wrapper(refuse_the_restore):
                with write_scope():
                    session = branch_connection.connection

        self.assertTrue(session.closed)
        self.assertIsNone(branch_connection.connection)
        self.assertEqual(self.timeouts(), (DEFAULT_BEFORE, SERVER_DEFAULT))

    def test_after_an_aborted_caller_transaction_no_session_keeps_the_timeout(self):
        """PostgreSQL refuses the restore in the aborted transaction, so the scope discards that session."""
        with activate(self.branch), self.assertRaisesMessage(RuntimeError, "'default'"):
            with transaction.atomic(using="default"), write_scope():
                abort_the_transaction("default")

        self.assertIsNone(connections["default"].connection)
        self.assertEqual(self.timeouts(), (SERVER_DEFAULT, BRANCH_BEFORE))

    def test_a_setup_that_fails_on_the_branch_restores_default(self):
        with activate(self.branch), transaction.atomic(using=self.alias):
            abort_the_transaction(self.alias)
            with self.assertRaises(InternalError), write_scope():
                self.fail("the scope opened")
            self.assertEqual(lock_timeout("default"), DEFAULT_BEFORE)
            transaction.set_rollback(True, using=self.alias)


class CommitJoinTest(_ScopeCase):
    """``on_commit`` runs its callback once, after the transactions open at the call commit on both connections."""

    NESTINGS = ((), ("default",), ("branch",), ("default", "branch"), ("branch", "default"))

    def _in_transaction(self):
        return connections["default"].in_atomic_block, connections[self.alias].in_atomic_block

    def _join(self, calls):
        on_commit(lambda: calls.append(self._in_transaction()))

    def _register(self, calls):
        with atomic_with_events():
            self._join(calls)

    def test_the_callback_runs_once_after_both_connections_commit_in_every_nesting(self):
        for nesting in self.NESTINGS:
            calls = []
            with self.subTest(nesting=nesting), activate(self.branch):
                with ExitStack() as caller:
                    for name in nesting:
                        caller.enter_context(transaction.atomic(using=self.alias_of(name)))
                    self._register(calls)
                    if nesting:
                        self.assertEqual(calls, [])
                self.assertEqual(calls, [(False, False)])

    def test_a_connection_in_autocommit_acknowledges_at_once_and_the_callback_waits_for_the_other(self):
        for held in ("default", "branch"):
            calls = []
            with self.subTest(held=held), activate(self.branch):
                with transaction.atomic(using=self.alias_of(held)), write_scope():
                    self._join(calls)
                    self.assertEqual(calls, [])
                self.assertEqual(calls, [(False, False)])

    def test_a_rollback_of_either_transaction_drops_the_callback(self):
        for nesting in (("default", "branch"), ("branch", "default")):
            for rolled_back in nesting:
                calls = []
                with self.subTest(nesting=nesting, rolled_back=rolled_back), activate(self.branch):
                    with ExitStack() as caller:
                        for name in nesting:
                            caller.enter_context(transaction.atomic(using=self.alias_of(name)))
                        self._register(calls)
                        transaction.set_rollback(True, using=self.alias_of(rolled_back))
                    self.assertEqual(calls, [])

    def test_a_savepoint_rollback_around_the_call_on_either_connection_drops_the_callback(self):
        for rolled_back in ("default", "branch"):
            calls = []
            with self.subTest(rolled_back=rolled_back), activate(self.branch):
                with transaction.atomic(using="default"), transaction.atomic(using=self.alias):
                    with transaction.atomic(using=self.alias_of(rolled_back)):
                        self._register(calls)
                        transaction.set_rollback(True, using=self.alias_of(rolled_back))
                self.assertEqual(calls, [])


class TwoAliasBlockTest(_ScopeCase):
    """A block opens ``default`` outside the branch, and a nested block opens a savepoint on each."""

    def _site(self, name):
        return Site.objects.create(name=f"TwoAlias {name}", slug=f"twoalias-{name}")

    @staticmethod
    def _webhook(name):
        # netbox-branching exempts webhooks, so they stay on default in a branch.
        return Webhook.objects.create(name=f"TwoAlias {name}", payload_url="http://localhost/")

    def test_the_branch_commits_before_default(self):
        commits = []
        with activate(self.branch), atomic_with_events():
            transaction.on_commit(lambda: commits.append("default"), using="default")
            transaction.on_commit(lambda: commits.append("branch"), using=self.alias)

        self.assertEqual(commits, ["branch", "default"])

    def test_a_block_marked_for_rollback_writes_nothing_on_either_connection(self):
        with activate(self.branch):
            with atomic_with_events() as block:
                self.assertEqual(block.aliases, ("default", self.alias))
                site, webhook = self._site("marked"), self._webhook("marked")
                block.set_rollback()
            self.assertFalse(Site.objects.filter(pk=site.pk).exists())
        self.assertFalse(Webhook.objects.filter(pk=webhook.pk).exists())

    def test_a_nested_block_that_raises_rolls_back_only_its_own_writes_on_both_connections(self):
        with activate(self.branch):
            with atomic_with_events():
                kept = (self._site("kept"), self._webhook("kept"))
                with self.assertRaises(ZeroDivisionError), atomic_with_events():
                    dropped = (self._site("dropped"), self._webhook("dropped"))
                    _ = 1 / 0
            sites = set(Site.objects.filter(pk__in=(kept[0].pk, dropped[0].pk)).values_list("pk", flat=True))
        webhooks = set(Webhook.objects.filter(pk__in=(kept[1].pk, dropped[1].pk)).values_list("pk", flat=True))

        self.assertEqual((sites, webhooks), ({kept[0].pk}, {kept[1].pk}))

    def test_a_default_commit_that_fails_after_the_branch_commit_keeps_the_events(self):
        """The branch rows are committed, so the events of the block stay queued (the failure table)."""
        user = User.objects.create_user(username="twoalias-events")
        with activate(self.branch), request_context(user):
            with self.assertRaises(IntegrityError) as raised, atomic_with_events():
                site = self._site("committed")
                # No user has this ID; PostgreSQL checks the deferred foreign key at the COMMIT of default.
                SavedFilter.objects.create(
                    name="TwoAlias dangling", slug="twoalias-dangling", user_id=2_000_000_000, parameters={}
                )
            queued = dict(events_queue.get())
            self.assertTrue(Site.objects.filter(pk=site.pk).exists())

        self.assertIn("foreign key", str(raised.exception))
        self.assertIn(f"dcim.site:{site.pk}", queued)


class RuleSaveInABranchTest(_ScopeCase):
    """A rule save writes through the alias that the router gives for the rule, not through another alias."""

    def test_a_save_through_default_in_a_branch_is_refused_before_any_query(self):
        module_type = make_module_type(make_manufacturer("BrRuleSave"), "BrRuleSave")
        rule = InterfaceNameRule.objects.create(module_type=module_type, name_template="xe-{bay_position}")
        rule.name_template = "xe-0/{bay_position}"

        with activate(self.branch), self.assertNumQueries(0), self.assertNumQueries(0, using=self.alias):
            with self.assertRaisesMessage(RuntimeError, f"'{self.alias}'"):
                rule.save(using="default", update_fields=["name_template"])

        self.assertEqual(InterfaceNameRule.objects.get(pk=rule.pk).name_template, "xe-{bay_position}")

    def test_a_full_save_through_default_in_a_branch_is_refused_before_any_query(self):
        module_type = make_module_type(make_manufacturer("BrRuleFull"), "BrRuleFull")
        rule = InterfaceNameRule.objects.create(module_type=module_type, name_template="xe-{bay_position}")
        rule.name_template = "xe-0/{bay_position}"

        with activate(self.branch), self.assertNumQueries(0), self.assertNumQueries(0, using=self.alias):
            with self.assertRaisesMessage(RuntimeError, f"'{self.alias}'"):
                rule.save(using="default")

        self.assertEqual(InterfaceNameRule.objects.get(pk=rule.pk).name_template, "xe-{bay_position}")

    def test_a_description_save_through_default_in_a_branch_is_refused_before_any_query(self):
        module_type = make_module_type(make_manufacturer("BrRuleDesc"), "BrRuleDesc")
        rule = InterfaceNameRule.objects.create(module_type=module_type, name_template="xe-{bay_position}")
        rule.description = "after"

        with activate(self.branch), self.assertNumQueries(0), self.assertNumQueries(0, using=self.alias):
            with self.assertRaisesMessage(RuntimeError, f"'{self.alias}'"):
                rule.save(using="default", update_fields=["description"])

        self.assertEqual(InterfaceNameRule.objects.get(pk=rule.pk).description, "")


class ExemptRuleModelInABranchTest(_ScopeCase):
    """An operator can exempt the plugin's models from netbox-branching; a rule then lives on ``default`` alone."""

    def setUp(self):
        module_type = make_module_type(make_manufacturer("BrExempt"), "BrExempt")
        # Made before the branch, so the branch holds a copy that the save must leave alone.
        self.rule = InterfaceNameRule.objects.create(module_type=module_type, name_template="xe-{bay_position}")
        super().setUp()
        branching = {**settings.PLUGINS_CONFIG["netbox_branching"], "exempt_models": ["netbox_interface_name_rules.*"]}
        exempt = override_settings(PLUGINS_CONFIG={**settings.PLUGINS_CONFIG, "netbox_branching": branching})
        exempt.enable()
        self.addCleanup(exempt.disable)

    def test_the_router_gives_default_for_the_rule_and_the_branch_for_an_interface(self):
        with activate(self.branch):
            routed = router.db_for_write(InterfaceNameRule), router.db_for_write(Interface)

        self.assertEqual(routed, ("default", self.alias))

    def test_a_partial_rule_save_in_a_branch_writes_on_default(self):
        self.rule.name_template = "xe-0/{bay_position}"

        with activate(self.branch):
            self.rule.save(update_fields=["name_template"])

        self.assertEqual(
            InterfaceNameRule.objects.using("default").get(pk=self.rule.pk).name_template, "xe-0/{bay_position}"
        )
        self.assertEqual(
            InterfaceNameRule.objects.using(self.alias).get(pk=self.rule.pk).name_template, "xe-{bay_position}"
        )
