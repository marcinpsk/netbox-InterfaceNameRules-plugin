# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests against a real netbox-branching branch.

They skip when netbox-branching is not installed. The CI leg that installs it sets
``EXPECT_NETBOX_BRANCHING=1``, and there a missing netbox-branching fails the guard test instead.
"""

import ast
import collections
import hashlib
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


# Each (name, receiver, module, scope) reference to a name that saves a replayed object in 1.2.1: count, what reaches it.
REPLAY_SAVES = {
    ("apply", "change", "merge_strategies/iterative.py", "IterativeMergeStrategy.merge"): (1, "Branch.merge"),
    ("undo", "change", "merge_strategies/iterative.py", "IterativeMergeStrategy.revert"): (1, "Branch.revert"),
    ("apply", "dummy_change", "merge_strategies/squash.py", "SquashMergeStrategy.merge"): (1, "Branch.merge"),
    ("undo", "dummy_change", "merge_strategies/squash.py", "SquashMergeStrategy.revert"): (1, "Branch.revert"),
    ("apply", "change", "models/branches.py", "Branch._apply_sync_update"): (3, "Branch.sync"),
    ("apply", "change", "models/branches.py", "Branch._handle_sync_delete"): (1, "Branch.sync"),
    ("apply", "", "models/changes.py", "ObjectChange"): (1, "apply.alters_data, no call"),
    ("undo", "", "models/changes.py", "ObjectChange"): (1, "undo.alters_data, no call"),
    ("deserialize_object", "from utilities.serialization", "models/changes.py", "<module>"): (1, "the import"),
    ("update_object", "from netbox_branching.utilities", "models/changes.py", "<module>"): (1, "the import"),
    ("deserialize_object", "hasattr(model)", "models/changes.py", "ObjectChange.apply"): (1, "each apply above"),
    ("deserialize_object", "model", "models/changes.py", "ObjectChange.apply"): (1, "each apply above"),
    ("deserialize_object", "", "models/changes.py", "ObjectChange.apply"): (1, "each apply above"),
    ("update_object", "", "models/changes.py", "ObjectChange.apply"): (1, "each apply above"),
    ("deserialize_object", "", "models/changes.py", "ObjectChange.undo"): (1, "each undo above"),
    ("update_object", "", "models/changes.py", "ObjectChange.undo"): (1, "each undo above"),
}
# Each reference to a name that starts a replay: a merge strategy, a sync helper, or a wrapped method.
REPLAY_ENTRIES = {
    ("merge", "strategy_class()", "models/branches.py", "Branch.merge"): (1, "inside the wrapped Branch.merge"),
    ("revert", "strategy_class()", "models/branches.py", "Branch.revert"): (1, "inside the wrapped Branch.revert"),
    ("_apply_sync_update", "self", "models/branches.py", "Branch.sync"): (1, "inside the wrapped Branch.sync"),
    ("_handle_sync_delete", "self", "models/branches.py", "Branch.sync"): (1, "inside the wrapped Branch.sync"),
    ("merge", "branch", "jobs.py", "MergeBranchJob.run"): (1, "the wrapped Branch.merge"),
    ("revert", "branch", "jobs.py", "RevertBranchJob.run"): (1, "the wrapped Branch.revert"),
    ("merge", "", "models/branches.py", "Branch"): (1, "merge.alters_data, no call"),
    ("revert", "", "models/branches.py", "Branch"): (1, "revert.alters_data, no call"),
    ("revert", "", "models/changes.py", "ObjectChange.migrate"): (1, "its revert flag, not a method"),
}
# The calls that name an attribute by a string.
NAMING_CALLS = frozenset({"getattr", "setattr", "hasattr"})
# The nodes that open a scope.
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
# The fingerprint of each scope that the allow-lists name, in netbox-branching 1.2.1; see fingerprint.
REVIEWED_FINGERPRINTS = {
    ("jobs.py", "MergeBranchJob.run"): "d039282f2832f890",
    ("jobs.py", "RevertBranchJob.run"): "e0be2037fbdbaca7",
    ("merge_strategies/iterative.py", "IterativeMergeStrategy.merge"): "42aa32ddbda92a55",
    ("merge_strategies/iterative.py", "IterativeMergeStrategy.revert"): "882b00bb19bab80d",
    ("merge_strategies/squash.py", "SquashMergeStrategy.merge"): "c57d4abe51355d00",
    ("merge_strategies/squash.py", "SquashMergeStrategy.revert"): "257f571c7d9201bb",
    ("models/branches.py", "Branch"): "5c1032613ea2f2a4",
    ("models/branches.py", "Branch._apply_sync_update"): "7e235e9385c13ac4",
    ("models/branches.py", "Branch._handle_sync_delete"): "ff0d26c6de4ae7df",
    ("models/branches.py", "Branch.merge"): "3e0ad757b725963d",
    ("models/branches.py", "Branch.revert"): "c736016e1ddc2011",
    ("models/branches.py", "Branch.sync"): "395aa970f1eeb7fd",
    ("models/changes.py", "<module>"): "6ed9b80466510e19",
    ("models/changes.py", "ObjectChange"): "b449c0c1b1db6de6",
    ("models/changes.py", "ObjectChange.apply"): "6b9f955fbd2370cf",
    ("models/changes.py", "ObjectChange.migrate"): "c1927873bbc41a6d",
    ("models/changes.py", "ObjectChange.undo"): "24e38af98d10d67c",
}
# The netbox-branching release whose replay paths the allow-lists and the fingerprints record.
REVIEWED_NETBOX_BRANCHING = "1.2.1"
RE_REVIEW_RELEASE = (
    "The installed netbox-branching is not the release whose replay paths were reviewed. Re-review its replay paths: "
    "run the scan, read every new reference, every changed fingerprint and any dynamic dispatch, such as getattr with "
    "a variable name. Then update REVIEWED_NETBOX_BRANCHING, the fingerprints and the allow-lists together."
)
# The message of a changed reviewed scope.
RE_REVIEW = (
    "A reviewed scope of netbox-branching changed. A change in it can defer a replay past the wrapper without a new "
    "reference. Read the scope again, then update its fingerprint and the allow-lists together."
)


def _references(node, names):
    """Yield ``(name, receiver)`` for each reference that *node* itself makes to one of *names*."""
    if isinstance(node, ast.Attribute) and node.attr in names:
        yield node.attr, ast.unparse(node.value)
    elif isinstance(node, ast.Name) and node.id in names:
        yield node.id, ""
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        source = f"from {'.' * node.level}{node.module or ''}" if isinstance(node, ast.ImportFrom) else "import"
        for alias in node.names:
            if (name := alias.name.rsplit(".", 1)[-1]) in names:
                yield name, source
    elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in NAMING_CALLS and len(node.args) > 1:
        attribute = node.args[1]
        if isinstance(attribute, ast.Constant) and attribute.value in names:
            yield attribute.value, f"{node.func.id}({ast.unparse(node.args[0])})"


def _scoped_nodes(node, scope=()):
    """Yield ``(scope, node)`` for each node under *node*; a function, a class and a lambda open a scope."""
    for child in ast.iter_child_nodes(node):
        inner = (*scope, getattr(child, "name", "<lambda>")) if isinstance(child, SCOPES) else scope
        yield inner, child
        yield from _scoped_nodes(child, inner)


def _modules(package):
    """Yield ``(module, tree)`` for each module of *package*, its tests excluded."""
    for path in sorted(package.rglob("*.py")):
        module = path.relative_to(package).as_posix()
        if not module.startswith("tests/"):
            yield module, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def replay_references(package, names):
    """Count each reference to one of *names* in *package*, its tests excluded, by ``(name, receiver, module, scope)``.

    A reference is an attribute, a name, an imported name, or the string of a getattr, setattr or hasattr call. The
    receiver is the object of the attribute or the call, the source of the import, or empty. A function, a class and a
    lambda each open a scope, so a deferred call is a site of its own.
    """
    sites = collections.Counter()
    for module, tree in _modules(package):
        for scope, node in _scoped_nodes(tree):
            for name, receiver in _references(node, names):
                sites[(name, receiver, module, ".".join(scope) or "<module>")] += 1
    return sites


def fingerprint(node):
    """Hash the source of a function, or of the statements of a class or module outside its nested scopes."""
    own = (
        [node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        else [s for s in node.body if not isinstance(s, SCOPES)]
    )
    return hashlib.sha256(ast.unparse(ast.Module(own, [])).encode()).hexdigest()[:16]


def scope_fingerprints(package, scopes):
    """Return the fingerprint of each ``(module, scope)`` of *scopes* that *package* has, scopes named as above."""
    found = {}
    for module, tree in _modules(package):
        scope_nodes = [(("<module>",), tree)] + [(s, n) for s, n in _scoped_nodes(tree) if isinstance(n, SCOPES)]
        for scope, node in scope_nodes:
            if (key := (module, ".".join(scope))) in scopes:
                found[key] = fingerprint(node)
    return found


def reviewed_scopes():
    """Return each ``(module, scope)`` that the allow-lists name."""
    return {(module, scope) for _, _, module, scope in (*REPLAY_SAVES, *REPLAY_ENTRIES)}


def reviewed(allowed):
    """Return the reviewed count of each site of *allowed*."""
    return collections.Counter({site: count for site, (count, _) in allowed.items()})


@skipUnless(BRANCHING_INSTALLED, BRANCHING_SKIP_REASON)
class ReplayCallSiteContractTest(SimpleTestCase):
    """The installed netbox-branching is the reviewed release; the scan and the fingerprints help review the next one."""

    maxDiff = None

    def test_the_installed_netbox_branching_is_the_reviewed_release(self):
        self.assertEqual(branching.installed_version(), REVIEWED_NETBOX_BRANCHING, RE_REVIEW_RELEASE)

    def assert_reviewed(self, allowed):
        """Assert that the installed netbox-branching makes the references of *allowed*, each as often, and no other."""
        import netbox_branching

        found = replay_references(Path(netbox_branching.__file__).parent, {name for name, *_ in allowed})
        self.assertDictEqual(dict(found), dict(reviewed(allowed)))

    def test_each_reference_that_saves_a_replayed_object_is_reviewed(self):
        self.assert_reviewed(REPLAY_SAVES)

    def test_each_replay_starts_in_a_wrapped_method(self):
        self.assert_reviewed(REPLAY_ENTRIES)

    def test_each_reviewed_scope_is_unchanged(self):
        import netbox_branching

        found = scope_fingerprints(Path(netbox_branching.__file__).parent, reviewed_scopes())

        self.assertEqual(set(REVIEWED_FINGERPRINTS), reviewed_scopes())
        self.assertDictEqual(found, REVIEWED_FINGERPRINTS, RE_REVIEW)


class ReplayCallSiteScanTest(SimpleTestCase):
    """The scan finds a replay reference wherever a release adds one."""

    def scan(self, source, names):
        """Return the sites that the scan finds in a package whose module ``added.py`` holds *source*."""
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "added.py").write_text(source, encoding="utf-8")
            (package / "tests").mkdir()
            (package / "tests" / "test_added.py").write_text("change.apply(None)\n", encoding="utf-8")
            return replay_references(package, names)

    def test_the_scan_reports_each_call_with_its_receiver_and_scope(self):
        source = (
            "class Strategy:\n    def merge(self, change):\n        change.apply(self)\n"
            "def helper(instance, data):\n    def nested():\n        update_object(instance, data, using=None)\n"
            "change.undo(None)\n"
        )

        self.assertEqual(
            self.scan(source, {"apply", "undo", "update_object"}),
            {
                ("apply", "change", "added.py", "Strategy.merge"): 1,
                ("update_object", "", "added.py", "helper.nested"): 1,
                ("undo", "change", "added.py", "<module>"): 1,
            },
        )

    def test_an_indirect_reference_is_reported(self):
        source = (
            "def indirect(change, branch):\n    replay = change.apply\n    replay(branch)\n"
            "def by_name(change, branch):\n    getattr(change, 'apply')(branch)\n"
            "from .utilities import update_object as u\n"
        )

        self.assertEqual(
            self.scan(source, {"apply", "update_object"}),
            {
                ("apply", "change", "added.py", "indirect"): 1,
                ("apply", "getattr(change)", "added.py", "by_name"): 1,
                ("update_object", "from .utilities", "added.py", "<module>"): 1,
            },
        )

    def test_a_second_receiver_and_a_second_call_in_one_scope_are_reported(self):
        source = (
            "class Job:\n    def run(self, branch, strategy):\n"
            "        branch.merge(None)\n        strategy.merge(None)\n        strategy.merge(None)\n"
        )

        self.assertEqual(
            self.scan(source, {"merge"}),
            {("merge", "branch", "added.py", "Job.run"): 1, ("merge", "strategy", "added.py", "Job.run"): 2},
        )

    def reviewed_state(self, source):
        """Return the references and the fingerprint of ``Strategy.merge`` in a package whose ``added.py`` is *source*."""
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "added.py").write_text(source, encoding="utf-8")
            return replay_references(package, {"apply"}), scope_fingerprints(package, {("added.py", "Strategy.merge")})

    def test_a_deferral_inside_a_reviewed_function_changes_its_fingerprint(self):
        reviewed_source = (
            "class Strategy:\n    def merge(self, branch, changes):\n"
            "        for change in changes:\n            change.apply(branch)\n"
        )
        deferrals = {
            "a stored reference called later": (
                "class Strategy:\n    def merge(self, branch, changes):\n        for change in changes:\n"
                "            replay = change.apply\n            on_commit(lambda: replay(branch))\n"
            ),
            "a generator": (
                "class Strategy:\n    def merge(self, branch, changes):\n"
                "        return (change.apply(branch) for change in changes)\n"
            ),
        }
        references, fingerprints = self.reviewed_state(reviewed_source)
        for deferral, source in deferrals.items():
            with self.subTest(deferral=deferral):
                deferred_references, deferred_fingerprints = self.reviewed_state(source)

                self.assertEqual(deferred_references, references)
                self.assertNotEqual(deferred_fingerprints, fingerprints)

    def test_a_lambda_is_a_scope_of_its_own(self):
        source = "class Branch:\n    def merge(self, strategy):\n        on_commit(lambda: strategy.merge(self))\n"

        self.assertEqual(self.scan(source, {"merge"}), {("merge", "strategy", "added.py", "Branch.merge.<lambda>"): 1})


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
