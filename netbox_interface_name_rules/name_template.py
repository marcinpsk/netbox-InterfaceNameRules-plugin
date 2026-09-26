# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Define and evaluate the name-template language."""

import ast
import itertools
import operator
import re
from dataclasses import dataclass
from enum import StrEnum

from django.core.exceptions import ValidationError

from .choices import BreakoutModeChoices


class NamingContext(StrEnum):
    """Name the contexts in which a name template is evaluated."""

    MODULE_MEMBER = "module_member"
    MODULE_PARENT = "module_parent"
    DEVICE_INTERFACE = "device_interface"


class TemplateVariableSource(StrEnum):
    """Name how a template variable reaches the evaluator."""

    MODULE_BAY_CHAIN = "module_bay_chain"
    RENAME_CALLER = "rename_caller"


class TemplateVariableCondition(StrEnum):
    """Name conditions that control whether a template variable is available."""

    VIRTUAL_CHASSIS_MEMBER = "virtual_chassis_member"
    RULE_DECLARES_CHANNELS = "rule_declares_channels"


@dataclass(frozen=True, slots=True)
class TemplateVariable:
    """Describe one template variable and its naming-context metadata."""

    name: str
    providers: tuple[tuple[NamingContext, TemplateVariableSource], ...]
    descriptions: tuple[tuple[NamingContext, str], ...]
    example: str
    condition: TemplateVariableCondition | None = None
    refusals: tuple[tuple[NamingContext, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _EvaluatedField:
    """Locate one brace group the evaluator reads."""

    start: int
    end: int
    expression: str


@dataclass(frozen=True, slots=True)
class _ParsedBraceGroups:
    """Hold the legacy reference and evaluation views of one name template."""

    reference_fields: tuple[str, ...]
    evaluated_fields: tuple[_EvaluatedField, ...]
    balanced: bool


_MEMBER = NamingContext.MODULE_MEMBER
_PARENT = NamingContext.MODULE_PARENT
_DEVICE = NamingContext.DEVICE_INTERFACE
_BUILT = TemplateVariableSource.MODULE_BAY_CHAIN
_CALLER = TemplateVariableSource.RENAME_CALLER
_VC_POSITION_DESCRIPTION = "Virtual Chassis member position. Available only on a member device."

TEMPLATE_VARIABLES = (
    TemplateVariable(
        "slot",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT)),
        ((_MEMBER, "Top-level module bay position."), (_PARENT, "Top-level module bay position.")),
        "Slot 3",
    ),
    TemplateVariable(
        "slot_num",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT)),
        (
            (_MEMBER, "Numeric suffix of the top-level module bay position."),
            (_PARENT, "Numeric suffix of the top-level module bay position."),
        ),
        "3",
    ),
    TemplateVariable(
        "bay_position",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT)),
        (
            (_MEMBER, "Position of the bay that holds the module."),
            (_PARENT, "Position of the bay that holds the module."),
        ),
        "swp1",
    ),
    TemplateVariable(
        "bay_position_num",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT)),
        (
            (_MEMBER, "Numeric suffix of the module bay position."),
            (_PARENT, "Numeric suffix of the module bay position."),
        ),
        "1",
    ),
    TemplateVariable(
        "parent_bay_position",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT)),
        (
            (_MEMBER, "Position of the parent module's bay."),
            (_PARENT, "Position of the parent module's bay."),
        ),
        "TenGigabitEthernet3/2",
    ),
    TemplateVariable(
        "parent_bay_position_num",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT)),
        (
            (_MEMBER, "Numeric suffix of the parent module's bay position."),
            (_PARENT, "Numeric suffix of the parent module's bay position."),
        ),
        "2",
    ),
    TemplateVariable(
        "sfp_slot",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT)),
        (
            (_MEMBER, "Numeric sub-bay index within the parent module."),
            (_PARENT, "Numeric sub-bay index within the parent module."),
        ),
        "0",
    ),
    TemplateVariable(
        "base",
        ((_MEMBER, _CALLER), (_PARENT, _CALLER), (_DEVICE, _CALLER)),
        (
            (_MEMBER, "The raw template name of the interface the rule renames."),
            (_PARENT, "The raw template name of the interface the rule renames."),
            (_DEVICE, "Current interface name before the rule applies."),
        ),
        "et-0/0/1",
    ),
    TemplateVariable(
        "vc_position",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT), (_DEVICE, _CALLER)),
        (
            (_MEMBER, _VC_POSITION_DESCRIPTION),
            (_PARENT, _VC_POSITION_DESCRIPTION),
            (_DEVICE, _VC_POSITION_DESCRIPTION),
        ),
        "2",
        condition=TemplateVariableCondition.VIRTUAL_CHASSIS_MEMBER,
    ),
    TemplateVariable(
        "channel",
        ((_MEMBER, _CALLER),),
        ((_MEMBER, "Breakout channel number. Available when the rule declares channels."),),
        "0",
        condition=TemplateVariableCondition.RULE_DECLARES_CHANNELS,
        refusals=((_PARENT, "The parent interface has no channel number; remove {channel}."),),
    ),
    TemplateVariable(
        "port",
        ((_DEVICE, _CALLER),),
        (
            (
                _DEVICE,
                "Segment after the last slash in the current interface name. Uses the full name when no slash is present.",
            ),
        ),
        "1",
    ),
)


