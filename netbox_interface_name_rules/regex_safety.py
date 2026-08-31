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
_CHARACTER_MODE_FLAGS = re.ASCII | re.LOCALE | re.UNICODE
_SEQUENCE_BARRIER = object()
_CATEGORY_PATTERNS = {
    _re_constants.CATEGORY_DIGIT: r"\d",
    _re_constants.CATEGORY_NOT_DIGIT: r"\D",
    _re_constants.CATEGORY_SPACE: r"\s",
    _re_constants.CATEGORY_NOT_SPACE: r"\S",
    _re_constants.CATEGORY_WORD: r"\w",
    _re_constants.CATEGORY_NOT_WORD: r"\W",
}
_CATEGORY_COMPLEMENTS = {
    _re_constants.CATEGORY_DIGIT: _re_constants.CATEGORY_NOT_DIGIT,
    _re_constants.CATEGORY_NOT_DIGIT: _re_constants.CATEGORY_DIGIT,
    _re_constants.CATEGORY_SPACE: _re_constants.CATEGORY_NOT_SPACE,
    _re_constants.CATEGORY_NOT_SPACE: _re_constants.CATEGORY_SPACE,
    _re_constants.CATEGORY_WORD: _re_constants.CATEGORY_NOT_WORD,
    _re_constants.CATEGORY_NOT_WORD: _re_constants.CATEGORY_WORD,
}
_POSITIVE_CATEGORIES = frozenset(
    {
        _re_constants.CATEGORY_DIGIT,
        _re_constants.CATEGORY_SPACE,
        _re_constants.CATEGORY_WORD,
    }
)


@dataclass(frozen=True)
class _CharacterExpression:
    """Describe one literal or category expression and its effective flags."""

    opcode: object
    value: object
    flags: int


@dataclass(frozen=True)
class _EndpointCharacters:
    """Describe a finite character set or its finite complement."""

    included: frozenset | None
    excluded: frozenset = frozenset()


@dataclass(frozen=True)
class _AmbiguousToken:
    """Store the character sets at one ambiguous expression's edges."""

    leading: _EndpointCharacters | None
    trailing: _EndpointCharacters | None
    can_match_empty: bool
    same_string_choices: bool


def _effective_flags(flags, add_flags, delete_flags):
    """Return the flags effective inside a scoped sub-pattern."""
    if add_flags & _CHARACTER_MODE_FLAGS:
        flags &= ~_CHARACTER_MODE_FLAGS
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


def _operation_endpoint_characters(opcode, argument, flags, trailing):
    """Return one operation's possible endpoint literals and empty-match state."""
    if opcode in _ZERO_WIDTH_OPCODES:
        return _EndpointCharacters(frozenset()), True
    if opcode is _re_constants.LITERAL:
        expression = _CharacterExpression(opcode, argument, flags)
        return _EndpointCharacters(frozenset({expression})), False
    if opcode is _re_constants.NOT_LITERAL:
        expression = _CharacterExpression(_re_constants.LITERAL, argument, flags)
        return _EndpointCharacters(None, frozenset({expression})), False
    if opcode is _re_constants.IN:
        negated = bool(argument and argument[0][0] is _re_constants.NEGATE)
        expressions = []
        for item_opcode, value in argument[1:] if negated else argument:
            if item_opcode is _re_constants.LITERAL or (
                item_opcode is _re_constants.CATEGORY and value in _CATEGORY_PATTERNS
            ):
                expressions.append(_CharacterExpression(item_opcode, value, flags))
                continue
            return None, False
        values = frozenset(expressions)
        return (_EndpointCharacters(None, values) if negated else _EndpointCharacters(values)), False
    if opcode is _re_constants.SUBPATTERN:
        _, add_flags, delete_flags, child = argument
        return _endpoint_characters(child, _effective_flags(flags, add_flags, delete_flags), trailing)
    if opcode is _re_constants.BRANCH:
        endpoints = []
        can_match_empty = False
        for branch in argument[1]:
            branch_endpoint, branch_empty = _endpoint_characters(branch, flags, trailing)
            if branch_endpoint is None:
                return None, False
            endpoints.append(branch_endpoint)
            can_match_empty = can_match_empty or branch_empty
        return _union_endpoints(endpoints), can_match_empty
    if opcode in _BACKTRACKING_REPEATS:
        minimum, maximum, child = argument
        if maximum == 0:
            return _EndpointCharacters(frozenset()), True
        endpoint, child_empty = _endpoint_characters(child, flags, trailing)
        return endpoint, minimum == 0 or child_empty
    if opcode is _re_constants.ATOMIC_GROUP:
        return _endpoint_characters(argument, flags, trailing)
    return None, False


