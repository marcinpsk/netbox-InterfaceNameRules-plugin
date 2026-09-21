# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The family package is used through its own public seam.

`family/__init__.py` re-exports what the rest of the plugin may use. A module outside the package
that imports a submodule instead binds itself to an internal layout the package is free to change.
"""

import ast
import pathlib
import tempfile
from importlib.util import resolve_name

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
FAMILY_PACKAGE = "family"

# The family package does not export the template-name helpers the engine needs, so the engine
# reaches past the seam for them. Widening the package API is a change to its public surface and
# belongs in its own commit; until then this is the one import allowed through.
PERMITTED_SUBMODULE_IMPORTS = {("engine.py", "template_names")}
PRIVATE_PLUGIN_IMPORT_PERMITS = frozenset()
EXPRESSION_PARSE_PERMITS = frozenset()
PRODUCTION_AST_IMPORT_PERMITS = frozenset()
LANGUAGE_MODULE = PACKAGE / "name_template.py"


def _family_submodules() -> set[str]:
    """Return the module names the family package is made of."""
    package = PACKAGE / FAMILY_PACKAGE
    return {path.stem for path in package.glob("*.py") if path.stem != "__init__"}


def _family_submodule_imports(path: pathlib.Path) -> set[str]:
    """Return the family submodules *path* imports directly, by either spelling."""
    submodules = _family_submodules()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # `import netbox_interface_name_rules.family.batch` binds the same internal layout.
            for alias in node.names:
                imported = alias.name.removeprefix("netbox_interface_name_rules.")
                if imported.startswith(f"{FAMILY_PACKAGE}."):
                    found.add(imported.split(".", 1)[1])
            continue
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        module = node.module.removeprefix("netbox_interface_name_rules.")
        if module == FAMILY_PACKAGE:
            # A submodule imported as a name reaches past the seam just as a dotted path does.
            found.update(alias.name for alias in node.names if alias.name in submodules)
        elif module.startswith(f"{FAMILY_PACKAGE}."):
            found.add(module.split(".", 1)[1])
    return found


def _module_name(path: pathlib.Path) -> str:
    """Return the dotted plugin module name for *path*."""
    relative = path.relative_to(PACKAGE.parent).with_suffix("")
    return ".".join(relative.parts)


def _production_modules():
    """Yield production Python modules, including migrations but not tests."""
    for path in sorted(PACKAGE.rglob("*.py")):
        if "tests" not in path.relative_to(PACKAGE).parts:
            yield path


def _private_plugin_imports(path: pathlib.Path, module_name: str | None = None) -> set[str]:
    """Return private names imported from another plugin module."""
    module_name = module_name or _module_name(path)
    package = module_name.rpartition(".")[0] or module_name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            imported_module = resolve_name("." * node.level + (node.module or ""), package)
        else:
            imported_module = node.module or ""
        if imported_module == module_name or not imported_module.startswith("netbox_interface_name_rules"):
            continue
        for alias in node.names:
            if alias.name.startswith("_"):
                found.add(f"{imported_module}.{alias.name}")
    return found


def _expression_parse_calls(path: pathlib.Path) -> list[int]:
    """Return lines that parse an abstract syntax tree in expression mode."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    ast_aliases = {"ast"}
    parse_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            ast_aliases.update(alias.asname for alias in node.names if alias.name == "ast" and alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "ast":
            parse_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "parse")
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct = isinstance(node.func, ast.Name) and node.func.id in parse_aliases
        attribute = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "parse"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ast_aliases
        )
        if not (direct or attribute):
            continue
        mode = next((keyword.value for keyword in node.keywords if keyword.arg == "mode"), None)
        if mode is None and len(node.args) >= 3:
            mode = node.args[2]
        if isinstance(mode, ast.Constant) and mode.value == "eval":
            found.append(node.lineno)
    return found


def _ast_imports(path: pathlib.Path) -> list[int]:
    """Return lines that import the abstract syntax tree module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Import) and any(alias.name == "ast" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "ast")
    ]


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

    def test_a_direct_import_statement_reaches_past_the_seam_too(self):
        """`import netbox_interface_name_rules.family.batch` binds the layout as a `from` import does."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(
                "import netbox_interface_name_rules.family.batch\n"
                "import netbox_interface_name_rules.family.conversion as conversion\n",
                encoding="utf-8",
            )

            self.assertEqual(_family_submodule_imports(path), {"batch", "conversion"})

    def test_every_permitted_import_still_exists(self):
        """A permit that nothing uses any more must be removed, not left to grant something later."""
        for name, submodule in PERMITTED_SUBMODULE_IMPORTS:
            self.assertIn(
                submodule,
                _family_submodule_imports(PACKAGE / name),
                f"{name} no longer imports family.{submodule}: drop it from the permit list.",
            )


class PluginModuleBoundaryTest(SimpleTestCase):
    """Keep name-template parsing and private helpers behind their owning modules."""

    def test_production_modules_do_not_import_private_plugin_names(self):
        violations = set()
        for path in _production_modules():
            relative = path.relative_to(PACKAGE)
            for imported in _private_plugin_imports(path):
                violation = (str(relative), imported)
                if violation not in PRIVATE_PLUGIN_IMPORT_PERMITS:
                    violations.add(violation)

        self.assertEqual(violations, set())

    def test_private_import_detector_catches_a_constructed_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text("from netbox_interface_name_rules.models import _private\n", encoding="utf-8")

            self.assertEqual(
                _private_plugin_imports(path, "netbox_interface_name_rules.sample"),
                {"netbox_interface_name_rules.models._private"},
            )

    def test_expression_mode_parsing_exists_only_in_the_language_module(self):
        violations = set()
        for path in sorted(PACKAGE.rglob("*.py")):
            if path == LANGUAGE_MODULE:
                continue
            relative = path.relative_to(PACKAGE)
            for line in _expression_parse_calls(path):
                violation = (str(relative), line)
                if violation not in EXPRESSION_PARSE_PERMITS:
                    violations.add(violation)

        self.assertEqual(violations, set())

    def test_expression_parse_detector_catches_a_constructed_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text("from ast import parse as read\nread('1', mode='eval')\n", encoding="utf-8")

            self.assertEqual(_expression_parse_calls(path), [2])

    def test_the_language_module_has_one_expression_parser(self):
        self.assertEqual(len(_expression_parse_calls(LANGUAGE_MODULE)), 1)

    def test_production_ast_import_exists_only_in_the_language_module(self):
        violations = set()
        for path in _production_modules():
            if path == LANGUAGE_MODULE:
                continue
            relative = path.relative_to(PACKAGE)
            for line in _ast_imports(path):
                violation = (str(relative), line)
                if violation not in PRODUCTION_AST_IMPORT_PERMITS:
                    violations.add(violation)

        self.assertEqual(violations, set())

    def test_ast_import_detector_catches_a_constructed_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text("import ast as syntax_tree\n", encoding="utf-8")

            self.assertEqual(_ast_imports(path), [1])
