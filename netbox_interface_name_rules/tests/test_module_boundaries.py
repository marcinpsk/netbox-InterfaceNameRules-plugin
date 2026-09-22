# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The family package is used through its own public seam.

`family/__init__.py` re-exports what the rest of the plugin may use. A module outside the package
that imports a submodule instead binds itself to an internal layout the package is free to change.
"""

import ast
import pathlib
import tempfile
import tomllib
from importlib.util import resolve_name

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = PACKAGE.name
FAMILY_PACKAGE = "family"

# The family package does not export the template-name helpers the engine needs, so the engine
# reaches past the seam for them. Widening the package API is a change to its public surface and
# belongs in its own commit; until then this is the one import allowed through.
PERMITTED_SUBMODULE_IMPORTS = {("engine.py", "template_names")}
PRIVATE_PLUGIN_IMPORT_PERMITS = frozenset()
PRIVATE_PLUGIN_ATTRIBUTE_PERMITS = frozenset()
EXPRESSION_PARSE_PERMITS = frozenset()
PRODUCTION_AST_IMPORT_PERMITS = frozenset()
SIMPLE_TEST_CASE_REVERSE_PERMITS = frozenset()
LANGUAGE_MODULE = PACKAGE / "name_template.py"
PYPROJECT = PACKAGE.parent / "pyproject.toml"


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
        if imported_module == module_name or not imported_module.startswith(PLUGIN_PACKAGE):
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


def _test_modules() -> list[pathlib.Path]:
    """Return the test modules of this package."""
    return sorted(path for path in (PACKAGE / "tests").glob("*.py") if path.stem != "__init__")


def _is_simple_test_case(node: ast.ClassDef) -> bool:
    """Return whether *node* derives directly from `SimpleTestCase`."""
    return any(
        (isinstance(base, ast.Name) and base.id == "SimpleTestCase")
        or (isinstance(base, ast.Attribute) and base.attr == "SimpleTestCase")
        for base in node.bases
    )


def _overrides_root_urlconf(node: ast.ClassDef) -> bool:
    """Return whether *node* is decorated with an `override_settings` that names `ROOT_URLCONF`."""
    return any(
        isinstance(decorator, ast.Call) and any(keyword.arg == "ROOT_URLCONF" for keyword in decorator.keywords)
        for decorator in node.decorator_list
    )


def _is_reverse_call(node: ast.AST) -> bool:
    """Return whether *node* calls `reverse`, by either spelling."""
    if not isinstance(node, ast.Call):
        return False
    return (isinstance(node.func, ast.Name) and node.func.id == "reverse") or (
        isinstance(node.func, ast.Attribute) and node.func.attr == "reverse"
    )


def _unisolated_reverse_classes(path: pathlib.Path) -> set[str]:
    """Return the `SimpleTestCase` classes in *path* that reverse against the real root URLconf."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_simple_test_case(node):
            continue
        if _overrides_root_urlconf(node):
            continue
        if any(_is_reverse_call(child) for child in ast.walk(node)):
            found.add(node.name)
    return found


def _private_module_names(path: pathlib.Path) -> set[str]:
    """Return the private names *path* binds at module level, by definition or by assignment."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name.startswith("_"):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found.update(t.id for t in node.targets if isinstance(t, ast.Name) and t.id.startswith("_"))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id.startswith("_"):
            found.add(node.target.id)
    return found


def _banned_api_names() -> set[str]:
    """Return the qualified names ruff's banned-api table refuses."""
    with PYPROJECT.open("rb") as handle:
        config = tomllib.load(handle)
    return set(config["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"])


def _module_alias_targets(tree: ast.AST, package: str) -> dict[str, str]:
    """Map each name bound in *tree* to the plugin module it refers to."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            module = resolve_name(base, package) if node.level else (node.module or "")
            if not module.startswith(PLUGIN_PACKAGE):
                continue
            for alias in node.names:
                if not alias.name.startswith("_"):
                    aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith(PLUGIN_PACKAGE):
                    continue
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]
    return aliases


def _dotted_parts(node: ast.AST) -> list[str] | None:
    """Return the dotted name *node* spells, or None when it is not a plain dotted name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    parts.reverse()
    return parts


def _private_plugin_attributes(path: pathlib.Path, module_name: str | None = None) -> set[str]:
    """Return the private names *path* reads from another plugin module by attribute access."""
    module_name = module_name or _module_name(path)
    package = module_name.rpartition(".")[0] or module_name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _module_alias_targets(tree, package)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        if node.attr.startswith("__") and node.attr.endswith("__"):
            continue
        parts = _dotted_parts(node)
        if parts is None or len(parts) < 2:
            continue
        target = aliases.get(parts[0])
        if target is None:
            continue
        owner = ".".join([target, *parts[1:-1]])
        if owner.startswith(PLUGIN_PACKAGE) and owner != module_name:
            found.add(f"{owner}.{node.attr}")
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


