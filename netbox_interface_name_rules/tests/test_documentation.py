# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Test documentation statements that define plugin behavior."""

import ast
import importlib
import json
import re
import tempfile
import unittest
from dataclasses import dataclass
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import re2
import yaml
from dcim.models import Device, ModuleBay
from django.core.management import call_command
from django.core.management.base import CommandError

from netbox_interface_name_rules import template_variable_reference
from netbox_interface_name_rules.models import InterfaceNameRule
from netbox_interface_name_rules.name_template import (
    TEMPLATE_VARIABLES,
    NamingContext,
    evaluate_name_template,
    variables_for_context,
)
from netbox_interface_name_rules.naming import build_variables
from netbox_interface_name_rules.template_variable_reference import (
    GENERATED_REFERENCE_REGIONS,
    GENERATED_REGION_BEGIN,
    GENERATED_REGION_END,
    GeneratedReferenceRegion,
    outdated_generated_regions,
    regenerated_document,
    render_markdown_reference,
    write_generated_regions,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_RE2_AUDIT = importlib.import_module("netbox_interface_name_rules.migrations.0014_validate_re2_patterns")

_CONVERTER_OFFSET_SCENARIO = re.compile(
    r"^# Slot (?P<slot_num>\d+), parent bay (?P<parent_bay_position_num>\d+), "
    r"SFP slot (?P<sfp_slot>\d+) → (?P<expected_name>\S+)$",
    re.MULTILINE,
)


class TemplateVariableCatalogueTest(unittest.TestCase):
    """The catalogue describes the reference shown to operators and agents."""

    def test_every_variable_carries_its_context_descriptions_and_example(self):
        for variable in TEMPLATE_VARIABLES:
            provider_contexts = {context for context, _source in variable.providers}
            with self.subTest(variable=variable.name):
                self.assertEqual(set(dict(variable.descriptions)), provider_contexts)
                self.assertTrue(variable.example)

    def test_base_description_matches_each_input_name(self):
        base = next(variable for variable in TEMPLATE_VARIABLES if variable.name == "base")
        module_description = (
            "The name the rule starts from, which is the module's raw template name when the rule first applies."
        )

        self.assertEqual(
            dict(base.descriptions),
            {
                NamingContext.MODULE_MEMBER: module_description,
                NamingContext.MODULE_PARENT: module_description,
                NamingContext.DEVICE_INTERFACE: "Current interface name before the rule applies.",
            },
        )


def _line_after_generated_region(text):
    """Return the first nonblank line after the generated region, or an empty string at the end."""
    return text.split(GENERATED_REGION_END, 1)[1].lstrip("\n").split("\n", 1)[0]


def _closes_generated_region(line, region):
    """Return whether a line ends the region's outermost section instead of joining its last table."""
    outer_level = region.heading_level - 1 if region.title else region.heading_level
    heading = re.match(r"(#+) ", line)
    return not line or (heading is not None and len(heading.group(1)) <= outer_level)


class GeneratedTemplateVariableReferenceTest(unittest.TestCase):
    """Generated references stay synchronized with the language catalogue."""

    def test_generated_regions_match_a_fresh_regeneration(self):
        self.assertEqual(outdated_generated_regions(), ())

    def test_generated_reference_names_each_rule_field(self):
        reference = render_markdown_reference(3)

        self.assertIn(
            "### Module member names\n\nModule rules use these variables in the Name Template field.", reference
        )
        self.assertIn(
            "### Module parent names\n\nModule rules use these variables in the Parent Name Template field.", reference
        )
        self.assertIn(
            "### Device interface names\n\nDevice-interface rules use these variables in the Name Template field.",
            reference,
        )

    def test_writer_repairs_a_stale_region(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "guide.md"
            path.write_text(
                f"Before\n{GENERATED_REGION_BEGIN}\nstale\n{GENERATED_REGION_END}\nAfter\n",
                encoding="utf-8",
            )
            region = GeneratedReferenceRegion(path, 3)
            with (
                patch.object(template_variable_reference, "PROJECT_ROOT", root),
                patch.object(template_variable_reference, "GENERATED_REFERENCE_REGIONS", (region,)),
            ):
                self.assertEqual(outdated_generated_regions(), ("guide.md",))
                self.assertEqual(write_generated_regions(), ("guide.md",))
                self.assertEqual(outdated_generated_regions(), ())

            self.assertIn(render_markdown_reference(3), path.read_text(encoding="utf-8"))

    def test_management_command_reports_updated_then_current_regions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "guide.md"
            path.write_text(
                f"{GENERATED_REGION_BEGIN}\nstale\n{GENERATED_REGION_END}\n",
                encoding="utf-8",
            )
            region = GeneratedReferenceRegion(path, 3)
            with (
                patch.object(template_variable_reference, "PROJECT_ROOT", root),
                patch.object(template_variable_reference, "GENERATED_REFERENCE_REGIONS", (region,)),
            ):
                updated_output = StringIO()
                call_command("generate_template_variable_reference", stdout=updated_output)
                current_output = StringIO()
                call_command("generate_template_variable_reference", stdout=current_output)

            self.assertEqual(updated_output.getvalue(), "Updated guide.md\n")
            self.assertEqual(current_output.getvalue(), "Template-variable references are current.\n")

    def test_management_command_rejects_missing_region_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "guide.md"
            stale = f"{GENERATED_REGION_BEGIN}\nstale\n{GENERATED_REGION_END}\n"
            path.write_text(stale, encoding="utf-8")
            missing = root / "missing.md"
            regions = (GeneratedReferenceRegion(path, 3), GeneratedReferenceRegion(missing, 3))
            with (
                patch.object(template_variable_reference, "PROJECT_ROOT", root),
                patch.object(template_variable_reference, "GENERATED_REFERENCE_REGIONS", regions),
            ):
                with self.assertRaises(CommandError) as raised:
                    call_command("generate_template_variable_reference")

            self.assertIn(str(missing), str(raised.exception))
            self.assertIn("source checkout", str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), stale)

    def test_writer_rejects_missing_region_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "guide.md"
            stale = f"{GENERATED_REGION_BEGIN}\nstale\n{GENERATED_REGION_END}\n"
            path.write_text(stale, encoding="utf-8")
            missing = root / "missing.md"
            regions = (GeneratedReferenceRegion(path, 3), GeneratedReferenceRegion(missing, 3))
            with (
                patch.object(template_variable_reference, "PROJECT_ROOT", root),
                patch.object(template_variable_reference, "GENERATED_REFERENCE_REGIONS", regions),
            ):
                with self.assertRaises(FileNotFoundError) as raised:
                    write_generated_regions()

            self.assertIn(str(missing), str(raised.exception))
            self.assertIn("source checkout", str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), stale)

    def test_writer_rejects_malformed_region_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "guide.md"
            stale = f"{GENERATED_REGION_BEGIN}\nstale\n{GENERATED_REGION_END}\n"
            path.write_text(stale, encoding="utf-8")
            malformed = root / "malformed.md"
            malformed.write_text("No generated region.\n", encoding="utf-8")
            regions = (GeneratedReferenceRegion(path, 3), GeneratedReferenceRegion(malformed, 3))
            with (
                patch.object(template_variable_reference, "PROJECT_ROOT", root),
                patch.object(template_variable_reference, "GENERATED_REFERENCE_REGIONS", regions),
            ):
                with self.assertRaises(CommandError) as raised:
                    call_command("generate_template_variable_reference")

            self.assertIn(str(malformed), str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), stale)

    def test_each_generated_region_ends_where_its_section_closes(self):
        for region in GENERATED_REFERENCE_REGIONS:
            with self.subTest(path=str(region.path.relative_to(_PROJECT_ROOT))):
                line = _line_after_generated_region(region.path.read_text(encoding="utf-8"))
                self.assertTrue(_closes_generated_region(line, region), f"{line!r} reads as part of the last table")

    def test_region_end_check_refuses_text_under_the_last_table(self):
        untitled = GeneratedReferenceRegion(Path("guide.md"), 3)
        titled = GeneratedReferenceRegion(Path("guide.md"), 4, "Template variables")

        self.assertTrue(_closes_generated_region("", untitled))
        self.assertTrue(_closes_generated_region("### Next section", untitled))
        self.assertTrue(_closes_generated_region("## Next chapter", titled))
        self.assertFalse(_closes_generated_region("A paragraph.", untitled))
        self.assertFalse(_closes_generated_region("- A list item", titled))
        self.assertFalse(_closes_generated_region("#### Deeper heading", untitled))
        self.assertFalse(_closes_generated_region("#### Sibling of the context headings", titled))
        self.assertEqual(_line_after_generated_region(f"{GENERATED_REGION_END}\n\n## Next\n"), "## Next")

    def test_malformed_generated_region_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text("No generated region.\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must contain exactly one generated template-variable region"):
                regenerated_document(GeneratedReferenceRegion(path, 3))

    def test_agent_instructions_name_the_language_owner(self):
        instructions = (_PROJECT_ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")

        self.assertIn("**`name_template.py`**: Owns the name-template language", instructions)
        self.assertNotIn("`naming.py` replaces known variables", instructions)
        self.assertNotIn("Arithmetic inside braces is parsed with `ast.parse`", instructions)

    def test_rule_model_points_at_the_catalogue_instead_of_relisting_variables(self):
        docstring = InterfaceNameRule.__doc__ or ""

        self.assertIn("TEMPLATE_VARIABLES", docstring)
        for variable in TEMPLATE_VARIABLES:
            with self.subTest(variable=variable.name):
                self.assertNotIn(f"{{{variable.name}}} -", docstring)


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


class BaseVariableDocumentationTest(unittest.TestCase):
    """Keep each module-family base path explicit outside the generated reference."""

    def test_each_module_family_base_path_is_documented(self):
        guide = (_PROJECT_ROOT / "docs" / "template-variables.md").read_text(encoding="utf-8")

        self.assertIn(
            "For a flat breakout family, `{base}` is the module's raw template name on every apply.",
            guide,
        )
        self.assertIn(
            "For an installed channelized family, `{base}` is the installed parent's current name.",
            guide,
        )
        self.assertIn(
            "For a plain interface rename, `{base}` is the interface's current name.",
            guide,
        )


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


_PATTERN_KEY = re.compile(r"^[^\S\r\n]*-?[^\S\r\n]*module_type_pattern:[^\S\r\n]*(.+)$", re.MULTILINE)


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
    visible = _visible_markdown(path)
    blocks = tuple(_MARKDOWN_YAML_BLOCK.finditer(visible))
    block_spans = tuple(block.span(1) for block in blocks)
    for key in _PATTERN_KEY.finditer(visible):
        if not any(start <= key.start() < end for start, end in block_spans):
            line = visible.count("\n", 0, key.start()) + 1
            raise AssertionError(f"{path.name}:{line} has a module-type pattern key outside a fenced YAML block")
    for block in blocks:
        yield from _patterns_in(yaml.safe_load(block.group(1)))


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
            path.write_text("```yaml\n- module_type_pattern: {2,3}\n```\n", encoding="utf-8")

            with self.assertRaisesRegex(TypeError, "module_type_pattern"):
                list(_patterns_in_markdown(path))

    def test_a_commented_out_pattern_is_not_audited(self):
        """A retired example is not on the page, so enforcing it would report a guide as stale."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text(
                '```yaml\n- module_type_pattern: "QSFP-.*"\n```\n'
                '<!--\n```yaml\n- module_type_pattern: "RETIRED-.*"\n```\n-->\n',
                encoding="utf-8",
            )

            self.assertEqual(list(_patterns_in_markdown(path)), ["QSFP-.*"])

    def test_a_blank_line_after_a_yaml_fence_stays_inside_the_pattern_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text('```yaml\n\n- module_type_pattern: "QSFP-.*"\n```\n', encoding="utf-8")

            self.assertEqual(list(_patterns_in_markdown(path)), ["QSFP-.*"])

    def test_a_pattern_in_a_yaml_flow_mapping_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text('```yaml\n- {module_type_pattern: "(?=a)"}\n```\n', encoding="utf-8")

            self.assertEqual(list(_patterns_in_markdown(path)), ["(?=a)"])


_HELP_TEXT_EXAMPLE = re.compile(r"'([^']*\{[^']*)'")
_UI = "netbox_interface_name_rules/templates/netbox_interface_name_rules"
_OFFSET = "{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}"
_FORM_EXAMPLE = "e.g. et-0/0/{bay_position} or {base}:{channel}"
_PARENT_FORM_EXAMPLE = "parent interface, e.g. et-0/0/{bay_position}"
_UI_LIST = f"{_UI}/interfacenamerule_list.html"
_UI_EXAMPLES_LIST_ID = "interface-name-rule-examples"
_MARKDOWN_YAML_BLOCK = re.compile(
    r"^```yaml[^\S\r\n]*\r?\n(.*?)^```[^\S\r\n]*$",
    re.DOTALL | re.MULTILINE,
)
_MARKDOWN_TEMPLATE_KEY = re.compile(
    r"^[^\S\r\n]*-?[^\S\r\n]*(?:parent_)?name_template[^\S\r\n]*:",
    re.MULTILINE,
)
_TEMPLATE_VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")
_MARKDOWN_TABLE_CONTEXTS = {
    "README.md": {"Supported scenarios": NamingContext.MODULE_MEMBER},
    "docs/index.md": {"Supported Scenarios": NamingContext.MODULE_MEMBER},
    "docs/examples.md": {
        "Rule overview for ACX7024": NamingContext.MODULE_MEMBER,
        "Juniper EX Virtual Chassis": NamingContext.DEVICE_INTERFACE,
    },
}
_DOCUMENTED_TEMPLATE_SOURCES = {
    ".devcontainer/scripts/test-e2e.py",
    "README.md",
    "contrib/README.md",
    "contrib/cisco.yaml",
    "contrib/converters.yaml",
    "contrib/demo-vc.yaml",
    "contrib/juniper-channelized.yaml",
    "contrib/juniper.yaml",
    "contrib/linux.yaml",
    "contrib/ufispace-device-type.yaml",
    "contrib/ufispace.yaml",
    "docs/configuration.md",
    "docs/examples.md",
    "docs/index.md",
    "docs/template-variables.md",
    "netbox_interface_name_rules/forms.py",
    "netbox_interface_name_rules/models.py",
    "netbox_interface_name_rules/name_template.py",
    f"{_UI}/interfacenamerule_list.html",
    f"{_UI}/rule_test.html",
}


@dataclass(frozen=True, slots=True)
class DocumentedTemplate:
    """Record a shipped or documented name template and its naming context."""

    source: str
    template: str
    context: NamingContext


class _CodeExampleParser(HTMLParser):
    """Collect marked elements from the rule-list help panel's examples list."""

    def __init__(self):
        super().__init__()
        self.examples = []
        self.examples_list_count = 0
        self.examples_list_depth = 0
        self.open_example = None
        self.unmarked_template_lines = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "ul" and attributes.get("id") == _UI_EXAMPLES_LIST_ID:
            self.examples_list_count += 1
            self.examples_list_depth = 1
        elif tag == "ul" and self.examples_list_depth:
            self.examples_list_depth += 1
        if not self.examples_list_depth or "data-name-template-context" not in attributes:
            return
        if self.open_example is not None:
            raise ValueError("marked name-template example elements cannot be nested")
        self.open_example = [tag, self.getpos()[0], attributes, []]

    def handle_data(self, data):
        if self.open_example is not None:
            self.open_example[3].append(data)
        elif self.examples_list_depth and any(brace in data for brace in "{}"):
            self.unmarked_template_lines.append(self.getpos()[0])

    def handle_endtag(self, tag):
        if self.open_example is not None and tag == self.open_example[0]:
            _tag, line, attributes, parts = self.open_example
            self.examples.append((line, attributes, "".join(parts)))
            self.open_example = None
        if tag == "ul" and self.examples_list_depth:
            self.examples_list_depth -= 1


_PINNED_EXAMPLES = (
    ("docs/template-variables.md", f"\n{_OFFSET}\n", ((_OFFSET, NamingContext.MODULE_MEMBER),)),
    (_UI_LIST, f"<code>{_OFFSET}</code>", ((_OFFSET, NamingContext.MODULE_MEMBER),)),
    (f"{_UI}/rule_test.html", f"<code>{_OFFSET}</code>", ((_OFFSET, NamingContext.MODULE_MEMBER),)),
    ("contrib/README.md", "{{slot_num} // 2}", (("{{slot_num} // 2}", NamingContext.MODULE_MEMBER),)),
    (
        "netbox_interface_name_rules/name_template.py",
        "GigabitEthernet{slot_num}/{8 + {sfp_slot}}",
        (("GigabitEthernet{slot_num}/{8 + {sfp_slot}}", NamingContext.MODULE_MEMBER),),
    ),
    (
        "netbox_interface_name_rules/forms.py",
        _FORM_EXAMPLE,
        (
            ("et-0/0/{bay_position}", NamingContext.MODULE_MEMBER),
            ("{base}:{channel}", NamingContext.MODULE_MEMBER),
        ),
    ),
    (
        "netbox_interface_name_rules/forms.py",
        _PARENT_FORM_EXAMPLE,
        (("et-0/0/{bay_position}", NamingContext.MODULE_PARENT),),
    ),
)


def _templates_in(node, source):
    """Yield every name template nested in loaded rule data with its naming context."""
    if isinstance(node, dict):
        device_rule = node.get("applies_to_device_interfaces") is True
        for key, context in (
            ("name_template", NamingContext.DEVICE_INTERFACE if device_rule else NamingContext.MODULE_MEMBER),
            ("parent_name_template", NamingContext.MODULE_PARENT),
        ):
            if key not in node:
                continue
            if not isinstance(node[key], str):
                raise TypeError(f"{key} is not a string: {node[key]!r}")
            yield DocumentedTemplate(source, node[key], context)
        for key, value in node.items():
            if key not in {"name_template", "parent_name_template"}:
                yield from _templates_in(value, source)
    elif isinstance(node, list):
        for value in node:
            yield from _templates_in(value, source)


def _visible_markdown(path):
    """Return visible Markdown while preserving content between generated-region markers."""
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    if "<!--" in text:
        raise ValueError(f"{path.name} holds an unterminated comment, so its examples cannot be read")
    return text


def _templates_in_markdown(path, source):
    """Yield templates from complete YAML blocks and reject visible keys outside them."""
    visible = _visible_markdown(path)
    blocks = tuple(_MARKDOWN_YAML_BLOCK.finditer(visible))
    templates = tuple(
        template for block in blocks for template in _templates_in(yaml.safe_load(block.group(1)), source)
    )
    block_spans = tuple(block.span(1) for block in blocks)
    for key in _MARKDOWN_TEMPLATE_KEY.finditer(visible):
        if not any(start <= key.start() < end for start, end in block_spans):
            line = visible.count("\n", 0, key.start()) + 1
            raise AssertionError(f"{source}:{line} has a name-template key outside a fenced YAML block")
    yield from templates


def _templates_in_markdown_tables(path, source, contexts):
    """Yield templates from explicitly named Markdown table columns."""
    lines = _visible_markdown(path).splitlines()
    heading = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        if not line.startswith("|"):
            index += 1
            continue
        headers = [cell.strip() for cell in line.strip("|").split("|")]
        if "Name template" not in headers:
            index += 1
            continue
        if heading not in contexts:
            raise AssertionError(f"{source} has no naming context for the {heading!r} template table")
        template_column = headers.index("Name template")
        index += 2
        while index < len(lines) and lines[index].startswith("|"):
            cells = [cell.strip() for cell in lines[index].strip("|").split("|")]
            value = cells[template_column]
            if value not in {"—", "-"}:
                match = re.fullmatch(r"`([^`]+)`", value)
                if match is None:
                    raise AssertionError(f"{source} has an ambiguous name template cell: {value!r}")
                yield DocumentedTemplate(source, match.group(1), contexts[heading])
            index += 1


def _templates_in_configuration_json():
    """Yield templates from JSON request payloads in the configuration guide."""
    source = "docs/configuration.md"
    text = (_PROJECT_ROOT / source).read_text(encoding="utf-8")
    for payload in re.findall(r"-d\s+'(\{[^\n]+\})'", text):
        yield from _templates_in(json.loads(payload), source)


def _templates_in_help_text():
    """Yield model field examples with the naming context of their field."""
    source = "netbox_interface_name_rules/models.py"
    for name, context in (
        ("name_template", NamingContext.MODULE_MEMBER),
        ("parent_name_template", NamingContext.MODULE_PARENT),
    ):
        help_text = str(InterfaceNameRule._meta.get_field(name).help_text)
        for template in _HELP_TEXT_EXAMPLE.findall(help_text):
            yield DocumentedTemplate(source, template, context)


def _templates_in_rule_list_examples():
    """Yield explicitly marked name templates from the rule list help panel."""
    source = _UI_LIST
    parser = _CodeExampleParser()
    parser.feed((_PROJECT_ROOT / source).read_text(encoding="utf-8"))
    parser.close()
    if parser.examples_list_count != 1:
        raise AssertionError(f"{source} must have one {_UI_EXAMPLES_LIST_ID} list")
    if parser.examples_list_depth:
        raise ValueError(f"{source} has an unclosed {_UI_EXAMPLES_LIST_ID} list")
    if parser.open_example is not None:
        raise ValueError(f"{source} has an unclosed marked name-template example element")
    if parser.unmarked_template_lines:
        line = parser.unmarked_template_lines[0]
        raise AssertionError(f"{source}:{line} has a name-template example not marked with a naming context")
    for _line, attributes, template in parser.examples:
        context = attributes["data-name-template-context"]
        yield DocumentedTemplate(source, template, NamingContext(context))


def _templates_in_e2e_script():
    """Yield rule payload templates from the end-to-end development script's syntax tree."""
    source = ".devcontainer/scripts/test-e2e.py"
    path = _PROJECT_ROOT / source
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    payloads = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "dumps":
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        payloads.append(node.args[0])
    for payload in payloads:
        for key, value in zip(payload.keys, payload.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                line = key.lineno if key is not None else value.lineno
                raise TypeError(f"{source}:{line} has a payload key that is not a string literal")
    template_fields = {"name_template", "parent_name_template"}
    payload_key_ids = {
        id(key)
        for payload in payloads
        for key in payload.keys
        if isinstance(key, ast.Constant) and key.value in template_fields
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in template_fields:
            raise AssertionError(f"{source}:{node.lineno} has a name-template key outside a direct dumps dictionary")
        if isinstance(node, ast.Constant) and node.value in template_fields and id(node) not in payload_key_ids:
            raise AssertionError(f"{source}:{node.lineno} has a name-template key outside a direct dumps dictionary")
    for payload in payloads:
        values = {key.value: value for key, value in zip(payload.keys, payload.values, strict=True)}
        device_rule = values.get("applies_to_device_interfaces")
        name_context = (
            NamingContext.DEVICE_INTERFACE
            if isinstance(device_rule, ast.Constant) and device_rule.value is True
            else NamingContext.MODULE_MEMBER
        )
        for field, context in (
            ("name_template", name_context),
            ("parent_name_template", NamingContext.MODULE_PARENT),
        ):
            if field not in values:
                continue
            template = values[field]
            if not isinstance(template, ast.Constant) or not isinstance(template.value, str):
                raise TypeError(f"{source}:{template.lineno} has a {field} that is not a string literal")
            yield DocumentedTemplate(source, template.value, context)


def _documented_templates():
    """Return every shipped and documented template with its declared naming context.

    Readers use structured YAML, JSON, Markdown table columns, marked HTML elements, or Python
    syntax. They do not scan prose for braces. The guides also contain invalid counter-examples,
    NetBox tokens, placeholders, regex quantifiers, and a literal stray brace.
    """
    found = []
    for path in sorted((_PROJECT_ROOT / "contrib").glob("*.yaml")):
        source = str(path.relative_to(_PROJECT_ROOT))
        found.extend(_templates_in(yaml.safe_load(path.read_text(encoding="utf-8")), source))
    for directory in ("docs", "contrib"):
        for path in sorted((_PROJECT_ROOT / directory).glob("*.md")):
            source = str(path.relative_to(_PROJECT_ROOT))
            found.extend(_templates_in_markdown(path, source))
    for source, contexts in _MARKDOWN_TABLE_CONTEXTS.items():
        found.extend(_templates_in_markdown_tables(_PROJECT_ROOT / source, source, contexts))
    found.extend(_templates_in_configuration_json())
    found.extend(_templates_in_help_text())
    found.extend(_templates_in_rule_list_examples())
    found.extend(_templates_in_e2e_script())
    for source, anchor, templates in _PINNED_EXAMPLES:
        text = (_PROJECT_ROOT / source).read_text(encoding="utf-8")
        if text.count(anchor) != 1:
            raise AssertionError(f"{source} must hold {anchor!r} exactly once")
        found.extend(DocumentedTemplate(source, template, context) for template, context in templates)
    return tuple(found)


def _example_variables(context):
    """Return representative values limited to one naming context."""
    composed = ModuleBay(position="Gi3/2/1", parent=ModuleBay(position="Gi3/2"))
    variables = build_variables(composed, Device(virtual_chassis_id=1, vc_position=1))
    variables.update({"base": "et-0/0/1", "channel": "0", "port": "1"})
    available = {variable.name for variable in variables_for_context(context)}
    return {name: value for name, value in variables.items() if name in available}


def _acx7024_guide_rules():
    """Return every module rule in the ACX7024 overview table."""
    text = _visible_markdown(_PROJECT_ROOT / "docs/examples.md")
    section = text.split("### Rule overview for ACX7024", 1)[1].split("### YAML", 1)[0]
    rules = set()
    for line in section.splitlines():
        if not line.startswith("|") or "Module type pattern" in line or line.startswith("|---"):
            continue
        pattern, template, channels, _result = [cell.strip() for cell in line.strip("|").split("|")]
        exact = pattern.endswith(" (exact)")
        selector = pattern.removesuffix(" (exact)").strip("`")
        channel_count = 0 if channels == "—" else int(channels.split()[0])
        rules.add(("module_type" if exact else "module_type_pattern", selector, template.strip("`"), channel_count))
    return rules


def _acx7024_shipped_rules():
    """Return every ACX7024 module rule shipped in the Juniper catalogue."""
    rules = yaml.safe_load((_PROJECT_ROOT / "contrib" / "juniper.yaml").read_text(encoding="utf-8"))
    found = set()
    for rule in rules:
        if rule.get("device_type") != "ACX7024" or rule.get("applies_to_device_interfaces"):
            continue
        key = "module_type_pattern" if "module_type_pattern" in rule else "module_type"
        found.add((key, rule[key], rule["name_template"], rule.get("channel_count", 0)))
    return found


def _shipped_converter_offset_template():
    """Return the converter-offset template from the shipped rule that defines it."""
    rules = yaml.safe_load((_PROJECT_ROOT / "contrib" / "converters.yaml").read_text(encoding="utf-8"))
    matches = [
        rule
        for rule in rules
        if rule.get("module_type") == "SFP-1G-T" and rule.get("parent_module_type") == "CVR-X2-SFP"
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"contrib/converters.yaml must define one SFP-1G-T in CVR-X2-SFP rule, found {len(matches)}"
        )
    template = matches[0].get("name_template")
    if not isinstance(template, str):
        raise TypeError(f"converter-offset name_template is not a string: {template!r}")
    return template


def _documented_converter_offset_scenario():
    """Return the converter-offset inputs and result stated identically in both guides."""
    documented = []
    for source in ("docs/examples.md", "docs/template-variables.md"):
        text = (_PROJECT_ROOT / source).read_text(encoding="utf-8")
        matches = tuple(_CONVERTER_OFFSET_SCENARIO.finditer(text))
        if len(matches) != 1:
            raise AssertionError(f"{source} must state one converter-offset scenario, found {len(matches)}")
        documented.append((source, matches[0]))

    if documented[0][1].group(0) != documented[1][1].group(0):
        raise AssertionError(f"{documented[0][0]} and {documented[1][0]} must state the same converter-offset scenario")
    values = documented[0][1].groupdict()
    expected_name = values.pop("expected_name")
    return values, expected_name


class DocumentedTemplateTest(unittest.TestCase):
    """Every shipped and documented name template must fit its naming context."""

    def test_the_example_positions_stay_path_shaped(self):
        """If they became numeric, every raw-position example would start passing silently."""
        variables = _example_variables(NamingContext.MODULE_MEMBER)

        for name in ("slot", "bay_position", "parent_bay_position"):
            with self.subTest(name=name):
                self.assertFalse(str(variables[name]).isdigit())

    def test_every_source_still_defines_templates(self):
        """A moved file, renamed key, or empty reader must change the exact source set."""
        self.assertEqual({item.source for item in _documented_templates()}, _DOCUMENTED_TEMPLATE_SOURCES)

    def test_configuration_json_payload_is_read(self):
        templates = [item.template for item in _documented_templates() if item.source == "docs/configuration.md"]

        self.assertEqual(templates, ["et-0/0/{bay_position}"])

    def test_markdown_template_tables_are_read(self):
        expected = {
            "README.md": {
                "GigabitEthernet{slot_num}/{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}",
                "et-0/0/{bay_position}:{channel}",
                "et-0/0/{bay_position}",
                "swp{bay_position_num}s{channel}",
            },
            "docs/index.md": {
                "GigabitEthernet{slot_num}/{8 + ({parent_bay_position_num} - 1) * 2 + {sfp_slot}}",
                "et-0/0/{bay_position}",
                "xe-0/0/{bay_position}:{channel}",
                "swp{bay_position_num}",
                "eth{bay_position_num}",
                "ens{slot}f{bay_position_num}",
            },
            "docs/examples.md": {
                "et-0/0/{bay_position}",
                "et-0/0/{bay_position}:{channel}",
                "ge-0/0/{bay_position}",
                "xe-0/0/{bay_position}",
                "xe-0/0/{bay_position}:{channel}",
                "ge-{vc_position}/0/{port}",
                "xe-{vc_position}/1/{port}",
            },
        }

        for source, contexts in _MARKDOWN_TABLE_CONTEXTS.items():
            rows = _templates_in_markdown_tables(_PROJECT_ROOT / source, source, contexts)
            with self.subTest(source=source):
                self.assertEqual({row.template for row in rows}, expected[source])

    def test_rule_list_help_reads_all_eight_marked_examples(self):
        templates = tuple(_templates_in_rule_list_examples())

        self.assertEqual(len(templates), 8)
        self.assertEqual(
            [item.context for item in templates],
            [NamingContext.MODULE_MEMBER] * 5 + [NamingContext.DEVICE_INTERFACE] * 3,
        )

    def test_rule_list_help_rejects_an_unmarked_template_example(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / _UI_LIST
            path.parent.mkdir(parents=True)
            path.write_text(
                f'<ul id="{_UI_EXAMPLES_LIST_ID}">\n'
                '<li><code data-name-template-context="module_member">eth{bay_position_num}</code></li>\n'
                "<li><code><span>{unknown_variable}</span></code></li>\n"
                "</ul>\n",
                encoding="utf-8",
            )

            with (
                patch.object(importlib.import_module(__name__), "_PROJECT_ROOT", root),
                self.assertRaisesRegex(AssertionError, rf"{re.escape(_UI_LIST)}:3 .*not marked"),
            ):
                tuple(_templates_in_rule_list_examples())

    def test_rule_list_help_audits_a_marked_template_on_any_element(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / _UI_LIST
            path.parent.mkdir(parents=True)
            path.write_text(
                f'<ul id="{_UI_EXAMPLES_LIST_ID}">\n'
                '<li><span data-name-template-context="module_member">{unknown_variable}</span></li>\n'
                "</ul>\n",
                encoding="utf-8",
            )

            with (
                patch.object(importlib.import_module(__name__), "_PROJECT_ROOT", root),
                self.assertRaisesRegex(AssertionError, "unknown_variable"),
            ):
                for documented in _templates_in_rule_list_examples():
                    available = {variable.name for variable in variables_for_context(documented.context)}
                    referenced = set(_TEMPLATE_VARIABLE.findall(documented.template))
                    self.assertLessEqual(referenced, available)

    def test_rule_list_help_rejects_an_unmarked_template_on_any_element(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / _UI_LIST
            path.parent.mkdir(parents=True)
            path.write_text(
                f'<ul id="{_UI_EXAMPLES_LIST_ID}"><li><span>{{unknown_variable}}</span></li></ul>\n',
                encoding="utf-8",
            )

            with (
                patch.object(importlib.import_module(__name__), "_PROJECT_ROOT", root),
                self.assertRaisesRegex(AssertionError, rf"{re.escape(_UI_LIST)}:1 .*not marked"),
            ):
                tuple(_templates_in_rule_list_examples())

    def test_rule_list_help_rejects_an_unmarked_arithmetic_template_example(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / _UI_LIST
            path.parent.mkdir(parents=True)
            path.write_text(
                f'<ul id="{_UI_EXAMPLES_LIST_ID}"><li>Half-slot naming: <code>eth{{slot_num // 2}}</code></li></ul>\n',
                encoding="utf-8",
            )

            with (
                patch.object(importlib.import_module(__name__), "_PROJECT_ROOT", root),
                self.assertRaisesRegex(AssertionError, rf"{re.escape(_UI_LIST)}:1 .*not marked"),
            ):
                tuple(_templates_in_rule_list_examples())

    def test_rule_list_help_ignores_code_outside_the_examples_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / _UI_LIST
            path.parent.mkdir(parents=True)
            path.write_text(
                "<p><code>{not_an_example}</code></p>\n"
                '<ul id="interface-name-rule-examples">\n'
                '<li><code data-name-template-context="module_member">eth{bay_position_num}</code></li>\n'
                "</ul>\n",
                encoding="utf-8",
            )

            with patch.object(importlib.import_module(__name__), "_PROJECT_ROOT", root):
                self.assertEqual(
                    tuple(_templates_in_rule_list_examples()),
                    (
                        DocumentedTemplate(
                            _UI_LIST,
                            "eth{bay_position_num}",
                            NamingContext.MODULE_MEMBER,
                        ),
                    ),
                )

    def test_end_to_end_rule_payloads_are_read_from_python_syntax(self):
        self.assertEqual(
            tuple(_templates_in_e2e_script()),
            (
                DocumentedTemplate(
                    ".devcontainer/scripts/test-e2e.py",
                    "Gi{vc_position}/{bay_position_num}",
                    NamingContext.MODULE_MEMBER,
                ),
                DocumentedTemplate(
                    ".devcontainer/scripts/test-e2e.py",
                    "Gi{vc_position}/{port}",
                    NamingContext.DEVICE_INTERFACE,
                ),
            ),
        )

    def test_end_to_end_script_rejects_an_indirect_rule_payload(self):
        payloads = (
            "payload = {'name_template': '{unknown_variable}'}",
            "payload = dict(name_template='{unknown_variable}')",
            "payload = {}; payload['name_template'] = '{unknown_variable}'",
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / ".devcontainer" / "scripts" / "test-e2e.py"
                path.parent.mkdir(parents=True)
                path.write_text(f"import json\n{payload}\njson.dumps(payload)\n", encoding="utf-8")

                with (
                    patch.object(importlib.import_module(__name__), "_PROJECT_ROOT", root),
                    self.assertRaisesRegex(
                        AssertionError,
                        r"\.devcontainer/scripts/test-e2e\.py:2 .*outside a direct dumps dictionary",
                    ),
                ):
                    tuple(_templates_in_e2e_script())

    def test_end_to_end_script_rejects_nonliteral_payload_keys(self):
        payload_keys = (
            '"name_" + "template": "{unknown_variable}"',
            "**make_payload()",
            '**{"name_template": "{unknown_variable}"}',
        )
        for payload_key in payload_keys:
            with self.subTest(payload_key=payload_key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / ".devcontainer" / "scripts" / "test-e2e.py"
                path.parent.mkdir(parents=True)
                path.write_text(
                    "import json\n"
                    "def make_payload(): return {}\n"
                    "json.dumps({\n"
                    '    "name_template": "eth{bay_position_num}",\n'
                    f"    {payload_key},\n"
                    "})\n",
                    encoding="utf-8",
                )

                with (
                    patch.object(importlib.import_module(__name__), "_PROJECT_ROOT", root),
                    self.assertRaisesRegex(
                        TypeError,
                        r"\.devcontainer/scripts/test-e2e\.py:5 .*not a string literal",
                    ),
                ):
                    tuple(_templates_in_e2e_script())

    def test_every_documented_template_uses_variables_from_its_naming_context(self):
        for documented in _documented_templates():
            available = {variable.name for variable in variables_for_context(documented.context)}
            referenced = set(_TEMPLATE_VARIABLE.findall(documented.template))
            with self.subTest(source=documented.source, template=documented.template, context=documented.context):
                self.assertLessEqual(referenced, available)

    def test_every_documented_template_evaluates_in_its_naming_context(self):
        for documented in _documented_templates():
            with self.subTest(source=documented.source, template=documented.template, context=documented.context):
                evaluate_name_template(documented.template, _example_variables(documented.context))

    def test_generated_markers_leave_their_content_visible_to_the_markdown_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text(
                "<!-- BEGIN GENERATED TEMPLATE VARIABLE REFERENCE -->\n"
                "```yaml\nname_template: 'eth{bay_position_num}'\n```\n"
                "<!-- END GENERATED TEMPLATE VARIABLE REFERENCE -->\n",
                encoding="utf-8",
            )

            self.assertEqual(
                tuple(_templates_in_markdown(path, "guide.md")),
                (DocumentedTemplate("guide.md", "eth{bay_position_num}", NamingContext.MODULE_MEMBER),),
            )

    def test_a_commented_out_template_is_not_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text(
                "```yaml\nname_template: 'eth{bay_position_num}'\n```\n"
                "<!-- ```yaml\nname_template: '{retired}'\n``` -->\n",
                encoding="utf-8",
            )

            self.assertEqual(
                tuple(_templates_in_markdown(path, "guide.md")),
                (DocumentedTemplate("guide.md", "eth{bay_position_num}", NamingContext.MODULE_MEMBER),),
            )

    def test_a_blank_line_after_a_yaml_fence_stays_inside_the_template_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text("```yaml\n\nname_template: 'eth{bay_position_num}'\n```\n", encoding="utf-8")

            self.assertEqual(
                tuple(_templates_in_markdown(path, "guide.md")),
                (DocumentedTemplate("guide.md", "eth{bay_position_num}", NamingContext.MODULE_MEMBER),),
            )

    def test_a_template_key_outside_a_yaml_fence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text(
                "```yaml\nname_template: 'eth{bay_position_num}'\n```\n```\nname_template: '{unknown_variable}'\n```\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AssertionError,
                r"guide\.md:5 .*outside a fenced YAML block",
            ):
                tuple(_templates_in_markdown(path, "guide.md"))

    def test_a_flow_template_cannot_cancel_a_key_outside_a_yaml_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text(
                "```yaml\n- {name_template: 'eth{bay_position_num}'}\n```\n"
                "```\nname_template: '{unknown_variable}'\n```\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AssertionError, r"guide\.md:5 .*outside a fenced YAML block"):
                tuple(_templates_in_markdown(path, "guide.md"))

    def test_converter_offset_surfaces_match_the_shipped_rule(self):
        template = _shipped_converter_offset_template()
        yaml_anchor = f'name_template: "{template}"'
        restatements = (
            ("contrib/README.md", yaml_anchor),
            ("docs/examples.md", yaml_anchor),
            ("docs/template-variables.md", yaml_anchor),
            (_UI_LIST, f'<code data-name-template-context="module_member">{template}</code>'),
            ("netbox_interface_name_rules/models.py", f"'{template}'"),
        )

        for source, anchor in restatements:
            text = (_PROJECT_ROOT / source).read_text(encoding="utf-8")
            with self.subTest(source=source):
                self.assertEqual(
                    text.count(anchor),
                    1,
                    f"{source} must restate the shipped converter-offset template {template!r} exactly once",
                )

    def test_converter_offset_template_produces_the_documented_name(self):
        variables, expected_name = _documented_converter_offset_scenario()

        self.assertEqual(
            evaluate_name_template(_shipped_converter_offset_template(), variables),
            expected_name,
        )

    def test_examples_guide_matches_all_shipped_acx7024_module_rules(self):
        self.assertEqual(_acx7024_guide_rules(), _acx7024_shipped_rules())