def variables_for_context(context: NamingContext, source: TemplateVariableSource | None = None):
    """Return the catalogue entries available in one naming context."""
    return tuple(
        variable
        for variable in TEMPLATE_VARIABLES
        if any(
            provider_context == context and (source is None or provider_source == source)
            for provider_context, provider_source in variable.providers
        )
    )


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FORMAT_FIELD_RE = re.compile(r"(.*?)\s*(?:![rsa]|:[^{}]*)")


def _identifiers(text):
    """Return each whole identifier in *text*; only str.isidentifier() knows every Unicode one."""
    runs = ("".join(chars) for part, chars in itertools.groupby(text, lambda char: f"_{char}".isidentifier()) if part)
    return [run for run in runs if run.isidentifier()]


def _reference_brace_fields(template):
    """Return fields from each closed group, starting at its last opening brace."""
    reference_fields = []
    reference_start = None
    for index, char in enumerate(template):
        if char == "{":
            reference_start = index
        elif char == "}" and reference_start is not None:
            reference_fields.append(template[reference_start + 1 : index])
            reference_start = None
    return tuple(reference_fields)


def _braces_balanced(template):
    """Return whether every closing brace matches an earlier opening brace and none stay open."""
    depth = 0
    for char in template:
        if char == "{":
            depth += 1
        elif char == "}":
            if not depth:
                return False
            depth -= 1
    return depth == 0


def _evaluated_brace_fields(template, literal_spans=()):
    """Return nonempty groups from their first opening brace to the next closing brace."""
    evaluated_fields = []
    evaluation_start = None
    spans = iter(literal_spans)
    span = next(spans, None)
    for index, char in enumerate(template):
        while span is not None and index >= span[1]:
            span = next(spans, None)
        if span is not None and index >= span[0]:
            continue
        if char == "{" and evaluation_start is None:
            evaluation_start = index
        elif char == "}" and evaluation_start is not None:
            if index > evaluation_start + 1:
                evaluated_fields.append(
                    _EvaluatedField(evaluation_start, index + 1, template[evaluation_start + 1 : index])
                )
            evaluation_start = None
    return tuple(evaluated_fields)


def _parse_brace_groups(template, literal_spans=()):
    """Return the reference, evaluation, and balance views of a template."""
    return _ParsedBraceGroups(
        _reference_brace_fields(template), _evaluated_brace_fields(template, literal_spans), _braces_balanced(template)
    )


def _parse_expression(expression):
    """Parse one brace-group expression."""
    return ast.parse(expression, mode="eval")


def _evaluate_arithmetic(node):
    """Evaluate one validated integer arithmetic syntax tree without executing code."""
    if isinstance(node, ast.Expression):
        return _evaluate_arithmetic(node.body)
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        return _BINARY_OPERATORS[type(node.op)](
            _evaluate_arithmetic(node.left),
            _evaluate_arithmetic(node.right),
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate_arithmetic(node.operand))
    raise ValueError(f"Unsafe AST node in expression: {type(node).__name__}")


def variable_token(name):
    """Return the exact text a name template substitutes for the variable *name*."""
    return f"{{{name}}}"


def references_variable(template, name):
    """Return whether evaluating *template* substitutes the variable *name*."""
    return variable_token(name) in template


def referenced_variables(template):
    """Return distinct variable names in order of first appearance."""
    names = {}
    for field in _parse_brace_groups(template).reference_fields:
        head = field.split("!", 1)[0].split(":", 1)[0].strip()
        named_head = re.split(r"[.\[]", head, maxsplit=1)[0]
        if named_head.isidentifier():
            names.setdefault(named_head, None)
        for source in (field.strip(), head):
            try:
                tree = _parse_expression(source)
            except (SyntaxError, ValueError):
                continue
            references = sorted(
                (node for node in ast.walk(tree) if isinstance(node, ast.Name)),
                key=lambda node: (node.lineno, node.col_offset),
            )
            for node in references:
                names.setdefault(node.id, None)
            break
        else:
            # A group that does not parse names every identifier in it, so the check fails closed.
            for name in _identifiers(head):
                names.setdefault(name, None)
    return tuple(names)


