# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""scripts/load_demo_data.py on the test database."""

import importlib.util
import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from netbox_interface_name_rules.models import InterfaceNameRule

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "load_demo_data.py"
_spec = importlib.util.spec_from_file_location("load_demo_data", SCRIPT)
load_demo_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_demo_data)

_REGEX_ROW = """- module_type_pattern: "DEMO-.*"
  module_type_is_regex: true
  name_template: "{name_template}"
"""


def run(function, *args):
    """Call a function of the loader, and return its result and what it printed."""
    output = io.StringIO()
    with redirect_stdout(output):
        result = function(*args)
    return result, output.getvalue()


class LoaderTestCase(TestCase):
    def setUp(self):
        (self.site, self.manufacturer, self.role), _ = run(load_demo_data.shared_objects)

    def use_contrib(self, files: dict[str, str]) -> None:
        """Point CONTRIB_DIR at a temporary directory that holds the YAML *files*."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name, text in files.items():
            (Path(directory.name) / name).write_text(text, encoding="utf-8")
        patcher = patch.object(load_demo_data, "CONTRIB_DIR", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def load_rules(self) -> tuple[int, str]:
        run(load_demo_data.load_rule_scopes, self.manufacturer)
        return run(load_demo_data.load_rules)


class ContribRowsTest(LoaderTestCase):
    def test_a_row_that_is_not_a_mapping_is_a_skipped_rule(self):
        self.use_contrib({"rules.yaml": _REGEX_ROW.format(name_template="x{bay_position}") + "-\n- stray\n"})

        skipped, output = self.load_rules()

        self.assertEqual(skipped, 2)
        self.assertIn("rules.yaml row 2", output)
        self.assertIn("rules.yaml row 3", output)
        self.assertEqual(InterfaceNameRule.objects.filter(module_type_pattern="DEMO-.*").count(), 1)
