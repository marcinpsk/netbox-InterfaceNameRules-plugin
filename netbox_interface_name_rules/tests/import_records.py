# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""One reader of the imports a parsed module makes, for the tests that guard module boundaries."""

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.util import resolve_name


@dataclass(frozen=True)
class ImportRecord:
    """One name that an import statement binds."""

    statement: ast.Import | ast.ImportFrom
    module: str
    """The module as spelled, without the dots of a relative import: `a.b` in `import a.b` and `from ..a.b import c`."""
    level: int
    """The number of dots of a relative import, 0 for an absolute one."""
    name: str | None
    """The imported name: `c` in `from a import c`, None in `import a`."""
    asname: str | None

    @property
    def bound(self) -> str:
        """Return the name the statement binds in the importing namespace."""
        if self.asname:
            return self.asname
        return self.module.split(".", 1)[0] if self.name is None else self.name

    def absolute(self, package: str) -> str:
        """Return the imported module, with a relative import resolved against *package*."""
        return resolve_name("." * self.level + self.module, package)


def import_records(nodes: Iterable[ast.AST]) -> list[ImportRecord]:
    """Return one record per alias of each import statement among *nodes*, in their order.

    Pass `ast.walk(tree)` to read every scope, or `tree.body` to read the module level only.
    """
    records = []
    for node in nodes:
        if isinstance(node, ast.Import):
            records.extend(ImportRecord(node, alias.name, 0, None, alias.asname) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            records.extend(
                ImportRecord(node, node.module or "", node.level, alias.name, alias.asname) for alias in node.names
            )
    return records


def import_statements(records: Iterable[ImportRecord]) -> list[ast.Import | ast.ImportFrom]:
    """Return each statement of *records* once, in their order."""
    return list(dict.fromkeys(record.statement for record in records))
