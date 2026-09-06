# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The family package is used through its own public seam.

`family/__init__.py` re-exports what the rest of the plugin may use. A module outside the package
that imports a submodule instead binds itself to an internal layout the package is free to change.
"""

import ast
import pathlib

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
FAMILY_PACKAGE = "family"

# The family package does not export the template-name helpers the engine needs, so the engine
# reaches past the seam for them. Widening the package API is a change to its public surface and
# belongs in its own commit; until then this is the one import allowed through.
PERMITTED_SUBMODULE_IMPORTS = {("engine.py", "template_names")}


def _family_submodules() -> set[str]:
    """Return the module names the family package is made of."""
    package = PACKAGE / FAMILY_PACKAGE
    return {path.stem for path in package.glob("*.py") if path.stem != "__init__"}


def _family_submodule_imports(path: pathlib.Path) -> set[str]:
    """Return the family submodules *path* imports directly, by either spelling."""
    submodules = _family_submodules()
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        module = node.module.removeprefix("netbox_interface_name_rules.")
        if module == FAMILY_PACKAGE:
            # A submodule imported as a name reaches past the seam just as a dotted path does.
            found.update(alias.name for alias in node.names if alias.name in submodules)
        elif module.startswith(f"{FAMILY_PACKAGE}."):
            found.add(module.split(".", 1)[1])
    return found


class FamilySeamTest(SimpleTestCase):
    """Modules outside the family package import the package, not its parts."""

    def test_no_module_reaches_past_the_family_seam(self):
        violations = set()
        for path in sorted(PACKAGE.rglob("*.py")):
            relative = path.relative_to(PACKAGE)
            if relative.parts[0] in {FAMILY_PACKAGE, "tests", "migrations"}:
                continue
            for submodule in _family_submodule_imports(path):
                if (str(relative), submodule) not in PERMITTED_SUBMODULE_IMPORTS:
                    violations.add(f"{relative} imports family.{submodule}")

        self.assertEqual(
            violations,
            set(),
            "Import these through `netbox_interface_name_rules.family`, or export them from it.",
        )

    def test_every_permitted_import_still_exists(self):
        """A permit that nothing uses any more must be removed, not left to grant something later."""
        for name, submodule in PERMITTED_SUBMODULE_IMPORTS:
            self.assertIn(
                submodule,
                _family_submodule_imports(PACKAGE / name),
                f"{name} no longer imports family.{submodule}: drop it from the permit list.",
            )
