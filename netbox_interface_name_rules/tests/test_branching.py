# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests against a real netbox-branching branch.

They skip when netbox-branching is not installed. The CI leg that installs it sets
``EXPECT_NETBOX_BRANCHING=1``, and there a missing netbox-branching fails the guard test instead.
"""

import ast
import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import skipUnless

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, connections, router
from django.test import SimpleTestCase

from netbox_interface_name_rules import branching
from netbox_interface_name_rules.branching import REPLAY_MARK, REPLAYING_METHODS, check_version
from netbox_interface_name_rules.tests.branch_cases import BRANCHING_INSTALLED, BRANCHING_SKIP_REASON, BranchTestCase
from netbox_interface_name_rules.tests.helpers import activate, make_device, make_device_type, make_manufacturer


def schema_exists(schema_name):
    """Return whether the database holds the schema *schema_name*."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [schema_name])
        return cursor.fetchone() is not None


class BranchingDetectionTest(SimpleTestCase):
    """The branch leg cannot skip the branch tests."""

    @skipUnless(os.environ.get("EXPECT_NETBOX_BRANCHING") == "1", "EXPECT_NETBOX_BRANCHING is not set")
    def test_branching_leg_has_netbox_branching(self):
        """CI guard: on the branch leg a missing netbox-branching would silently skip every branch test."""
        self.assertTrue(BRANCHING_INSTALLED)


class VersionGateTest(SimpleTestCase):
    """The plugin supports netbox-branching 1.2.x only."""

    def test_a_1_2_release_is_accepted(self):
        for version in ("1.2.0", "1.2.1", "1.2.10", "1.2.0rc1", "1.2.1.dev3"):
            with self.subTest(version=version):
                check_version(version)

    def test_any_other_release_is_refused(self):
        for version in ("1.1.3", "1.3.0", "1.3.0rc1", "1.20.0", "2.2.0"):
            with self.subTest(version=version), self.assertRaisesMessage(ImproperlyConfigured, f"is {version}."):
                check_version(version)


@skipUnless(BRANCHING_INSTALLED, BRANCHING_SKIP_REASON)
class VersionGateStartupTest(SimpleTestCase):
    """The plugin checks the installed netbox-branching when NetBox starts."""

    def test_an_unsupported_version_stops_startup(self):
        probe = "import django, netbox_branching\nnetbox_branching.AppConfig.version = '1.3.0'\ndjango.setup()\n"
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry)}

        completed = subprocess.run(  # noqa: S603 - Run a fixed probe in a child interpreter.
            [sys.executable, "-c", probe], env=environment, capture_output=True, text=True, check=False
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("ImproperlyConfigured: ", completed.stderr)
        self.assertIn("is 1.3.0.", completed.stderr)


@skipUnless(BRANCHING_INSTALLED, BRANCHING_SKIP_REASON)
class ReplayWrapperContractTest(SimpleTestCase):
    """The plugin wraps each method of netbox-branching's Branch that replays changes, once, as it was reviewed."""

    def replaying_methods(self):
        from netbox_branching.models import Branch

        return {name: getattr(Branch, name) for name in REPLAYING_METHODS}

    def test_each_replaying_method_is_wrapped_once_and_keeps_its_signature_and_attributes(self):
        for name, method in self.replaying_methods().items():
            with self.subTest(method=name):
                original = method.__wrapped__

                self.assertFalse(getattr(original, REPLAY_MARK, False))
                self.assertEqual(str(inspect.signature(original)), "(self, user, commit=True)")
                self.assertEqual(inspect.signature(method), inspect.signature(original))
                self.assertEqual((method.__name__, method.alters_data), (name, True))

    def test_a_second_start_wraps_nothing_again(self):
        wrapped = self.replaying_methods()

        branching.ready()

        self.assertEqual(self.replaying_methods(), wrapped)


# The calls that save a replayed object, each with the wrapped Branch method that reaches it (netbox-branching 1.2.1).
REPLAY_SAVES = {
    ("apply", "merge_strategies/iterative.py", "IterativeMergeStrategy.merge"): "Branch.merge",
    ("undo", "merge_strategies/iterative.py", "IterativeMergeStrategy.revert"): "Branch.revert",
    ("apply", "merge_strategies/squash.py", "SquashMergeStrategy.merge"): "Branch.merge",
    ("undo", "merge_strategies/squash.py", "SquashMergeStrategy.revert"): "Branch.revert",
    ("apply", "models/branches.py", "Branch._apply_sync_update"): "Branch.sync",
    ("apply", "models/branches.py", "Branch._handle_sync_delete"): "Branch.sync",
    ("deserialize_object", "models/changes.py", "ObjectChange.apply"): "each apply above",
    ("update_object", "models/changes.py", "ObjectChange.apply"): "each apply above",
    ("deserialize_object", "models/changes.py", "ObjectChange.undo"): "each undo above",
    ("update_object", "models/changes.py", "ObjectChange.undo"): "each undo above",
}
# The calls that start a replay: a merge strategy or a sync helper, or a job that calls a wrapped method.
REPLAY_ENTRIES = {
    ("merge", "models/branches.py", "Branch.merge"): "the strategy, inside the wrapped Branch.merge",
    ("revert", "models/branches.py", "Branch.revert"): "the strategy, inside the wrapped Branch.revert",
    ("_apply_sync_update", "models/branches.py", "Branch.sync"): "inside the wrapped Branch.sync",
    ("_handle_sync_delete", "models/branches.py", "Branch.sync"): "inside the wrapped Branch.sync",
    ("merge", "jobs.py", "MergeBranchJob.run"): "the wrapped Branch.merge",
    ("revert", "jobs.py", "RevertBranchJob.run"): "the wrapped Branch.revert",
}


def replay_call_sites(package, names):
    """Return ``(callee, module, enclosing qualname)`` of each call of one of *names* in *package*, its tests excluded."""
    sites = set()

    def visit(node, scope, module):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, (*scope, child.name), module)
                continue
            if isinstance(child, ast.Call):
                callee = getattr(child.func, "attr", getattr(child.func, "id", None))
                if callee in names:
                    sites.add((callee, module, ".".join(scope) or "<module>"))
            visit(child, scope, module)

    for path in sorted(package.rglob("*.py")):
        module = path.relative_to(package).as_posix()
        if not module.startswith("tests/"):
            visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), (), module)
    return sites


