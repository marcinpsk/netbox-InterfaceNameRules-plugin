# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Keep the family package below the engine, as specified in ADR 0011."""

import ast
from importlib.util import resolve_name
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

_PLUGIN = "netbox_interface_name_rules"
_FAMILY = Path(__file__).resolve().parents[1] / "family"


def _plugin_dependencies(path):
    dependencies = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), f"{_PLUGIN}.family")
            modules = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        else:
            continue
        for module in modules:
            if not module.startswith(f"{_PLUGIN}."):
                continue
            dependency = module.removeprefix(f"{_PLUGIN}.").split(".")
            if dependency[0] != "family":
                dependencies.add(dependency[0])
            elif dependency[1:2] == ["engine"]:
                dependencies.add("engine")
    return dependencies


class FamilyDependencyDirectionTest(SimpleTestCase):
    def test_family_dependencies_stay_below_the_engine(self):
        dependencies = set()
        modules = sorted(_FAMILY.glob("*.py"))
        self.assertTrue(modules)
        for path in modules:
            with self.subTest(module=path.name):
                module_dependencies = _plugin_dependencies(path)
                self.assertNotIn("engine", module_dependencies)
                dependencies.update(module_dependencies)
        self.assertEqual(dependencies, {"naming", "choices", "rule_selection"})

    def test_detector_reports_every_engine_import_spelling(self):
        spellings = (
            "from ..engine import build_variables",
            "from netbox_interface_name_rules.engine import build_variables",
            "import netbox_interface_name_rules.engine",
            "from . import engine",
            "from .. import engine",
            "from netbox_interface_name_rules import engine",
            "import netbox_interface_name_rules.engine as renamed",
            "def deferred():\n    from .. import engine as renamed",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dependency.py"
            for source in spellings:
                with self.subTest(source=source):
                    path.write_text(source, encoding="utf-8")
                    self.assertEqual(_plugin_dependencies(path), {"engine"})