def _union_endpoints(endpoints):
    """Return the union of endpoint character constraints when representable."""
    if all(endpoint.included is not None for endpoint in endpoints):
        return _EndpointCharacters(frozenset().union(*(endpoint.included for endpoint in endpoints)))
    if all(endpoint.included is None for endpoint in endpoints):
        excluded = set(endpoints[0].excluded)
        for endpoint in endpoints[1:]:
            excluded.intersection_update(endpoint.excluded)
        return _EndpointCharacters(None, frozenset(excluded))
    return None


def _endpoint_characters(node, flags, trailing=False):
    """Return known literals at one end of a parsed sequence."""
    endpoints = []
    operations = reversed(node) if trailing else iter(node)
    for opcode, argument in operations:
        endpoint, can_match_empty = _operation_endpoint_characters(opcode, argument, flags, trailing)
        if endpoint is None:
            return None, False
        endpoints.append(endpoint)
        if not can_match_empty:
            return _union_endpoints(endpoints), False
    return _union_endpoints(endpoints), True


def _annotated_endpoint_characters(operations, trailing=False):
    """Return endpoint literals for operations carrying their effective flags."""
    endpoints = []
    ordered = reversed(operations) if trailing else iter(operations)
    for opcode, argument, flags in ordered:
        endpoint, can_match_empty = _operation_endpoint_characters(opcode, argument, flags, trailing)
        if endpoint is None:
            return None, False
        endpoints.append(endpoint)
        if not can_match_empty:
            return _union_endpoints(endpoints), False
    return _union_endpoints(endpoints), True


def _leading_characters(node, flags):
    """Return known literal first characters with their effective flags."""
    return _endpoint_characters(node, flags)


def _literal_matches(literal, flags, candidate):
    """Return whether one literal expression accepts the candidate character."""
    return re.fullmatch(re.escape(chr(literal)), chr(candidate), flags=flags) is not None


def _expression_matches(expression, candidate):
    """Return whether one character expression accepts a concrete character."""
    if expression.opcode is _re_constants.LITERAL:
        return _literal_matches(expression.value, expression.flags, candidate)
    return re.fullmatch(_CATEGORY_PATTERNS[expression.value], chr(candidate), flags=expression.flags) is not None


def _positive_category_is_subset(value, flags, superset_value, superset_flags):
    """Return whether one positive category language is a subset of another."""
    same_family = value is superset_value
    digit_is_word = value is _re_constants.CATEGORY_DIGIT and superset_value is _re_constants.CATEGORY_WORD
    if not same_family and not digit_is_word:
        return False
    return bool(flags & re.ASCII) or not superset_flags & re.ASCII


def _category_expressions_overlap(left, right):
    """Return whether two category expressions accept a common character."""
    left_positive = left.value in _POSITIVE_CATEGORIES
    right_positive = right.value in _POSITIVE_CATEGORIES
    if left_positive and right_positive:
        return not (
            left.value is not right.value
            and (left.value is _re_constants.CATEGORY_SPACE or right.value is _re_constants.CATEGORY_SPACE)
        )
    if not left_positive and not right_positive:
        return True
    positive, negative = (left, right) if left_positive else (right, left)
    return not _positive_category_is_subset(
        positive.value,
        positive.flags,
        _CATEGORY_COMPLEMENTS[negative.value],
        negative.flags,
    )


def _expressions_overlap(left, right):
    """Return whether two character expressions accept a common character."""
    if left.opcode is _re_constants.LITERAL and right.opcode is _re_constants.LITERAL:
        return _expression_matches(left, right.value) or _expression_matches(right, left.value)
    if left.opcode is _re_constants.LITERAL:
        return _expression_matches(right, left.value)
    if right.opcode is _re_constants.LITERAL:
        return _expression_matches(left, right.value)
    return _category_expressions_overlap(left, right)


def _expression_sets_overlap(left, right):
    """Return whether two character-expression sets accept a common character."""
    return any(
        _expressions_overlap(left_expression, right_expression)
        for left_expression in left
        for right_expression in right
    )