def replay_names(sites):
    """Return the callee of each of the ``(callee, module, qualname)`` *sites*."""
    return {callee for callee, _, _ in sites}


@skipUnless(BRANCHING_INSTALLED, BRANCHING_SKIP_REASON)
class ReplayCallSiteContractTest(SimpleTestCase):
    """Each replay of the installed netbox-branching runs inside a wrapped method, as in the reviewed release."""

    def installed_sites(self, allowed):
        import netbox_branching

        return replay_call_sites(Path(netbox_branching.__file__).parent, replay_names(allowed))

    def test_each_save_of_a_replayed_object_is_a_reviewed_call_site(self):
        self.assertEqual(self.installed_sites(REPLAY_SAVES), set(REPLAY_SAVES))

    def test_each_replay_starts_in_a_wrapped_method(self):
        self.assertEqual(self.installed_sites(REPLAY_ENTRIES), set(REPLAY_ENTRIES))


class ReplayCallSiteScanTest(SimpleTestCase):
    """The scan finds a replay call wherever a release adds one."""

    def test_the_scan_reports_each_call_with_its_enclosing_function(self):
        source = (
            "class Strategy:\n    def merge(self, change):\n        change.apply(self)\n"
            "def helper(instance, data):\n    def nested():\n        update_object(instance, data, using=None)\n"
            "change.undo(None)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "added.py").write_text(source, encoding="utf-8")
            (package / "tests").mkdir()
            (package / "tests" / "test_added.py").write_text("change.apply(None)\n", encoding="utf-8")

            sites = replay_call_sites(package, replay_names(REPLAY_SAVES))

        self.assertEqual(
            sites,
            {
                ("apply", "added.py", "Strategy.merge"),
                ("update_object", "added.py", "helper.nested"),
                ("undo", "added.py", "<module>"),
            },
        )


class BranchProvisioningTest(BranchTestCase):
    """A branch provisions, takes the writes made while it is active, and is removed at teardown."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="branch-provisioning-operator")
        self.device = make_device(
            "BranchProvisioning", make_device_type(make_manufacturer("BranchProvisioning"), "BranchProvisioning")
        )

    def test_a_branch_provisions_ready(self):
        from netbox_branching.choices import BranchStatusChoices

        branch = self.provision_branch("Provisioning", self.user)

        self.assertEqual(branch.status, BranchStatusChoices.READY)
        self.assertTrue(schema_exists(branch.schema_name))

    def test_a_write_in_the_active_branch_lands_in_the_branch_schema_only(self):
        branch = self.provision_branch("Write", self.user)

        with activate(branch):
            self.assertEqual(router.db_for_write(Interface), branch.connection_name)
            Interface.objects.create(device=self.device, name="branch-only", type="1000base-t")
            self.assertTrue(Interface.objects.filter(device=self.device, name="branch-only").exists())

        self.assertEqual(router.db_for_write(Interface), "default")
        self.assertFalse(Interface.objects.filter(device=self.device, name="branch-only").exists())

    def test_the_teardown_drops_the_schema_and_closes_the_connection(self):
        branch = self.provision_branch("Teardown", self.user)
        with activate(branch):
            self.assertFalse(Interface.objects.filter(device=self.device).exists())
        branch_connection = connections[branch.connection_name]
        self.assertIsNotNone(branch_connection.connection)

        self.doCleanups()

        self.assertIsNone(branch_connection.connection)
        self.assertFalse(schema_exists(branch.schema_name))
