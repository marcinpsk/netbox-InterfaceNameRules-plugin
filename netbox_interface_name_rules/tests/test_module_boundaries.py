# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The family package is used through its own public seam.

`family/__init__.py` re-exports what the rest of the plugin may use. A module outside the package
that imports a submodule instead binds itself to an internal layout the package is free to change.
"""

import ast
import functools
import pathlib
import tempfile
import tomllib
from importlib.util import resolve_name
from unittest.mock import patch

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


def _reverse_class_bindings(class_index):
    """Map each scanned module's class names and imports to their bindings."""
    modules = {}
    for path in class_index:
        modules[path.stem] = path
        if path.is_relative_to(PACKAGE.parent):
            modules[_module_name(path)] = path
    classes = {}
    imports = {}
    module_imports = {}
    for path, nodes in class_index.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top_level = {node.lineno for node in tree.body if isinstance(node, ast.ClassDef)}
        classes[path] = {node.name: node for node in nodes if node.lineno in top_level}
        imports[path] = {}
        module_imports[path] = {}
        package = _module_name(path).rpartition(".")[0] if path.is_relative_to(PACKAGE.parent) else path.parent.name
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module = resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module
                for alias in node.names:
                    target = modules.get(module)
                    if target is None and node.level:
                        target = modules.get((module or "").rpartition(".")[2])
                    if target is not None:
                        imports[path][alias.asname or alias.name] = (target, alias.name)
                    elif node.level and node.module is None:
                        target = modules.get(alias.name)
                        if target is not None:
                            module_imports[path][alias.asname or alias.name] = target
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = modules.get(alias.name)
                    if target is not None:
                        module_imports[path][alias.asname or alias.name.split(".", 1)[0]] = target
    return classes, imports, module_imports


def _resolved_reverse_class(path, name, bindings, seen=None):
    """Resolve a class name through local definitions and scanned-module re-exports."""
    classes, imports, _ = bindings
    seen = set() if seen is None else seen
    if (path, name) in seen:
        return None
    seen.add((path, name))
    if name in classes[path]:
        return path, classes[path][name]
    imported = imports[path].get(name)
    return _resolved_reverse_class(*imported, bindings, seen) if imported else None


def _reverse_bases(path, node, bindings):
    """Yield bases resolved to classes in scanned test modules."""
    module_imports = bindings[2]
    for base in node.bases:
        if isinstance(base, ast.Name):
            resolved = _resolved_reverse_class(path, base.id, bindings)
        elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            module = module_imports[path].get(base.value.id)
            resolved = _resolved_reverse_class(module, base.attr, bindings) if module else None
        else:
            resolved = None
        if resolved:
            yield resolved


def _is_simple_test_case(path, node: ast.ClassDef, bindings, seen=None) -> bool:
    """Return whether *node* derives from `SimpleTestCase` through known test classes."""
    seen = set() if seen is None else seen
    if id(node) in seen:
        return False
    seen.add(id(node))
    return any(
        (isinstance(base, ast.Name) and base.id == "SimpleTestCase")
        or (isinstance(base, ast.Attribute) and base.attr == "SimpleTestCase")
        for base in node.bases
    ) or any(
        _is_simple_test_case(base_path, base, bindings, seen)
        for base_path, base in _reverse_bases(path, node, bindings)
    )


def _overrides_root_urlconf(path, node: ast.ClassDef, bindings, seen=None) -> bool:
    """Return whether *node* or a known base overrides `ROOT_URLCONF`."""
    seen = set() if seen is None else seen
    if id(node) in seen:
        return False
    seen.add(id(node))
    return any(
        isinstance(decorator, ast.Call) and any(keyword.arg == "ROOT_URLCONF" for keyword in decorator.keywords)
        for decorator in node.decorator_list
    ) or any(
        _overrides_root_urlconf(base_path, base, bindings, seen)
        for base_path, base in _reverse_bases(path, node, bindings)
    )


def _is_reverse_call(node: ast.AST) -> bool:
    """Return whether *node* calls `reverse`, by either spelling."""
    if not isinstance(node, ast.Call):
        return False
    return (isinstance(node.func, ast.Name) and node.func.id == "reverse") or (
        isinstance(node.func, ast.Attribute) and node.func.attr == "reverse"
    )


