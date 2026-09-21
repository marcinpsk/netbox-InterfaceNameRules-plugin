# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Test documentation statements that define plugin behavior."""

import importlib
import re
import tempfile
import unittest
from pathlib import Path

import re2
import yaml
from dcim.models import Device, ModuleBay

from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.name_template import evaluate_name_template
from netbox_interface_name_rules.naming import build_variables

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_RE2_AUDIT = importlib.import_module("netbox_interface_name_rules.migrations.0014_validate_re2_patterns")

# Names the engine adds when it renames, so a reference table may list them.
_ENGINE_VARIABLES = frozenset({"base", "channel", "port"})
_MARKDOWN_VARIABLE_ROW = re.compile(r"^\| `\{(\w+)\}`")
_VARIABLE_TABLES = (
    (
        "netbox_interface_name_rules/templates/netbox_interface_name_rules/interfacenamerule_list.html",
        re.compile(r"<td><code>\{(\w+)\}</code>"),
    ),
    ("contrib/README.md", _MARKDOWN_VARIABLE_ROW),
    ("docs/template-variables.md", _MARKDOWN_VARIABLE_ROW),
    ("netbox_interface_name_rules/models.py", re.compile(r"^ {6}\{(\w+)\} +-")),
)


# _blocking_reason() and the staleness check decide these before _rewrite() runs, so NetBox never sees them.
_PREFLIGHT_REASONS = (
    "missing member",
    "stale family",
    "occupied parent name",
    "another channel family",
    "cabled sibling",
)


def _conversion_sentences():
    """Return the conversion section as lowercased, whitespace-collapsed sentences."""
    guide = (_PROJECT_ROOT / "docs" / "template-variables.md").read_text(encoding="utf-8")
    section = guide.split("### Converting an installed flat family", 1)[1].split("### Converter Offset", 1)[0]
    return [sentence for sentence in " ".join(section.lower().split()).split(". ") if sentence]


def _example_conversion_sentences():
    """Return the conversion example as lowercased, whitespace-collapsed sentences."""
    guide = (_PROJECT_ROOT / "docs" / "examples.md").read_text(encoding="utf-8")
    section = guide.split("### Converting a flat family (NetBox 4.7+)", 1)[1].split("What the conversion does", 1)[0]
    return [sentence for sentence in " ".join(section.lower().split()).split(". ") if sentence]


class ConversionDocumentationTest(unittest.TestCase):
    """Keep the conversion preflight description consistent with its implementation."""

    def test_missing_members_are_not_described_as_a_rollback_rejection(self):
        normalized = ". ".join(_conversion_sentences())

        self.assertIn("rejects missing members locally before a conversion transaction starts", normalized)
        self.assertNotIn("a missing sibling", normalized)

    def test_preflight_reasons_are_credited_to_the_plugin(self):
        sentences = _conversion_sentences()
        local = " ".join(s for s in sentences if "preflight" in s or "locally" in s)

        for reason in _PREFLIGHT_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, local)

    def test_no_preflight_reason_is_credited_to_netbox(self):
        for sentence in _conversion_sentences():
            if "netbox" not in sentence:
                continue
            for reason in _PREFLIGHT_REASONS:
                with self.subTest(reason=reason, sentence=sentence):
                    self.assertNotIn(reason, sentence)

    def test_example_separates_plugin_preflight_from_netbox_rejection(self):
        sentences = _example_conversion_sentences()
        local = " ".join(s for s in sentences if "preflight" in s or "locally" in s)

        for reason in _PREFLIGHT_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, local)
        for sentence in sentences:
            if "netbox" not in sentence:
                continue
            for reason in _PREFLIGHT_REASONS:
                with self.subTest(reason=reason, sentence=sentence):
                    self.assertNotIn(reason, sentence)


