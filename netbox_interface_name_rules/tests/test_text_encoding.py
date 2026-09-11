# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Require explicit encodings for text file access in the package."""

import ast
import pathlib

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]


def _missing_encodings(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", None)
        if name not in {"open", "read_text", "write_text"}:
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        if name == "open":
            module_open = (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id in {"io", "builtins"}
            )
            index = 0 if isinstance(function, ast.Attribute) and not module_open else 1
            mode = next((keyword.value for keyword in node.keywords if keyword.arg == "mode"), None)
            if mode is None and len(node.args) > index:
                mode = node.args[index]
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
                continue
        yield node.lineno


class TextEncodingTest(SimpleTestCase):
    def test_text_file_calls_declare_an_encoding(self):
        violations = []
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            violations.extend(f"{path.relative_to(PACKAGE)}:{line}" for line in _missing_encodings(tree))
        self.assertEqual(violations, [], "Missing encoding= at:\n" + "\n".join(violations))

    def test_binary_modes_do_not_need_an_encoding(self):
        tree = ast.parse('open("data", "rb"); path.open("wb"); open("data", mode="ab"); path.open(mode="rb")')
        self.assertEqual(list(_missing_encodings(tree)), [])

    def test_text_access_requires_an_encoding_keyword(self):
        tree = ast.parse('open("data")\npath.open("r")\npath.read_text()\npath.write_text("text")')
        self.assertEqual(list(_missing_encodings(tree)), [1, 2, 3, 4])

    def test_module_open_uses_the_second_argument_as_mode(self):
        tree = ast.parse('io.open("blob.txt", "r")\nbuiltins.open("blob.txt")\nio.open("data.txt", "rb")')
        self.assertEqual(list(_missing_encodings(tree)), [1, 2])