def _test_class_index(paths: list[pathlib.Path]) -> dict[pathlib.Path, list[ast.ClassDef]]:
    """Collect classes from every scanned test module."""
    return {
        path: [
            node
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            if isinstance(node, ast.ClassDef)
        ]
        for path in paths
    }


def _unisolated_reverse_classes(
    path: pathlib.Path, class_index: dict[pathlib.Path, list[ast.ClassDef]] | None = None
) -> set[str]:
    """Return the `SimpleTestCase` classes in *path* that reverse against the real root URLconf."""
    class_index = class_index if class_index is not None else _test_class_index([path])
    bindings = _reverse_class_bindings(class_index)
    found = set()
    for node in class_index[path]:
        if not _is_simple_test_case(path, node, bindings):
            continue
        if _overrides_root_urlconf(path, node, bindings):
            continue
        if any(_is_reverse_call(child) for statement in node.body for child in ast.walk(statement)):
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


def _module_alias_targets(tree: ast.AST, package: str) -> dict[str, set[str]]:
    """Map each name bound in *tree* to every plugin module it can refer to.

    A name can be bound more than once, in separate scopes or in sequence. Every binding is kept,
    so a later one cannot exempt an access made through an earlier one.
    """
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            module = resolve_name(base, package) if node.level else (node.module or "")
            if not module.startswith(PLUGIN_PACKAGE):
                continue
            for alias in node.names:
                if not alias.name.startswith("_"):
                    aliases.setdefault(alias.asname or alias.name, set()).update(
                        _walk_module_chain({module}, [alias.name])
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith(PLUGIN_PACKAGE):
                    continue
                bound = alias.asname or alias.name.split(".")[0]
                aliases.setdefault(bound, set()).add(alias.name if alias.asname else bound)
    _propagate_copied_references(tree, aliases)
    return aliases


def _propagate_copied_references(tree: ast.AST, aliases: dict[str, set[str]]) -> None:
    """Treat `name = <module reference>` as another binding, to a fixed point."""
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            parts = _dotted_parts(node.value)
            if not isinstance(target, ast.Name) or parts is None:
                continue
            for owner in _referenced_modules(parts, aliases):
                if owner not in aliases.get(target.id, set()):
                    aliases.setdefault(target.id, set()).add(owner)
                    changed = True


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


@functools.lru_cache(maxsize=1)
def _plugin_modules() -> frozenset[str]:
    """Return every module and subpackage name the plugin defines."""
    names = set()
    for path in PACKAGE.rglob("*.py"):
        name = _module_name(path)
        names.add(name.removesuffix(".__init__"))
    return frozenset(names)


def _module_path(module_name: str) -> pathlib.Path | None:
    """Return the file that defines *module_name*, or None when the plugin does not define it."""
    if module_name == PLUGIN_PACKAGE:
        return PACKAGE / "__init__.py"
    relative = pathlib.Path(*module_name.split(".")[1:])
    for candidate in (PACKAGE / relative.with_suffix(".py"), PACKAGE / relative / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


@functools.cache
def _module_import_aliases(
    module_name: str, seen: frozenset[tuple[str, str]] = frozenset()
) -> frozenset[tuple[str, str]]:
    """Return the (bound name, plugin module) pairs *module_name* imports.

    A module that imports a sibling re-exposes it as an attribute, so `engine.naming` is `naming`.
    """
    if (path := _module_path(module_name)) is None:
        return frozenset()
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0] or module_name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    pairs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            imported = resolve_name(base, package) if node.level else (node.module or "")
            if not imported.startswith(PLUGIN_PACKAGE):
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                if (module_name, bound) in seen:
                    continue
                for target in _walk_module_chain({imported}, [alias.name], seen | {(module_name, bound)}):
                    pairs.add((bound, target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                if target in _plugin_modules():
                    pairs.add((bound, target))
    return frozenset(pairs)


def _walk_module_chain(start: set[str], parts: list[str], seen: frozenset[tuple[str, str]] = frozenset()) -> set[str]:
    """Follow *parts* from each module in *start*, through submodules and re-exported modules.

    Every step lands on a plugin module. A repeated import binding ends a re-export cycle.
    """
    current = set(start)
    for part in parts:
        following = set()
        for module in current:
            if f"{module}.{part}" in _plugin_modules():
                following.add(f"{module}.{part}")
                continue
            following.update(target for bound, target in _module_import_aliases(module, seen) if bound == part)
        current = following
        if not current:
            break
    return current


def _referenced_modules(parts: list[str], aliases: dict[str, set[str]]) -> set[str]:
    """Return every plugin module the dotted name *parts* can refer to.

    Each step must land on a module the plugin defines, so a dotted name that walks into a class
    or a function yields nothing, and the alias fixed point is bounded by a finite set.
    """
    return _walk_module_chain(aliases.get(parts[0], set()) & _plugin_modules(), parts[1:])


def _is_private_name(name: str) -> bool:
    """Return whether *name* is private rather than public or a dunder."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _private_plugin_attributes(path: pathlib.Path, module_name: str | None = None) -> set[str]:
    """Return the private names *path* reads from another plugin module without importing them."""
    module_name = module_name or _module_name(path)
    package = module_name.rpartition(".")[0] or module_name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _module_alias_targets(tree, package)
    found = set()

    def record(owners, attribute):
        for owner in owners:
            if owner.startswith(PLUGIN_PACKAGE) and owner != module_name:
                found.add(f"{owner}.{attribute}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_private_name(node.attr):
            parts = _dotted_parts(node)
            if parts is not None and len(parts) >= 2:
                record(_referenced_modules(parts[:-1], aliases), node.attr)
        elif _is_literal_getattr(node):
            parts = _dotted_parts(node.args[0])
            if parts is not None:
                record(_referenced_modules(parts, aliases), node.args[1].value)
    return found


def _is_literal_getattr(node: ast.AST) -> bool:
    """Return whether *node* is `getattr(<name>, "<private>")`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and _is_private_name(node.args[1].value)
    )


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
        class_index = _test_class_index(_test_modules())
        for path in class_index:
            relative = path.relative_to(PACKAGE)
            for name in _unisolated_reverse_classes(path, class_index):
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

    def test_an_indirect_simple_test_case_is_reported(self):
        source = (
            "class Base(SimpleTestCase):\n    pass\n"
            "class Sample(Base):\n    def test_x(self):\n        reverse('name')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")

            self.assertEqual(_unisolated_reverse_classes(path), {"Sample"})

    def test_an_indirect_simple_test_case_from_another_module_is_reported(self):
        """An imported class keeps its ancestry under a local alias."""
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory) / "base.py"
            base.write_text("class Base(SimpleTestCase):\n    pass\n", encoding="utf-8")
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(
                "from base import Base as Parent\nclass Sample(Parent):\n    def test_x(self):\n        reverse('name')\n",
                encoding="utf-8",
            )

            self.assertEqual(_unisolated_reverse_classes(path, _test_class_index([base, path])), {"Sample"})

    def test_an_unrelated_isolated_base_does_not_isolate_a_local_subclass(self):
        """A same-named class in another module cannot change local ancestry."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(
                "class Base(SimpleTestCase):\n    pass\n"
                "class Sample(Base):\n    def test_x(self):\n        reverse('name')\n",
                encoding="utf-8",
            )
            other = pathlib.Path(directory) / "other.py"
            other.write_text(
                "@override_settings(ROOT_URLCONF=_Conf)\nclass Base(SimpleTestCase):\n    pass\n",
                encoding="utf-8",
            )

            self.assertEqual(_unisolated_reverse_classes(path, _test_class_index([path, other])), {"Sample"})

    def test_an_unrelated_unisolated_base_does_not_change_local_isolation(self):
        """A local isolated base takes precedence over another module's class."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(
                "@override_settings(ROOT_URLCONF=_Conf)\nclass Base(SimpleTestCase):\n    pass\n"
                "class Sample(Base):\n    def test_x(self):\n        reverse('name')\n",
                encoding="utf-8",
            )
            other = pathlib.Path(directory) / "other.py"
            other.write_text("class Base(SimpleTestCase):\n    pass\n", encoding="utf-8")

            self.assertEqual(_unisolated_reverse_classes(path, _test_class_index([path, other])), set())

    def test_an_imported_module_base_resolves_only_through_its_module(self):
        """A module-qualified base uses only its imported test module."""
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory) / "base.py"
            base.write_text("class Base(SimpleTestCase):\n    pass\n", encoding="utf-8")
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(
                "import base as parent\nclass Sample(parent.Base):\n    def test_x(self):\n        reverse('name')\n",
                encoding="utf-8",
            )

            self.assertEqual(_unisolated_reverse_classes(path, _test_class_index([base, path])), {"Sample"})

    def test_an_isolated_base_also_isolates_its_subclass(self):
        source = (
            "@override_settings(ROOT_URLCONF=_Conf)\n"
            "class Base(SimpleTestCase):\n    pass\n"
            "class Sample(Base):\n    def test_x(self):\n        reverse('name')\n"
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

    def _found(self, source, module_name=f"{PLUGIN_PACKAGE}.engine"):
        """Return what the detector reports for *source*, analysed as *module_name*."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text(source, encoding="utf-8")
            return _private_plugin_attributes(path, module_name=module_name)

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
        for source in spellings:
            with self.subTest(source=source):
                self.assertEqual(self._found(source), {f"{PLUGIN_PACKAGE}.naming._resolve_slot"})

    def test_a_from_import_follows_a_reexported_module(self):
        """A module imported from another module keeps its original owner."""
        source = "from .engine import naming\nnaming._resolve_slot(None)"

        self.assertEqual(
            self._found(source, module_name=f"{PLUGIN_PACKAGE}.views"), {f"{PLUGIN_PACKAGE}.naming._resolve_slot"}
        )

    def test_a_public_name_on_a_from_imported_module_is_allowed(self):
        """A public access through a re-export does not cross the private boundary."""
        source = "from .engine import naming\nnaming.numeric_suffix('3')"

        self.assertEqual(self._found(source, module_name=f"{PLUGIN_PACKAGE}.views"), set())

    def test_reexport_chains_and_cycles(self):
        """Follow nested re-exports and stop when module imports form a cycle."""
        with tempfile.TemporaryDirectory() as directory:
            package = pathlib.Path(directory) / PLUGIN_PACKAGE
            package.mkdir()
            for name, source in {
                "__init__": "",
                "naming": "def _resolve_slot(): pass\n",
                "base": "from . import naming\n",
                "relay": "from .base import naming\n",
                "round_trip": "from .returning import naming as forwarded\nfrom . import naming as original\n",
                "returning": "from .round_trip import original as naming\n",
                "first": "from .second import naming\n",
                "second": "from .first import naming\n",
            }.items():
                (package / f"{name}.py").write_text(source, encoding="utf-8")
            with patch(f"{__name__}.PACKAGE", package):
                _plugin_modules.cache_clear()
                _module_import_aliases.cache_clear()
                try:
                    self.assertEqual(
                        self._found("from . import relay\nrelay.naming._resolve_slot()"),
                        {f"{PLUGIN_PACKAGE}.naming._resolve_slot"},
                    )
                    self.assertEqual(
                        self._found("from . import round_trip\nround_trip.forwarded._resolve_slot()"),
                        {f"{PLUGIN_PACKAGE}.naming._resolve_slot"},
                    )
                    self.assertEqual(self._found("from . import first\nfirst.naming._resolve_slot()"), set())
                finally:
                    _plugin_modules.cache_clear()
                    _module_import_aliases.cache_clear()

    def test_a_subpackage_reexport_keeps_its_module_owner(self):
        """Resolve a relative re-export from a package against that package."""
        with tempfile.TemporaryDirectory() as directory:
            package = pathlib.Path(directory) / PLUGIN_PACKAGE
            subpackage = package / "sub"
            subpackage.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (subpackage / "__init__.py").write_text("from . import inner as alias\n", encoding="utf-8")
            (subpackage / "inner.py").write_text("def _private(): pass\n", encoding="utf-8")

            with patch(f"{__name__}.PACKAGE", package):
                _plugin_modules.cache_clear()
                _module_import_aliases.cache_clear()
                try:
                    self.assertEqual(
                        self._found("from . import sub\nsub.alias._private()"),
                        {f"{PLUGIN_PACKAGE}.sub.inner._private"},
                    )
                    self.assertEqual(self._found("from . import sub\nsub.alias.public()"), set())
                finally:
                    _plugin_modules.cache_clear()
                    _module_import_aliases.cache_clear()

    def test_a_scanned_subpackage_initializer_resolves_relative_imports(self):
        """Scan relative imports in a package initializer against its package."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / PLUGIN_PACKAGE / "sub" / "__init__.py"
            path.parent.mkdir(parents=True)
            (path.parents[1] / "__init__.py").write_text("", encoding="utf-8")
            (path.parent / "inner.py").write_text("def _private(): pass\n", encoding="utf-8")
            path.write_text("from . import inner\nfrom .inner import _private\ninner._private()\n", encoding="utf-8")

            with patch(f"{__name__}.PACKAGE", path.parents[1]):
                _plugin_modules.cache_clear()
                _module_import_aliases.cache_clear()
                try:
                    self.assertEqual(_private_plugin_imports(path), {f"{PLUGIN_PACKAGE}.sub.inner._private"})
                    self.assertEqual(_private_plugin_attributes(path), {f"{PLUGIN_PACKAGE}.sub.inner._private"})
                finally:
                    _plugin_modules.cache_clear()
                    _module_import_aliases.cache_clear()

    def test_a_literal_getattr_is_a_violation(self):
        source = 'from . import naming\ngetattr(naming, "_resolve_slot")(1, 2, 3)'

        self.assertEqual(self._found(source), {f"{PLUGIN_PACKAGE}.naming._resolve_slot"})

    def test_a_copied_module_reference_is_a_violation(self):
        source = "from . import naming\nresolver = naming\nresolver._resolve_slot(1, 2, 3)"

        self.assertEqual(self._found(source), {f"{PLUGIN_PACKAGE}.naming._resolve_slot"})

    def test_a_chain_of_copied_references_is_a_violation(self):
        source = "from . import naming\nfirst = naming\nsecond = first\nsecond._resolve_slot(1, 2, 3)"

        self.assertEqual(self._found(source), {f"{PLUGIN_PACKAGE}.naming._resolve_slot"})

    def test_a_later_import_cannot_erase_an_earlier_violation(self):
        """A name rebound to this module must not exempt an access made through the sibling."""
        sequential = "from . import naming as helper\nhelper._resolve_slot(1, 2, 3)\nfrom . import engine as helper"
        scoped = (
            "def run():\n    from . import naming as helper\n    return helper._resolve_slot(1, 2, 3)\n"
            "def other():\n    from . import engine as helper\n    return helper\n"
        )
        for source in (sequential, scoped):
            with self.subTest(source=source):
                self.assertEqual(self._found(source), {f"{PLUGIN_PACKAGE}.naming._resolve_slot"})

    def test_a_module_reached_through_another_modules_import_is_a_violation(self):
        """`engine.naming` is `naming`, so a private read through it crosses the same boundary."""
        source = "from . import engine\nengine.naming._resolve_slot(1, 2, 3)"

        found = self._found(source, module_name=f"{PLUGIN_PACKAGE}.views")

        self.assertEqual(found, {f"{PLUGIN_PACKAGE}.naming._resolve_slot"})

    def test_a_self_rebinding_assignment_terminates(self):
        """A name reassigned to one of its own attributes must not grow the candidates forever."""
        source = 'from . import naming\nnaming = naming.numeric_suffix\nnaming("3")'

        self.assertEqual(self._found(source), set())

    def test_a_private_name_on_an_imported_class_is_not_a_module_violation(self):
        source = "from netbox_interface_name_rules.name_template import NamingContext\nNamingContext._member_map_"

        self.assertEqual(self._found(source), set())

    def test_a_modules_own_private_name_is_not_a_violation(self):
        source = "import netbox_interface_name_rules.naming\nnetbox_interface_name_rules.naming._resolve_slot(1, 2, 3)"

        self.assertEqual(self._found(source, module_name=f"{PLUGIN_PACKAGE}.naming"), set())

    def test_a_dunder_on_a_sibling_module_is_not_a_violation(self):
        source = "from netbox_interface_name_rules import naming\nprint(naming.__name__)"

        self.assertEqual(self._found(source), set())

    def test_a_public_name_on_a_sibling_module_is_not_a_violation(self):
        source = "from . import naming\nnaming.numeric_suffix('3')"

        self.assertEqual(self._found(source), set())

    def test_a_reference_to_the_root_package_resolves_its_attributes(self):
        """A copied root reference must allow public reads and report private reads."""
        public = "import netbox_interface_name_rules\nplugin = netbox_interface_name_rules\nconfig = plugin.config"
        private = "import netbox_interface_name_rules\nplugin = netbox_interface_name_rules\nplugin._private"

        self.assertEqual(self._found(public), set())
        self.assertEqual(self._found(private), {f"{PLUGIN_PACKAGE}._private"})