def _literal_language_is_covered(literal, exclusion):
    """Return whether an exclusion covers a literal expression's full language."""
    if not literal.flags & re.IGNORECASE:
        return _expression_matches(exclusion, literal.value)
    if exclusion.opcode is not _re_constants.LITERAL or not exclusion.flags & re.IGNORECASE:
        return False
    language_flags = re.ASCII | re.LOCALE
    return (
        literal.flags & language_flags == exclusion.flags & language_flags
        and _expression_matches(literal, exclusion.value)
        and _expression_matches(exclusion, literal.value)
    )


def _expression_language_is_covered(expression, exclusion):
    """Return whether an exclusion covers one character expression."""
    if expression.opcode is _re_constants.LITERAL:
        return _literal_language_is_covered(expression, exclusion)
    complement = _CharacterExpression(
        _re_constants.CATEGORY,
        _CATEGORY_COMPLEMENTS[exclusion.value],
        exclusion.flags,
    )
    return not _category_expressions_overlap(expression, complement)


def _endpoint_accepts_expression(endpoint, expression):
    """Return whether an endpoint can accept an expression's full language."""
    if endpoint.included is not None:
        return _expression_sets_overlap(endpoint.included, frozenset({expression}))
    return not any(_expression_language_is_covered(expression, excluded) for excluded in endpoint.excluded)


def _endpoint_sets_overlap(left, right):
    """Return whether two endpoint character constraints can overlap."""
    if left is None or right is None:
        return True
    if left.included is not None:
        return any(_endpoint_accepts_expression(right, expression) for expression in left.included)
    if right.included is not None:
        return any(_endpoint_accepts_expression(left, expression) for expression in right.included)
    return True


def _branch_matches_ambiguously(branches, flags):
    """Return True unless every alternative starts with a distinct literal."""
    seen = []
    for branch in branches:
        endpoint, empty = _leading_characters(branch, flags)
        if endpoint is None or empty or any(_endpoint_sets_overlap(endpoint, previous) for previous in seen):
            return True
        seen.append(endpoint)
    return False


def _literal_sequence(node, flags):
    """Return fixed-width character constraints for one simple sequence."""
    sequence = []
    for opcode, argument in node:
        if opcode is _re_constants.LITERAL or opcode is _re_constants.NOT_LITERAL or opcode is _re_constants.IN:
            endpoint, _ = _operation_endpoint_characters(opcode, argument, flags, False)
            if endpoint is None:
                return None
            sequence.append(endpoint)
            continue
        if opcode is _re_constants.SUBPATTERN:
            _, add_flags, delete_flags, child = argument
            child_sequence = _literal_sequence(child, _effective_flags(flags, add_flags, delete_flags))
        elif opcode in _BACKTRACKING_REPEATS and argument[0] == argument[1] <= _MAX_AMBIGUOUS_RUN + 1:
            child_sequence = _literal_sequence(argument[2], flags)
            child_sequence = None if child_sequence is None else child_sequence * argument[0]
        else:
            return None
        if child_sequence is None:
            return None
        sequence.extend(child_sequence)
    return sequence


def _branch_matches_same_string(branches, flags):
    """Return whether two alternatives can consume the same string."""
    sequences = []
    for branch in branches:
        sequence = _literal_sequence(branch, flags)
        if sequence is None:
            return True
        if any(
            len(sequence) == len(previous)
            and all(_endpoint_sets_overlap(left, right) for left, right in zip(sequence, previous, strict=True))
            for previous in sequences
        ):
            return True
        sequences.append(sequence)
    return False


def _matches_same_string_ambiguously(node, flags):
    """Return whether one sequence has distinct paths for the same string."""
    for opcode, argument in node:
        if opcode is _re_constants.BRANCH and _branch_matches_same_string(argument[1], flags):
            return True
        if any(
            _matches_same_string_ambiguously(child, child_flags)
            for child, child_flags in _child_subpatterns(opcode, argument, flags)
        ):
            return True
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


def _flatten_operations(node, flags):
    """Yield syntax-neutral operations with their effective flags."""
    for opcode, argument in node:
        if opcode is _re_constants.SUBPATTERN:
            _, add_flags, delete_flags, child = argument
            yield from _flatten_operations(child, _effective_flags(flags, add_flags, delete_flags))
        elif opcode in _BACKTRACKING_REPEATS and argument[0] == argument[1]:
            for _ in range(min(argument[0], _MAX_AMBIGUOUS_RUN + 1)):
                yield from _flatten_operations(argument[2], flags)
        else:
            yield opcode, argument, flags


