# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Define and evaluate the name-template language."""

import ast
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
    """Describe one template variable and its providers."""

    name: str
    providers: tuple[tuple[NamingContext, TemplateVariableSource], ...]
    condition: TemplateVariableCondition | None = None


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

TEMPLATE_VARIABLES = (
    TemplateVariable("slot", ((_MEMBER, _BUILT), (_PARENT, _BUILT))),
    TemplateVariable("slot_num", ((_MEMBER, _BUILT), (_PARENT, _BUILT))),
    TemplateVariable("bay_position", ((_MEMBER, _BUILT), (_PARENT, _BUILT))),
    TemplateVariable("bay_position_num", ((_MEMBER, _BUILT), (_PARENT, _BUILT))),
    TemplateVariable("parent_bay_position", ((_MEMBER, _BUILT), (_PARENT, _BUILT))),
    TemplateVariable("parent_bay_position_num", ((_MEMBER, _BUILT), (_PARENT, _BUILT))),
    TemplateVariable("sfp_slot", ((_MEMBER, _BUILT), (_PARENT, _BUILT))),
    TemplateVariable("base", ((_MEMBER, _CALLER), (_PARENT, _CALLER), (_DEVICE, _CALLER))),
    TemplateVariable(
        "vc_position",
        ((_MEMBER, _BUILT), (_PARENT, _BUILT), (_DEVICE, _CALLER)),
        TemplateVariableCondition.VIRTUAL_CHASSIS_MEMBER,
    ),
    TemplateVariable(
        "channel",
        ((_MEMBER, _CALLER),),
        TemplateVariableCondition.RULE_DECLARES_CHANNELS,
    ),
    TemplateVariable("port", ((_DEVICE, _CALLER),)),
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
_FORMAT_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\s*(?:![rsa]|:[^{}]*)$")


def _parse_brace_groups(template):
    """Parse brace groups once while preserving both existing consumer views."""
    reference_fields = []
    evaluated_fields = []
    reference_start = None
    evaluation_start = None
    depth = 0
    unbalanced = False
    for index, char in enumerate(template):
        if char == "{":
            depth += 1
            reference_start = index
            if evaluation_start is None:
                evaluation_start = index
        elif char == "}":
            if depth:
                depth -= 1
            else:
                unbalanced = True
            if reference_start is not None:
                reference_fields.append(template[reference_start + 1 : index])
                reference_start = None
            if evaluation_start is not None:
                expression = template[evaluation_start + 1 : index]
                if expression:
                    evaluated_fields.append(_EvaluatedField(evaluation_start, index + 1, expression))
                evaluation_start = None
    return _ParsedBraceGroups(tuple(reference_fields), tuple(evaluated_fields), not unbalanced and depth == 0)


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


def _expression_names_channel(field):
    """Return whether one brace-group expression names the channel variable."""
    try:
        tree = _parse_expression(field.strip())
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(node, ast.Name) and node.id == "channel" for node in ast.walk(tree))


def references_channel(template):
    """Return whether a name template references the channel variable."""
    for field in _parse_brace_groups(template).reference_fields:
        name = field.split("!", 1)[0].split(":", 1)[0].strip()
        if name == "channel" or name.startswith(("channel.", "channel[")):
            return True
        if _expression_names_channel(field):
            return True
    return False


def _has_unbalanced_braces(template):
    """Return whether a name template has unpaired braces."""
    return not _parse_brace_groups(template).balanced


def validate_breakout_topology(
    breakout_mode,
    channel_count,
    parent_name_template,
    applies_to_device_interfaces=False,
):
    """Check that the mode, channel count, and parent template describe one topology."""
    channelized = breakout_mode == BreakoutModeChoices.CHANNELIZED
    if applies_to_device_interfaces:
        if channelized:
            raise ValidationError({"breakout_mode": "Device-level interface rules cannot build a channelized family."})
        if parent_name_template:
            raise ValidationError(
                {"parent_name_template": "Parent name template is not available for device-level interface rules."}
            )
    if parent_name_template:
        if not channelized:
            raise ValidationError(
                {"parent_name_template": "Parent name template requires the channelized breakout mode."}
            )
        if _has_unbalanced_braces(parent_name_template):
            raise ValidationError(
                {"parent_name_template": "Unbalanced braces — every '{' in the template needs a '}'."}
            )
        if references_channel(parent_name_template):
            raise ValidationError(
                {"parent_name_template": "The parent interface has no channel number; remove {channel}."}
            )
    if channelized and not channel_count:
        raise ValidationError({"channel_count": "A channelized rule must define at least one channel."})


def evaluate_name_template(template: str, variables: dict) -> str:
    """Evaluate a name template with variable substitution and safe arithmetic.

    A brace group holds either a documented variable name or an arithmetic expression over the
    substituted values. This is not ``str.format``: braces nest for arithmetic, and ``str.format``
    conversions (``!r``) and format specifications (``:>2``) are not part of the language.

    Variables are substituted before remaining brace-enclosed arithmetic is evaluated. True
    division is not allowed. Arithmetic results become integers so interface names contain whole
    numbers.

    For example, ``GigabitEthernet{slot_num}/{8 + {sfp_slot}}`` substitutes the variables before
    evaluating the arithmetic expression.
    """
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))

    def evaluate_expression(field):
        expr = field.expression.strip()
        if _FORMAT_FIELD_RE.fullmatch(expr):
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

    parsed = _parse_brace_groups(result)
    parts = []
    end = 0
    for field in parsed.evaluated_fields:
        parts.extend((result[end : field.start], evaluate_expression(field)))
        end = field.end
    parts.append(result[end:])
    return "".join(parts)