def _validate_breakout_topology(breakout_mode, channel_count, parent_name_template, applies_to_device_interfaces):
    """Check that the mode, channel count, and parent template describe one topology."""
    channelized = breakout_mode == BreakoutModeChoices.CHANNELIZED
    if applies_to_device_interfaces:
        if channelized:
            raise ValidationError({"breakout_mode": "Device-level interface rules cannot build a channelized family."})
        if parent_name_template:
            raise ValidationError(
                {"parent_name_template": "Parent name template is not available for device-level interface rules."}
            )
    if parent_name_template and not channelized:
        raise ValidationError({"parent_name_template": "Parent name template requires the channelized breakout mode."})
    if channelized and not channel_count:
        raise ValidationError({"channel_count": "A channelized rule must define at least one channel."})


_CONTEXT_LABELS = {
    NamingContext.DEVICE_INTERFACE: "device-level rule's name template",
    NamingContext.MODULE_MEMBER: "module rule's name template",
    NamingContext.MODULE_PARENT: "module rule's parent name template",
}
_REFUSALS = {
    (context, variable.name): message for variable in TEMPLATE_VARIABLES for context, message in variable.refusals
}


def _template_errors(template, context, channel_count):
    """Return brace and context errors for one template."""
    if not _parse_brace_groups(template).balanced:
        return ["Unbalanced braces — every '{' in the template needs a '}'."]
    available = {
        variable.name
        for variable in variables_for_context(context)
        if variable.condition != TemplateVariableCondition.RULE_DECLARES_CHANNELS or channel_count > 0
    }
    available_text = ", ".join(variable_token(name) for name in sorted(available))
    return [
        _REFUSALS.get((context, name))
        or f"{variable_token(name)} is not available in a {_CONTEXT_LABELS[context]}. Available: {available_text}."
        for name in referenced_variables(template)
        if name not in available
    ]


def validate_rule(*, breakout_mode, channel_count, name_template, parent_name_template, applies_to_device_interfaces):
    """Validate topology first, then collect both templates' brace and context errors."""
    _validate_breakout_topology(breakout_mode, channel_count, parent_name_template, applies_to_device_interfaces)
    context = NamingContext.DEVICE_INTERFACE if applies_to_device_interfaces else NamingContext.MODULE_MEMBER
    errors = {}
    for field, template, naming_context in (
        ("name_template", name_template, context),
        ("parent_name_template", parent_name_template, NamingContext.MODULE_PARENT),
    ):
        messages = _template_errors(template, naming_context, channel_count)
        if messages:
            errors[field] = messages
    if errors:
        raise ValidationError(errors)


def evaluate_name_template(template: str, variables: dict) -> str:
    """Evaluate a name template with variable substitution and safe arithmetic.

    A brace group holds either a documented variable name or an arithmetic expression over the
    substituted values. This is not ``str.format``: braces nest for arithmetic, and ``str.format``
    conversions (``!r``) and format specifications (``:>2``) are not part of the language.

    Substituted values are literal text, not template syntax.
    Variables are substituted before remaining brace-enclosed arithmetic is evaluated. True
    division is not allowed. Arithmetic results become integers so interface names contain whole
    numbers.

    For example, ``GigabitEthernet{slot_num}/{8 + {sfp_slot}}`` substitutes the variables before
    evaluating the arithmetic expression.
    """
    replacements = {variable_token(key): str(value) for key, value in variables.items()}
    literal_spans = []
    offset = 0

    def substitute(match):
        nonlocal offset
        value = replacements[match.group()]
        start = match.start() + offset
        literal_spans.append((start, start + len(value)))
        offset += len(value) - len(match.group())
        return value

    result = template
    if replacements:
        pattern = "|".join(re.escape(token) for token in sorted(replacements, key=lambda token: (-len(token), token)))
        result = re.sub(pattern, substitute, template)

    def evaluate_expression(field):
        expr = field.expression.strip()
        format_field = _FORMAT_FIELD_RE.fullmatch(expr)
        if format_field and format_field[1].isidentifier():
            raise ValueError(
                f"Name templates take a variable or an arithmetic expression, not str.format "
                f"conversions and format specifications: {{{expr}}}"
            )
        if not re.match(r"^(?!.*(?<!/)/(?!/))[\d\s\+\-\*\(\/\)]+$", expr):
            raise ValueError(f"Unsafe expression in name template: {expr}")
        try:
            node = _parse_expression(expr)
            return str(int(_evaluate_arithmetic(node)))
        except (SyntaxError, TypeError, ZeroDivisionError) as exc:
            raise ValueError(f"Invalid arithmetic expression '{expr}': {exc}") from exc

    parsed = _parse_brace_groups(result, literal_spans)
    parts = []
    end = 0
    for field in parsed.evaluated_fields:
        parts.extend((result[end : field.start], evaluate_expression(field)))
        end = field.end
    parts.append(result[end:])
    return "".join(parts)