class UnisolatedReverseTest(SimpleTestCase):
    """A `SimpleTestCase` must not resolve a URL against NetBox's real root URLconf.

    Loading that URLconf queries the database on some NetBox releases, which `SimpleTestCase`
    forbids. Commit 5a8248f already fixed one CI failure of this shape on 4.3.7 and 4.5.3. A test
    that needs the configured path belongs on `TestCase`; one that does not belongs under an
    `override_settings(ROOT_URLCONF=...)`.
    """

    def test_no_simple_test_case_reverses_against_the_real_root_urlconf(self):
        violations = set()
        for path in _test_modules():
            relative = path.relative_to(PACKAGE)
            for name in _unisolated_reverse_classes(path):
                violation = (str(relative), name)
                if violation not in SIMPLE_TEST_CASE_REVERSE_PERMITS:
                    violations.add(violation)

        self.assertEqual(violations, set())

    def test_the_detector_catches_a_constructed_violation(self):
        source = "class Sample(SimpleTestCase):\n    def test_x(self):\n        reverse('name')\n"
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            self.assertEqual(_unisolated_reverse_classes(path), {"Sample"})

    def test_an_isolated_root_urlconf_is_permitted(self):
        source = (
            "@override_settings(ROOT_URLCONF=_Conf)\n"
            "class Sample(SimpleTestCase):\n"
            "    def test_x(self):\n        reverse('name')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            self.assertEqual(_unisolated_reverse_classes(path), set())


class BannedPrivateLanguageNameTest(SimpleTestCase):
    """Ruff's banned-api table must name every private helper the language module defines.

    The table is hand-written, so a new private helper would otherwise be reachable from any
    adapter by module-qualified access. Deriving the expectation from the module keeps the two
    from drifting.
    """

    def test_every_private_language_name_is_banned(self):
        prefix = f"netbox_interface_name_rules.{LANGUAGE_MODULE.stem}"
        expected = {f"{prefix}.{name}" for name in _private_module_names(LANGUAGE_MODULE)}

        self.assertEqual(expected - _banned_api_names(), set())

    def test_the_language_module_defines_private_names(self):
        self.assertNotEqual(_private_module_names(LANGUAGE_MODULE), set())


class PrivateAttributeBoundaryTest(SimpleTestCase):
    """A production module must not read another plugin module's private name.

    `_private_plugin_imports` sees the import spelling only. A module can also hold a public
    reference to a sibling and reach a private name through it, which no guard saw before.
    """

    def test_production_modules_do_not_read_private_plugin_attributes(self):
        violations = set()
        for path in _production_modules():
            relative = path.relative_to(PACKAGE)
            for name in _private_plugin_attributes(path):
                violation = (str(relative), name)
                if violation not in PRIVATE_PLUGIN_ATTRIBUTE_PERMITS:
                    violations.add(violation)

        self.assertEqual(violations, set())

    def test_the_detector_reports_every_spelling(self):
        spellings = (
            "from netbox_interface_name_rules import naming\nnaming._resolve_slot(1, 2, 3)",
            "from . import naming\nnaming._resolve_slot(1, 2, 3)",
            "from netbox_interface_name_rules import naming as renamed\nrenamed._resolve_slot(1, 2, 3)",
            "import netbox_interface_name_rules.naming as renamed\nrenamed._resolve_slot(1, 2, 3)",
            "import netbox_interface_name_rules\nnetbox_interface_name_rules.naming._resolve_slot(1, 2, 3)",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            for source in spellings:
                with self.subTest(source=source):
                    path.write_text(source, encoding="utf-8")
                    found = _private_plugin_attributes(path, module_name=f"{PLUGIN_PACKAGE}.sample")

                    self.assertEqual(found, {f"{PLUGIN_PACKAGE}.naming._resolve_slot"})

    def test_a_modules_own_private_name_is_not_a_violation(self):
        source = "import netbox_interface_name_rules.naming\nnetbox_interface_name_rules.naming._resolve_slot(1, 2, 3)"
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            found = _private_plugin_attributes(path, module_name=f"{PLUGIN_PACKAGE}.naming")

            self.assertEqual(found, set())

    def test_a_dunder_on_a_sibling_module_is_not_a_violation(self):
        source = "from netbox_interface_name_rules import naming\nprint(naming.__name__)"
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            self.assertEqual(_private_plugin_attributes(path, module_name=f"{PLUGIN_PACKAGE}.sample"), set())
