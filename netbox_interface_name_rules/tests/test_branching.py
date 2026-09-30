# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests against a real netbox-branching branch.

They skip when netbox-branching is not installed. The CI leg that installs it sets
``EXPECT_NETBOX_BRANCHING=1``, and there a missing netbox-branching fails the guard test instead.
"""

import os
import subprocess
import sys
from unittest import skipUnless

from dcim.models import Interface
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, connections, router
from django.test import SimpleTestCase, TransactionTestCase

from netbox_interface_name_rules.branching import check_version
from netbox_interface_name_rules.tests.helpers import make_device, make_device_type, make_manufacturer

BRANCHING_INSTALLED = apps.is_installed("netbox_branching")
BRANCHING_SKIP_REASON = "netbox-branching is not installed"


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


def remove_branch(branch):
    """Close the connection of *branch*, then drop its schema."""
    connections[branch.connection_name].close()
    branch.deprovision()


@skipUnless(BRANCHING_INSTALLED, BRANCHING_SKIP_REASON)
class BranchTestCase(TransactionTestCase):
    """Provision real branches. Each branch is removed when its test ends, because ``--reuse-db`` keeps schemas."""

    def provision_branch(self, name, user):
        """Return a new branch named *name*, provisioned by *user* as netbox-branching's own tests do."""
        from netbox_branching.models import Branch

        branch = Branch(name=name)
        branch.save(provision=False)
        self.addCleanup(remove_branch, branch)
        branch.provision(user=user)
        # provision() writes the status with a queryset update, which the instance does not see.
        branch.refresh_from_db()
        return branch


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
        from netbox_branching.utilities import activate_branch

        branch = self.provision_branch("Write", self.user)

        with activate_branch(branch):
            self.assertEqual(router.db_for_write(Interface), branch.connection_name)
            Interface.objects.create(device=self.device, name="branch-only", type="1000base-t")
            self.assertTrue(Interface.objects.filter(device=self.device, name="branch-only").exists())

        self.assertEqual(router.db_for_write(Interface), "default")
        self.assertFalse(Interface.objects.filter(device=self.device, name="branch-only").exists())

    def test_the_teardown_drops_the_schema_and_closes_the_connection(self):
        from netbox_branching.utilities import activate_branch

        branch = self.provision_branch("Teardown", self.user)
        with activate_branch(branch):
            self.assertFalse(Interface.objects.filter(device=self.device).exists())
        branch_connection = connections[branch.connection_name]
        self.assertIsNotNone(branch_connection.connection)

        self.doCleanups()

        self.assertIsNone(branch_connection.connection)
        self.assertFalse(schema_exists(branch.schema_name))