def _transparent_operations(node, flags):
    """Yield operations after removing capture-only syntax."""
    for opcode, argument in node:
        if opcode is _re_constants.SUBPATTERN:
            _, add_flags, delete_flags, child = argument
            yield from _transparent_operations(child, _effective_flags(flags, add_flags, delete_flags))
        else:
            yield opcode, argument, flags


def _append_sequence_barrier(tokens, pending):
    """Append one barrier for pending consuming operations."""
    if pending:
        tokens.append(_SEQUENCE_BARRIER)
        pending.clear()


def _sequence_tokens(node, flags):
    """Flatten transparent groups and fixed repeats into ambiguity tokens."""
    tokens = []
    pending = []
    for opcode, argument, operation_flags in _flatten_operations(node, flags):
        if opcode in _ZERO_WIDTH_OPCODES:
            continue
        ambiguous_branch = opcode is _re_constants.BRANCH and _branch_matches_ambiguously(argument[1], operation_flags)
        variable_repeat = opcode in _BACKTRACKING_REPEATS and argument[0] != argument[1]
        if ambiguous_branch or variable_repeat:
            segment = [*pending, (opcode, argument, operation_flags)]
            leading, leading_empty = _annotated_endpoint_characters(segment)
            trailing, trailing_empty = _annotated_endpoint_characters(segment, trailing=True)
            same_string_choices = (
                _branch_matches_same_string(argument[1], operation_flags)
                if ambiguous_branch
                else _matches_same_string_ambiguously(argument[2], operation_flags)
            )
            tokens.append(_AmbiguousToken(leading, trailing, leading_empty and trailing_empty, same_string_choices))
            pending.clear()
            continue
        pending.append((opcode, argument, operation_flags))
    _append_sequence_barrier(tokens, pending)
    return tokens


def _boundaries_overlap(left, right):
    """Return whether two ambiguity tokens can share a character boundary."""
    return _endpoint_sets_overlap(left.trailing, right.leading)


def _tokens_backtrack_exponentially(tokens):
    """Return True when ambiguity tokens form an excessive overlapping run."""
    active_runs = []
    same_string_choices = 0
    for token in tokens:
        if token is _SEQUENCE_BARRIER:
            active_runs.clear()
            same_string_choices = 0
            continue
        run_length = max(
            (previous_length + 1 for previous, previous_length in active_runs if _boundaries_overlap(previous, token)),
            default=1,
        )
        same_string_choices += int(token.same_string_choices)
        if run_length > _MAX_AMBIGUOUS_RUN or same_string_choices > _MAX_AMBIGUOUS_RUN:
            return True
        if token.can_match_empty:
            active_runs.append((token, run_length))
        else:
            active_runs = [(token, run_length)]
    return False


def _has_ambiguous_sequence(node, flags):
    """Return True when adjacent ambiguous expressions exceed the safe bound."""
    for opcode, argument in node:
        if opcode is _re_constants.ATOMIC_GROUP:
            if _has_ambiguous_sequence(argument, flags):
                return True
            continue
        if any(
            _has_ambiguous_sequence(child, child_flags)
            for child, child_flags in _child_subpatterns(opcode, argument, flags)
        ):
            return True
    return _tokens_backtrack_exponentially(_sequence_tokens(node, flags))


def _repeats_ambiguously(node, flags, require_trailing_failure=False):
    """Return True when *node* repeats an ambiguous body enough to backtrack excessively."""
    operations = tuple(_transparent_operations(node, flags))
    for index, (opcode, argument, operation_flags) in enumerate(operations):
        minimum_can_fail = opcode in _BACKTRACKING_REPEATS and argument[0] > _MAX_AMBIGUOUS_RUN
        has_local_failure = not require_trailing_failure or index < len(operations) - 1 or minimum_can_fail
        if (
            opcode in _BACKTRACKING_REPEATS
            and argument[1] > _MAX_AMBIGUOUS_RUN
            and _matches_ambiguously(argument[2], operation_flags)
            and has_local_failure
        ):
            return True
        if opcode is _re_constants.ATOMIC_GROUP:
            if _repeats_ambiguously(argument, operation_flags, require_trailing_failure=True):
                return True
            continue
        if any(
            _repeats_ambiguously(child, child_flags)
            for child, child_flags in _child_subpatterns(opcode, argument, operation_flags)
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
