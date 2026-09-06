# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The migration graph resolves against the migrations NetBox actually ships."""

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

APP_LABEL = "netbox_interface_name_rules"


class MigrationGraphTest(SimpleTestCase):
    """A dependency on a migration a NetBox squash removed must not reach the graph."""

    def test_the_graph_builds_with_replacements_disabled(self):
        """`migrate` remaps a replaced node through the squash that lists it in `replaces`, so it
        hides this. `sqlmigrate` disables replacements and fails outright.
        """
        loader = MigrationLoader(None, replace_migrations=False)

        self.assertIn((APP_LABEL, "0001_initial"), loader.graph.nodes)

    def test_every_cross_app_dependency_exists_on_disk(self):
        """Report every offending dependency, not just the one the graph happens to reach first.

        Building the graph raises on the first dangling node, so a tree with two of these shows the
        second only after the first is fixed. `load=False` reads the migrations off disk without it.
        """
        loader = MigrationLoader(None, load=False)
        loader.load_disk()
        available = set(loader.disk_migrations)

        missing = [
            f"{name} depends on {dependency}"
            for (app, name), migration in loader.disk_migrations.items()
            if app == APP_LABEL
            for dependency in migration.dependencies
            if dependency[0] != APP_LABEL and not dependency[1].startswith("__") and dependency not in available
        ]

        self.assertEqual(missing, [], "NetBox squashes remove migrations; depend on one it still ships.")