class PerformanceDocumentationTest(unittest.TestCase):
    """Keep the performance narrative consistent with the committed comparison."""

    def test_statement_attribution_matches_the_comparison(self):
        comparison = (_PROJECT_ROOT / "performance" / "comparisons" / "family-package-vs-existing.md").read_text(
            encoding="utf-8"
        )
        attribution = comparison.split("### Where those statements come from", 1)[1]
        changes_by_scenario = {}
        for line in attribution.splitlines():
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if len(cells) != 5 or not cells[0].startswith("module."):
                continue
            change = int(cells[4])
            if change:
                changes_by_scenario.setdefault(cells[0], {})[cells[1]] = change

        readme = (_PROJECT_ROOT / "performance" / "README.md").read_text(encoding="utf-8")
        result = readme.split("## Result of the interface-family comparison", 1)[1]
        readme_changes = {}
        for line in result.split("Count the statements", 1)[0].splitlines():
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if len(cells) == 4 and re.fullmatch(r"[-+]?\d+", cells[3]):
                readme_changes[cells[0]] = int(cells[3])

        narrative = readme.split("No shared-buffer reads were observed", 1)[1].split("That revalidation", 1)[0].lower()
        sources = {source for changes in changes_by_scenario.values() for source in changes}

        self.assertTrue(changes_by_scenario)
        for source in sorted(sources):
            with self.subTest(source=source):
                self.assertIn(source.lower(), narrative)
        for scenario, changes in changes_by_scenario.items():
            with self.subTest(scenario=scenario):
                self.assertEqual(readme_changes.get(scenario.rsplit(".", 1)[-1]), sum(changes.values()))


