# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Compile module-type patterns after checking their backtracking behavior."""

import re
from dataclasses import dataclass

# Python exposes no public regex AST, and re._parser is the only way to see a nested quantifier.
from re import _constants as _re_constants
from re import _parser as _re_parser

from django.core.exceptions import ValidationError

_BACKTRACKING_REPEATS = frozenset({_re_constants.MAX_REPEAT, _re_constants.MIN_REPEAT})
_MAX_AMBIGUOUS_RUN = 4
_ZERO_WIDTH_OPCODES = frozenset({_re_constants.ASSERT, _re_constants.ASSERT_NOT, _re_constants.AT})
_SEQUENCE_BARRIER = object()


@dataclass(frozen=True)
class _AmbiguousToken:
    """Store the character sets at one ambiguous expression's edges."""

    leading: frozenset | None
    trailing: frozenset | None


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


def _operation_endpoint_literals(opcode, argument, flags, trailing):
    """Return one operation's possible endpoint literals and empty-match state."""
    if opcode in _ZERO_WIDTH_OPCODES:
        return frozenset(), True
    if opcode is _re_constants.LITERAL:
        return frozenset({(argument, flags)}), False
    if opcode is _re_constants.IN:
        literals = frozenset((value, flags) for item_opcode, value in argument if item_opcode is _re_constants.LITERAL)
        return (literals, False) if len(literals) == len(argument) else (None, False)
    if opcode is _re_constants.SUBPATTERN:
        _, add_flags, delete_flags, child = argument
        return _endpoint_literals(child, _effective_flags(flags, add_flags, delete_flags), trailing)
    if opcode is _re_constants.BRANCH:
        literals = set()
        can_match_empty = False
        for branch in argument[1]:
            branch_literals, branch_empty = _endpoint_literals(branch, flags, trailing)
            if branch_literals is None:
                return None, False
            literals.update(branch_literals)
            can_match_empty = can_match_empty or branch_empty
        return frozenset(literals), can_match_empty
    if opcode in _BACKTRACKING_REPEATS:
        minimum, maximum, child = argument
        if maximum == 0:
            return frozenset(), True
        literals, child_empty = _endpoint_literals(child, flags, trailing)
        return literals, minimum == 0 or child_empty
    return None, False


def _endpoint_literals(node, flags, trailing=False):
    """Return known literals at one end of a parsed sequence."""
    literals = set()
    operations = reversed(node) if trailing else iter(node)
    for opcode, argument in operations:
        operation_literals, can_match_empty = _operation_endpoint_literals(opcode, argument, flags, trailing)
        if operation_literals is None:
            return None, False
        literals.update(operation_literals)
        if not can_match_empty:
            return frozenset(literals), False
    return frozenset(literals), True


def _leading_literals(node, flags):
    """Return known literal first characters with their effective flags."""
    return _endpoint_literals(node, flags)


def _trailing_literals(node, flags):
    """Return known literal final characters with their effective flags."""
    return _endpoint_literals(node, flags, trailing=True)


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


def _append_sequence_barrier(tokens, pending):
    """Append one barrier for pending consuming operations."""
    if pending:
        tokens.append(_SEQUENCE_BARRIER)
        pending.clear()


def _sequence_tokens(node, flags):
    """Flatten transparent groups and fixed repeats into ambiguity tokens."""
    tokens = []
    pending = []
    for opcode, argument in node:
        if opcode in _ZERO_WIDTH_OPCODES:
            continue
        if opcode is _re_constants.SUBPATTERN:
            _, add_flags, delete_flags, child = argument
            child_tokens = _sequence_tokens(child, _effective_flags(flags, add_flags, delete_flags))
            if child.getwidth() == (0, 0):
                continue
            _append_sequence_barrier(tokens, pending)
            tokens.extend(child_tokens)
            continue
        if opcode in _BACKTRACKING_REPEATS and argument[0] == argument[1]:
            child_tokens = _sequence_tokens(argument[2], flags)
            if not child_tokens:
                continue
            _append_sequence_barrier(tokens, pending)
            tokens.extend(child_tokens * min(argument[0], _MAX_AMBIGUOUS_RUN + 1))
            continue
        if opcode is _re_constants.BRANCH and _branch_matches_ambiguously(argument[1], flags):
            segment = [*pending, (opcode, argument)]
            leading, _ = _leading_literals(segment, flags)
            trailing, _ = _trailing_literals(segment, flags)
            tokens.append(_AmbiguousToken(leading, trailing))
            pending.clear()
            continue
        pending.append((opcode, argument))
    _append_sequence_barrier(tokens, pending)
    return tokens


def _boundaries_overlap(left, right):
    """Return whether two ambiguity tokens can share a character boundary."""
    return not left or not right or _literal_sets_overlap(left, right)


def _tokens_backtrack_exponentially(tokens):
    """Return True when ambiguity tokens form an excessive overlapping run."""
    run_length = 0
    previous_trailing = None
    for token in tokens:
        if token is _SEQUENCE_BARRIER:
            run_length = 0
            previous_trailing = None
            continue
        run_length = run_length + 1 if run_length and _boundaries_overlap(previous_trailing, token.leading) else 1
        if run_length > _MAX_AMBIGUOUS_RUN:
            return True
        previous_trailing = token.trailing
    return False


def _has_ambiguous_sequence(node, flags):
    """Return True when adjacent ambiguous expressions exceed the safe bound."""
    for opcode, argument in node:
        if any(
            _has_ambiguous_sequence(child, child_flags)
            for child, child_flags in _child_subpatterns(opcode, argument, flags)
        ):
            return True
    return _tokens_backtrack_exponentially(_sequence_tokens(node, flags))


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
