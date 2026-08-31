# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Compile module-type patterns after checking their backtracking behavior."""

import re

# Python exposes no public regex AST, and re._parser is the only way to see a nested quantifier.
from re import _constants as _re_constants
from re import _parser as _re_parser

from django.core.exceptions import ValidationError

_BACKTRACKING_REPEATS = frozenset({_re_constants.MAX_REPEAT, _re_constants.MIN_REPEAT})
_MAX_AMBIGUOUS_RUN = 4
_ZERO_WIDTH_OPCODES = frozenset({_re_constants.ASSERT, _re_constants.ASSERT_NOT, _re_constants.AT})


def _effective_flags(flags, add_flags, delete_flags):
    """Return the flags effective inside a scoped sub-pattern."""
    return (flags | add_flags) & ~delete_flags


def _parsed_subpatterns(argument):
    """Yield every sub-pattern nested anywhere inside a parsed node's argument."""
    if isinstance(argument, _re_parser.SubPattern):
        yield argument
    elif isinstance(argument, (tuple, list)):
        for item in argument:
            yield from _parsed_subpatterns(item)


def _child_subpatterns(opcode, argument, flags):
    """Yield child sub-patterns with the flags effective inside each child."""
    if opcode is _re_constants.SUBPATTERN:
        _, add_flags, delete_flags, child = argument
        yield child, _effective_flags(flags, add_flags, delete_flags)
        return
    for child in _parsed_subpatterns(argument):
        yield child, flags


def _leading_literals(node, flags):
    """Return known literal first characters with their effective flags."""
    for opcode, argument in node:
        if opcode is _re_constants.AT:
            continue
        if opcode is _re_constants.LITERAL:
            return frozenset({(argument, flags)}), False
        if opcode is _re_constants.IN:
            literals = frozenset(
                (value, flags) for item_opcode, value in argument if item_opcode is _re_constants.LITERAL
            )
            if len(literals) != len(argument):
                return None, False
            return literals, False
        if opcode is _re_constants.SUBPATTERN:
            _, add_flags, delete_flags, child = argument
            return _leading_literals(child, _effective_flags(flags, add_flags, delete_flags))
        return None, False
    return frozenset(), True


def _literal_matches(literal, flags, candidate):
    """Return whether one literal expression accepts the candidate character."""
    return re.fullmatch(re.escape(chr(literal)), chr(candidate), flags=flags) is not None


def _literal_sets_overlap(left, right):
    """Return whether two leading-literal sets accept a common character."""
    return any(
        _literal_matches(left_literal, left_flags, right_literal)
        or _literal_matches(right_literal, right_flags, left_literal)
        for left_literal, left_flags in left
        for right_literal, right_flags in right
    )


def _branch_matches_ambiguously(branches, flags):
    """Return True unless every alternative starts with a distinct literal."""
    seen = []
    for branch in branches:
        literals, empty = _leading_literals(branch, flags)
        if literals is None or empty or any(_literal_sets_overlap(literals, previous) for previous in seen):
            return True
        seen.append(literals)
    return False


def _operation_matches_ambiguously(opcode, argument, flags, children):
    """Return whether one parsed operation can match ambiguously."""
    return (
        opcode in _BACKTRACKING_REPEATS
        or (opcode is _re_constants.BRANCH and _branch_matches_ambiguously(argument[1], flags))
        or any(_matches_ambiguously(child, child_flags) for child, child_flags in children)
    )


def _matches_ambiguously(node, flags):
    """Return True when *node* can match one string in more than one way."""
    for opcode, argument in node:
        children = tuple(_child_subpatterns(opcode, argument, flags))
        if _operation_matches_ambiguously(opcode, argument, flags, children):
            return True
    return False


def _operation_branches_ambiguously(opcode, argument, flags, children):
    """Return whether one parsed operation contains an ambiguous branch."""
    if opcode is _re_constants.BRANCH and _branch_matches_ambiguously(argument[1], flags):
        return True
    return any(_branches_ambiguously(child, child_flags) for child, child_flags in children)


def _branches_ambiguously(node, flags):
    """Return True when *node* contains an ambiguous branch."""
    for opcode, argument in node:
        children = tuple(_child_subpatterns(opcode, argument, flags))
        if _operation_branches_ambiguously(opcode, argument, flags, children):
            return True
    return False


def _ambiguous_component_width(opcode, argument, flags, children):
    """Return the number of adjacent ambiguous expressions in one operation."""
    if opcode is _re_constants.BRANCH and _branch_matches_ambiguously(argument[1], flags):
        return 1
    if opcode is _re_constants.SUBPATTERN:
        child, child_flags = children[0]
        return _grouped_ambiguous_width(child, child_flags) or int(_branches_ambiguously(child, child_flags))
    return 0


def _grouped_ambiguous_width(node, flags):
    """Return an ambiguity width when a group contains only ambiguous expressions."""
    width = 0
    for opcode, argument in node:
        if opcode in _ZERO_WIDTH_OPCODES:
            continue
        children = tuple(_child_subpatterns(opcode, argument, flags))
        component_width = _ambiguous_component_width(opcode, argument, flags, children)
        if not component_width:
            return 0
        width += component_width
    return width


def _has_ambiguous_sequence(node, flags):
    """Return True when adjacent ambiguous expressions exceed the safe bound."""
    run_length = 0
    for opcode, argument in node:
        children = tuple(_child_subpatterns(opcode, argument, flags))
        if any(_has_ambiguous_sequence(child, child_flags) for child, child_flags in children):
            return True
        if opcode in _ZERO_WIDTH_OPCODES:
            continue
        component_width = _ambiguous_component_width(opcode, argument, flags, children)
        if component_width:
            run_length += component_width
            if run_length > _MAX_AMBIGUOUS_RUN:
                return True
        else:
            run_length = 0
    return False


def _repeats_ambiguously(node, flags):
    """Return True when *node* repeats an ambiguous body enough to backtrack excessively."""
    for opcode, argument in node:
        if (
            opcode in _BACKTRACKING_REPEATS
            and argument[1] > _MAX_AMBIGUOUS_RUN
            and _matches_ambiguously(argument[2], flags)
        ):
            return True
        if any(
            _repeats_ambiguously(child, child_flags)
            for child, child_flags in _child_subpatterns(opcode, argument, flags)
        ):
            return True
    return False


def compile_module_type_pattern(pattern):
    """Compile a pattern, or raise ``ValidationError`` when it is invalid or unsafe."""
    try:
        compiled = re.compile(pattern)
        parsed = _re_parser.parse(pattern)
    except re.error as exc:
        raise ValidationError({"module_type_pattern": f"Invalid regex pattern: {exc}"}) from exc
    if _repeats_ambiguously(parsed, parsed.state.flags) or _has_ambiguous_sequence(parsed, parsed.state.flags):
        raise ValidationError({"module_type_pattern": "Pattern can backtrack exponentially."})
    return compiled
