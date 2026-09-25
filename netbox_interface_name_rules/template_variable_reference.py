# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Present the template-variable catalogue to operators and agents."""

from dataclasses import dataclass
from pathlib import Path

from .name_template import (
    TEMPLATE_VARIABLES,
    NamingContext,
    variables_for_context,
)


@dataclass(frozen=True, slots=True)
class TemplateVariableReferenceRow:
    """Describe one variable across naming contexts with equal operator guidance."""

    contexts: tuple[NamingContext, ...]
    name: str
    description: str
    example: str

    @property
    def token(self):
        """Return the variable as it appears in a name template."""
        return f"{{{self.name}}}"

    @property
    def context_values(self):
        """Return the naming contexts as a space-separated HTML data value."""
        return " ".join(context.value for context in self.contexts)

    @property
    def rule_type_label(self):
        """Name each rule kind and template field represented by the row."""
        fields_by_rule_kind = {}
        for context in self.contexts:
            rule_kind, field = CONTEXT_RULE_FIELDS[context]
            fields_by_rule_kind.setdefault(rule_kind, []).append(field)
        return "; ".join(f"{rule_kind}: {', '.join(fields)}" for rule_kind, fields in fields_by_rule_kind.items())


CONTEXT_LABELS = {
    NamingContext.MODULE_MEMBER: "Module member",
    NamingContext.MODULE_PARENT: "Module parent",
    NamingContext.DEVICE_INTERFACE: "Device interface",
}

CONTEXT_SUMMARIES = {
    NamingContext.MODULE_MEMBER: "Module rules use these variables in the Name Template field.",
    NamingContext.MODULE_PARENT: "Module rules use these variables in the Parent Name Template field.",
    NamingContext.DEVICE_INTERFACE: "Device-interface rules use these variables in the Name Template field.",
}

CONTEXT_RULE_FIELDS = {
    NamingContext.MODULE_MEMBER: ("Module", "Name Template"),
    NamingContext.MODULE_PARENT: ("Module", "Parent Name Template"),
    NamingContext.DEVICE_INTERFACE: ("Device interface", "Name Template"),
}

GENERATED_REGION_BEGIN = "<!-- BEGIN GENERATED TEMPLATE VARIABLE REFERENCE -->"
GENERATED_REGION_END = "<!-- END GENERATED TEMPLATE VARIABLE REFERENCE -->"


@dataclass(frozen=True, slots=True)
class GeneratedReferenceRegion:
    """Locate and format one generated template-variable reference."""

    path: Path
    heading_level: int
    title: str | None = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_REFERENCE_REGIONS = (
    GeneratedReferenceRegion(PROJECT_ROOT / "docs" / "template-variables.md", 3),
    GeneratedReferenceRegion(PROJECT_ROOT / "contrib" / "README.md", 3),
    GeneratedReferenceRegion(PROJECT_ROOT / ".github" / "copilot-instructions.md", 4, "Template variables"),
)


def naming_context_reference():
    """Return each naming context with its operator-facing summary."""
    return tuple({"label": CONTEXT_LABELS[context], "summary": CONTEXT_SUMMARIES[context]} for context in NamingContext)


def variable_reference_rows():
    """Group catalogue contexts that give one variable equal operator guidance."""
    rows = []
    for variable in TEMPLATE_VARIABLES:
        contexts_by_guidance = {}
        descriptions = dict(variable.descriptions)
        for context, _source in variable.providers:
            key = (descriptions[context], variable.example)
            contexts_by_guidance.setdefault(key, []).append(context)
        rows.extend(
            TemplateVariableReferenceRow(tuple(contexts), variable.name, description, example)
            for (description, example), contexts in contexts_by_guidance.items()
        )
    return tuple(rows)


def rule_tester_variable_rows(context):
    """Return the catalogue rows and descriptions for the preview's naming context."""
    return tuple(
        TemplateVariableReferenceRow(
            contexts=(context,),
            name=variable.name,
            description=dict(variable.descriptions)[context],
            example=variable.example,
        )
        for variable in variables_for_context(context)
    )


def render_markdown_reference(heading_level, title=None):
    """Render the catalogue as Markdown tables grouped by naming context."""
    lines = []
    if title:
        lines.extend((f"{'#' * (heading_level - 1)} {title}", ""))
    rows = variable_reference_rows()
    for context in NamingContext:
        lines.extend(
            (
                f"{'#' * heading_level} {CONTEXT_LABELS[context]} names",
                "",
                CONTEXT_SUMMARIES[context],
                "",
                "| Variable | Description | Example |",
                "|----------|-------------|---------|",
            )
        )
        lines.extend(
            f"| `{row.token}` | {row.description} | `{row.example}` |" for row in rows if context in row.contexts
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def regenerated_document(region):
    """Return one document with its generated region refreshed."""
    text = region.path.read_text(encoding="utf-8")
    if text.count(GENERATED_REGION_BEGIN) != 1 or text.count(GENERATED_REGION_END) != 1:
        raise ValueError(f"{region.path} must contain exactly one generated template-variable region")
    before, remainder = text.split(GENERATED_REGION_BEGIN, 1)
    _old_content, after = remainder.split(GENERATED_REGION_END, 1)
    generated = render_markdown_reference(region.heading_level, region.title)
    return f"{before}{GENERATED_REGION_BEGIN}\n{generated}\n{GENERATED_REGION_END}{after}"


def outdated_generated_regions():
    """Return generated-reference paths whose committed content is stale."""
    return tuple(
        str(region.path.relative_to(PROJECT_ROOT))
        for region in GENERATED_REFERENCE_REGIONS
        if region.path.read_text(encoding="utf-8") != regenerated_document(region)
    )


def write_generated_regions():
    """Regenerate every catalogue-backed documentation region and return changed paths."""
    missing_paths = tuple(region.path for region in GENERATED_REFERENCE_REGIONS if not region.path.exists())
    if missing_paths:
        paths = ", ".join(str(path) for path in missing_paths)
        raise FileNotFoundError(f"Reference regions exist only in a source checkout. Missing paths: {paths}")

    documents = tuple(
        (region, region.path.read_text(encoding="utf-8"), regenerated_document(region))
        for region in GENERATED_REFERENCE_REGIONS
    )
    changed = []
    for region, current, generated in documents:
        if current == generated:
            continue
        region.path.write_text(generated, encoding="utf-8")
        changed.append(str(region.path.relative_to(PROJECT_ROOT)))
    return tuple(changed)