class ReviewedDocumentationContractTest(unittest.TestCase):
    """Keep reviewed compatibility and transaction statements complete."""

    def test_rule_priority_lists_every_specificity_score(self):
        guide = (_PROJECT_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
        priority = guide.split("### Rule Priority", 1)[1].split("### RE2 Pattern Syntax", 1)[0]
        scopes = {
            7: (
                "Exact module type + parent module type + device type + platform",
                "Regex pattern + parent module type + device type + platform",
            ),
            6: (
                "Exact module type + parent module type + device type",
                "Regex pattern + parent module type + device type",
            ),
            5: ("Exact module type + parent module type + platform", "Regex pattern + parent module type + platform"),
            4: ("Exact module type + parent module type", "Regex pattern + parent module type"),
            3: ("Exact module type + device type + platform", "Regex pattern + device type + platform"),
            2: ("Exact module type + device type", "Regex pattern + device type"),
            1: ("Exact module type + platform", "Regex pattern + platform"),
            0: ("Exact module type only", "Regex pattern only"),
        }

        for score, (exact_scope, regex_scope) in scopes.items():
            with self.subTest(score=score):
                self.assertIn(
                    f"| {score} | {exact_scope} | {regex_scope} |",
                    priority,
                )

    def test_transaction_adr_states_unrelated_failure_behavior(self):
        adr = (_PROJECT_ROOT / "docs" / "adr" / "0005-execute-each-family-in-its-own-transaction.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "An unrelated integrity or infrastructure failure rolls back its own family and propagates to the operation boundary.",
            adr,
        )

    def test_re2_upgrade_guide_separates_errors_from_warnings(self):
        guide = (_PROJECT_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
        section = guide.split("## Run Database Migrations", 1)[1].split("## Restart NetBox", 1)[0]
        migration = " ".join(section.split())

        self.assertIn("The migration stops", migration)
        self.assertIn("The migration warns and continues", migration)
        self.assertIn(r"`\d`, `\s`, and `\w`", migration)
        self.assertIn("case-insensitive matching outside a negated character class", migration)
        self.assertIn("Django records the warning-only migration as applied", migration)
        self.assertIn("Do not rerun the completed migration", migration)
        self.assertIn("If the migration stops", migration)
        self.assertIn("run the migration again", migration)

    def test_configuration_names_both_pattern_matching_contexts(self):
        guide = (_PROJECT_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
        pattern_guidance = guide.split("### Rule Fields", 1)[1].split("### RE2 Pattern Syntax", 1)[0]

        self.assertIn("module type model name", pattern_guidance)
        # Device-interface rules match any device interface, including a standalone one such as
        # mgmt0; saying "family parent" would send an operator to the wrong scope.
        self.assertIn("device interface's current name", pattern_guidance)
        self.assertNotIn("family parent", pattern_guidance)

    def test_pattern_help_text_names_both_matching_contexts(self):
        """The field is a module-type matcher for module rules and a name filter for device rules."""
        from netbox_interface_name_rules.models import InterfaceNameRule

        help_text = InterfaceNameRule._meta.get_field("module_type_pattern").help_text

        self.assertIn("module type model name", help_text)
        self.assertIn("interface name", help_text)
        self.assertIn("Applies to Device Interfaces", help_text)

    def test_readme_badge_matches_the_supported_netbox_floor(self):
        readme = (_PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("NetBox-%E2%89%A54.3.0-blue", readme)
        self.assertNotIn("NetBox-%E2%89%A54.2.0-blue", readme)


_PATTERN_KEY = re.compile(r"^\s*-?\s*module_type_pattern:\s*(.+)$", re.MULTILINE)


def _patterns_in(node):
    """Yield every module_type_pattern nested anywhere in a loaded YAML document.

    A value that is not a string is an error rather than a skip: an unquoted pattern such as
    `[A-Z]` parses as a list, and a pattern the reader drops is one `re2.compile` never sees.
    """
    if isinstance(node, dict):
        if "module_type_pattern" in node:
            pattern = node["module_type_pattern"]
            if not isinstance(pattern, str):
                raise TypeError(f"module_type_pattern is not a string: {pattern!r}")
            yield pattern
        for value in node.values():
            yield from _patterns_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _patterns_in(value)


def _patterns_in_markdown(path):
    """Yield every module-type pattern a guide documents, ignoring what an HTML comment retired.

    Reads the guides the way `_templates_in_markdown` does, for the same two reasons.
    """
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    if "<!--" in text:
        raise ValueError(f"{path.name} holds an unterminated comment, so its patterns cannot be read")
    for raw in _PATTERN_KEY.findall(text):
        value = yaml.safe_load(raw)
        if not isinstance(value, str):
            raise TypeError(f"{path.name} documents a module_type_pattern that is not a string: {raw!r}")
        yield value


def _shipped_patterns():
    """Return every module-type pattern the plugin ships or documents, by source."""
    found = []
    for path in sorted((_PROJECT_ROOT / "contrib").glob("*.yaml")):
        found.extend((path.name, pattern) for pattern in _patterns_in(yaml.safe_load(path.read_text(encoding="utf-8"))))
    for directory in ("docs", "contrib"):
        for path in sorted((_PROJECT_ROOT / directory).glob("*.md")):
            found.extend((path.name, pattern) for pattern in _patterns_in_markdown(path))
    return found


class ShippedPatternRe2AuditTest(unittest.TestCase):
    """Every pattern the plugin ships or documents must survive the RE2 upgrade audit."""

    def test_no_shipped_pattern_blocks_the_upgrade(self):
        patterns = _shipped_patterns()

        self.assertTrue(patterns)
        for source, pattern in patterns:
            with self.subTest(source=source, pattern=pattern):
                re.compile(pattern)
                re2.compile(pattern)
                self.assertFalse(_RE2_AUDIT._uses_different_re2_semantics(pattern))

    def test_every_shipped_and_documented_source_contributes_patterns(self):
        """A renamed file or a dropped directory would otherwise shrink the audit in silence."""
        expected = {
            "cisco.yaml",
            "demo-vc.yaml",
            "juniper-channelized.yaml",
            "juniper.yaml",
            "linux.yaml",
            "ufispace-device-type.yaml",
            "ufispace.yaml",
            "README.md",
            "examples.md",
            "template-variables.md",
        }

        self.assertEqual({source for source, _ in _shipped_patterns()}, expected)

    def test_a_non_string_pattern_in_a_shipped_file_is_an_error(self):
        """An unquoted `[A-Z]` parses as a list, and a skipped pattern is one re2 never compiles."""
        with self.assertRaisesRegex(TypeError, "module_type_pattern"):
            list(_patterns_in({"rules": [{"module_type_pattern": ["A-Z"]}]}))

    def test_a_non_string_pattern_in_a_guide_is_an_error(self):
        """A bare RE2 quantifier such as `{2,3}` parses as a mapping."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text("- module_type_pattern: {2,3}\n", encoding="utf-8")

            with self.assertRaisesRegex(TypeError, "module_type_pattern"):
                list(_patterns_in_markdown(path))

    def test_a_commented_out_pattern_is_not_audited(self):
        """A retired example is not on the page, so enforcing it would report a guide as stale."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text(
                '- module_type_pattern: "QSFP-.*"\n<!--\n- module_type_pattern: "RETIRED-.*"\n-->\n',
                encoding="utf-8",
            )

            self.assertEqual(list(_patterns_in_markdown(path)), ["QSFP-.*"])


def _first_table_variables(path, pattern):
    """Return the variables listed in the first variable table of *path*.

    The rows end at the first line that lists none, because a second table further down would
    otherwise cover a variable missing from the first: `docs/template-variables.md` lists the
    device-rule variables in one of its own. A commented-out row is not on the page, so the
    comments go first, and an unterminated one is an error rather than a row that counts.
    """
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    if "<!--" in text:
        raise ValueError(f"{path.name} holds an unterminated comment, so its table cannot be read")
    names = []
    for line in text.splitlines():
        match = pattern.search(line)
        if match:
            names.append(match.group(1))
        elif names:
            break
    return frozenset(names)


def _built_variables():
    """Return every variable name `build_variables` produces, from the function itself.

    Neither call reaches the database: the resolvers read the position, name, parent and module
    of the bay, and the virtual-chassis fields of the device.
    """
    member = Device(virtual_chassis_id=1, vc_position=1)
    return frozenset(build_variables(ModuleBay())) | frozenset(build_variables(ModuleBay(), member))


class TemplateVariableReferenceTest(unittest.TestCase):
    """Every reference table lists every variable `build_variables` produces.

    Three tables restate that set, and the two variables this feature added reached only one of
    them. A table may also list `base`, `channel` and `port`, which the engine adds at rename
    time; anything else it lists does not exist.
    """

    def test_a_commented_out_row_is_not_documented(self):
        """A row inside an HTML comment is not on the page, so it must not satisfy the guard."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.html"
            path.write_text(
                "<tr><td><code>{slot}</code></td></tr>\n<!-- <tr><td><code>{slot_num}</code></td></tr> -->\n",
                encoding="utf-8",
            )

            self.assertEqual(_first_table_variables(path, _VARIABLE_TABLES[0][1]), frozenset({"slot"}))

    def test_an_unterminated_comment_is_an_error(self):
        """It hides every row below it on the page, so reading the rows would report a stale table."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.html"
            path.write_text(
                "<tr><td><code>{slot}</code></td></tr>\n<!-- <tr><td><code>{slot_num}</code></td></tr>\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unterminated comment"):
                _first_table_variables(path, _VARIABLE_TABLES[0][1])

    def test_every_reference_table_lists_every_built_variable(self):
        built = _built_variables()

        for path, pattern in _VARIABLE_TABLES:
            with self.subTest(path=path):
                documented = _first_table_variables(_PROJECT_ROOT / path, pattern)

                self.assertEqual(documented - _ENGINE_VARIABLES, built)


_TEMPLATE_KEY = re.compile(r"^\s*-?\s*(?:parent_)?name_template:\s*(.+)$", re.MULTILINE)
_HELP_TEXT_EXAMPLE = re.compile(r"'([^']*\{[^']*)'")
_TEMPLATE_SOURCES = ("contrib/*.yaml", "docs/*.md", "contrib/*.md", "models.py")
_UI = "netbox_interface_name_rules/templates/netbox_interface_name_rules"
_OFFSET = "{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}"
_CISCO_OFFSET = f"GigabitEthernet{{slot_num}}/{_OFFSET}"
_FORM_EXAMPLE = "e.g. et-0/0/{bay_position} or {base}:{channel}"
_PARENT_FORM_EXAMPLE = "parent interface, e.g. et-0/0/{bay_position}"
_UI_LIST = f"{_UI}/interfacenamerule_list.html"
_YAML_KEY = f'name_template: "{_CISCO_OFFSET}"'
# Examples that state the language rather than define a rule; each anchor is unique in its file.
_PINNED_EXAMPLES = (
    ("README.md", _OFFSET, (_OFFSET,)),
    ("docs/index.md", _OFFSET, (_OFFSET,)),
    # Line 233 of the guide is a name_template value the extractor already reads; anchor the prose one.
    ("docs/template-variables.md", f"\n{_OFFSET}\n", (_OFFSET,)),
    (_UI_LIST, f"<code>{_OFFSET}</code>", (_OFFSET,)),
    (_UI_LIST, f"<code>{_CISCO_OFFSET}</code>", (_CISCO_OFFSET,)),
    (f"{_UI}/rule_test.html", f"<code>{_OFFSET}</code>", (_OFFSET,)),
    ("contrib/README.md", "{{slot_num} // 2}", ("{{slot_num} // 2}",)),
    (
        "netbox_interface_name_rules/name_template.py",
        "GigabitEthernet{slot_num}/{8 + {sfp_slot}}",
        ("GigabitEthernet{slot_num}/{8 + {sfp_slot}}",),
    ),
    ("netbox_interface_name_rules/forms.py", _FORM_EXAMPLE, ("et-0/0/{bay_position}", "{base}:{channel}")),
    ("netbox_interface_name_rules/forms.py", _PARENT_FORM_EXAMPLE, ("et-0/0/{bay_position}",)),
    # Evaluating the converter-offset rule is not enough: a raw slot in its literal prefix renders.
    ("contrib/README.md", _YAML_KEY, (_CISCO_OFFSET,)),
    ("contrib/converters.yaml", _YAML_KEY, (_CISCO_OFFSET,)),
    ("docs/examples.md", _YAML_KEY, (_CISCO_OFFSET,)),
    ("docs/template-variables.md", _YAML_KEY, (_CISCO_OFFSET,)),
    ("netbox_interface_name_rules/models.py", f"'{_CISCO_OFFSET}'", (_CISCO_OFFSET,)),
)


def _templates_in(node):
    """Yield every name template nested anywhere in a loaded YAML document."""
    if isinstance(node, dict):
        for key in ("name_template", "parent_name_template"):
            if key not in node:
                continue
            if not isinstance(node[key], str):
                raise TypeError(f"{key} is not a string: {node[key]!r}")
            yield node[key]
        for value in node.values():
            yield from _templates_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _templates_in(value)


def _templates_in_markdown(path):
    """Yield every name template a guide defines, ignoring what an HTML comment retired.

    `_first_table_variables` drops commented-out rows for the same reason: a retired example is
    not on the page, so enforcing it would report a guide that no longer says what it says.
    """
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    if "<!--" in text:
        raise ValueError(f"{path.name} holds an unterminated comment, so its examples cannot be read")
    for raw in _TEMPLATE_KEY.findall(text):
        value = yaml.safe_load(raw)
        if not isinstance(value, str):
            raise TypeError(f"{path.name} has a name_template that is not a string: {raw!r}")
        yield value


def _documented_templates():
    """Return every name template the plugin ships or documents, grouped by the source it came from.

    The value is read from the key that defines a rule, the way `_shipped_patterns` reads a
    pattern. Scanning prose for braces was tried over two review rounds and abandoned:
    documentation also holds counter-examples the engine must reject, NetBox's own
    `{vc_position:0}` token syntax, placeholders such as `{N}`, regex quantifiers such as `{0,3}`
    and a literal stray `{`, and no filter separated those from a template reliably. The examples
    that live in prose are pinned by text in `_PINNED_EXAMPLES` instead.
    """
    found = {source: [] for source in _TEMPLATE_SOURCES}
    for path in sorted((_PROJECT_ROOT / "contrib").glob("*.yaml")):
        found["contrib/*.yaml"].extend(
            (path.name, template) for template in _templates_in(yaml.safe_load(path.read_text(encoding="utf-8")))
        )
    for source in ("docs/*.md", "contrib/*.md"):
        directory, pattern = source.split("/")
        for path in sorted((_PROJECT_ROOT / directory).glob(pattern)):
            found[source].extend((path.name, template) for template in _templates_in_markdown(path))
    for name in ("name_template", "parent_name_template"):
        help_text = str(InterfaceNameRule._meta.get_field(name).help_text)
        found["models.py"].extend(("models.py", t) for t in _HELP_TEXT_EXAMPLE.findall(help_text))
    return found


def _example_variables():
    """Return the variables a documented template is read against, from a composed bay.

    The position is path-shaped because a device type may compose the parent into it, which is
    what makes a template that reads a raw position inside arithmetic fail here. Neither call
    reaches the database: the resolvers read the position and parent of the bay and the
    virtual-chassis fields of the device.
    """
    composed = ModuleBay(position="Gi3/2/1", parent=ModuleBay(position="Gi3/2"))
    variables = build_variables(composed, Device(virtual_chassis_id=1, vc_position=1))
    variables.update({"base": "et-0/0/1", "channel": "0", "port": "1"})
    return variables


class DocumentedTemplateTest(unittest.TestCase):
    """Every name template the plugin ships or documents must be one the engine can evaluate.

    The converter-offset example restates the variable contract on six surfaces, and the
    correction that moved it to the numeric variables reached only the UI table. Two shapes go
    wrong: a bare identifier inside a brace group, which the engine never substitutes and then
    rejects, and a raw position inside arithmetic, which reaches the expression as a path.
    """

    def test_the_example_positions_stay_path_shaped(self):
        """If they became numeric, every raw-position example would start passing silently."""
        variables = _example_variables()

        for name in ("slot", "bay_position", "parent_bay_position"):
            with self.subTest(name=name):
                self.assertFalse(str(variables[name]).isdigit())

    def test_every_source_still_defines_templates(self):
        """A moved directory or a renamed key would otherwise drop a source with a green suite."""
        for source, templates in _documented_templates().items():
            with self.subTest(source=source):
                self.assertTrue(templates, f"{source} defines no template, so its examples are unchecked")

    def test_every_documented_template_evaluates(self):
        """`{slot_num // 2}` reads like arithmetic, but the engine never substitutes a bare name."""
        variables = _example_variables()

        for source, templates in _documented_templates().items():
            for name, template in templates:
                with self.subTest(source=source, name=name, template=template):
                    evaluate_name_template(template, variables)

    def test_every_pinned_example_is_present_and_evaluates(self):
        """The prose examples are pinned, because reading them out of prose is what failed."""
        variables = _example_variables()

        for name, anchor, templates in _PINNED_EXAMPLES:
            text = (_PROJECT_ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name, anchor=anchor):
                self.assertEqual(text.count(anchor), 1, f"{name} must hold {anchor!r} exactly once")
            for template in templates:
                with self.subTest(name=name, template=template):
                    evaluate_name_template(template, variables)

    def test_the_tester_variable_table_lists_what_the_preview_derives(self):
        """This diff had to add two rows to that table, and no test read it."""
        from netbox_interface_name_rules.tests.conftest import _preview_key_contract

        _, variables = _preview_key_contract()
        path = _PROJECT_ROOT / _UI / "rule_test.html"

        listed = _first_table_variables(path, _VARIABLE_TABLES[0][1])

        self.assertEqual(listed, set(variables))
