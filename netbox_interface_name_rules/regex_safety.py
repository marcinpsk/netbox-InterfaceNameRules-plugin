# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Compile module-type patterns after checking their backtracking behavior."""

import re

# Python exposes no public regex AST, and re._parser is the only way to see a nested quantifier.
from re import _constants as _re_constants
from re import _parser as _re_parser

from django.core.exceptions import ValidationError

_BACKTRACKING_REPEATS = frozenset({_re_constants.MAX_REPEAT, _re_constants.MIN_REPEAT})
_MAX_AMBIGUOUS_REPEAT = 4


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


def _matches_ambiguously(node, flags):
    """Return True when *node* can match one string in more than one way."""
    for opcode, argument in node:
        if opcode in _BACKTRACKING_REPEATS:
            return True
        if opcode is _re_constants.BRANCH and _branch_matches_ambiguously(argument[1], flags):
            return True
        if any(
            _matches_ambiguously(child, child_flags)
            for child, child_flags in _child_subpatterns(opcode, argument, flags)
        ):
            return True
    return False


def _repeats_ambiguously(node, flags):
    """Return True when *node* repeats an ambiguous body enough to backtrack excessively."""
    for opcode, argument in node:
        if (
            opcode in _BACKTRACKING_REPEATS
            and argument[1] > _MAX_AMBIGUOUS_REPEAT
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
    if _repeats_ambiguously(parsed, parsed.state.flags):
        raise ValidationError(
            {"module_type_pattern": "Pattern contains nested quantifiers that can backtrack exponentially."}
        )
    return compiled
