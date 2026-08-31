# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Compile stored rule patterns with a bounded regular-expression engine."""

import re2
from django.core.exceptions import ValidationError

_OPTIONS = re2.Options()
_OPTIONS.log_errors = False


def _error_detail(exc):
    """Return RE2's parser error as text without its bytes representation."""
    detail = exc.args[0] if exc.args else str(exc)
    return detail.decode(errors="replace") if isinstance(detail, bytes) else str(detail)


def compile_module_type_pattern(pattern):
    """Compile a stored rule pattern with RE2, or raise a field validation error."""
    try:
        return re2.compile(pattern, options=_OPTIONS)
    except re2.error as exc:
        raise ValidationError({"module_type_pattern": f"Invalid RE2 pattern: {_error_detail(exc)}"}) from exc
