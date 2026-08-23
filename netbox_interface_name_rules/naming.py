# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Build naming variables and evaluate interface-name templates."""

import ast
import re


def _extract_trailing_digits(value: str) -> str:
    r"""Return the trailing digit run of *value* without regex backtracking.

    This O(n) string scan avoids the polynomial backtracking risk from a
    trailing-digit regular expression on a long value that ends in a non-digit.

    Returns an empty string when *value* has no trailing digits.
    """
    index = len(value)
    while index > 0 and value[index - 1].isdigit():
        index -= 1
    return value[index:]


def _resolve_bay_position(module_bay):
    """Return the raw and numeric positions for *module_bay*.

    A template expression such as ``{module}`` resolves from trailing digits in
    the bay name. A missing numeric suffix resolves to zero.
    """
    bay_position = module_bay.position or "0"
    if bay_position.startswith("{"):
        digits = _extract_trailing_digits(module_bay.name)
        bay_position = digits if digits else "0"
    digits = _extract_trailing_digits(bay_position)
    bay_position_num = digits if digits else "0"
    return bay_position, bay_position_num


def _resolve_slot(module_bay, bay_position_num, parent_bay_position):
    """Return the slot value from the module-bay hierarchy.

    A nested bay takes the parent or grandparent position. A bay owned by an
    installed module takes that module's bay position. Other bays use their
    numeric position.
    """
    if module_bay.parent:
        parent_bay = module_bay.parent
        if parent_bay.parent and hasattr(parent_bay.parent, "installed_module"):
            return parent_bay.parent.position or parent_bay_position
        return parent_bay_position
    if hasattr(module_bay, "module") and module_bay.module:
        owner_module = module_bay.module
        if hasattr(owner_module, "module_bay") and owner_module.module_bay:
            return owner_module.module_bay.position or bay_position_num
    return bay_position_num


def build_variables(module_bay, device=None):
    """Build template variables from a module bay and optional device.

    The result includes slot, bay position, numeric bay position, parent bay
    position, and SFP slot. A virtual-chassis position is included only for a
    member device that has a position.

    A template that uses ``{vc_position}`` for a non-member device fails during
    evaluation because the variable is intentionally absent. Position zero is
    retained because it is a valid virtual-chassis position.
    """
    bay_position, bay_position_num = _resolve_bay_position(module_bay)

    parent_bay_position = "0"
    if module_bay.parent:
        parent_bay_position = module_bay.parent.position or "0"

    slot = _resolve_slot(module_bay, bay_position_num, parent_bay_position)

    result = {
        "slot": slot,
        "bay_position": bay_position,
        "bay_position_num": bay_position_num,
        "parent_bay_position": parent_bay_position,
        "sfp_slot": bay_position_num,
    }
    if (
        device is not None
        and getattr(device, "virtual_chassis_id", None) is not None
        and device.vc_position is not None
    ):
        result["vc_position"] = str(device.vc_position)
    return result


def evaluate_name_template(template: str, variables: dict) -> str:
    """Evaluate a name template with variable substitution and safe arithmetic.

    Variables are substituted before remaining brace-enclosed arithmetic is
    evaluated. True division is not allowed. Arithmetic results are converted
    to integers so interface names contain whole numbers.

    For example, ``GigabitEthernet{slot}/{8 + {sfp_slot}}`` substitutes the
    variables before evaluating the arithmetic expression.
    """
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))

    def _eval_expr(match):
        expr = match.group(1).strip()
        if not re.match(r"^(?!.*(?<!/)/(?!/))[\d\s\+\-\*\(\/\)]+$", expr):
            raise ValueError(f"Unsafe expression in name template: {expr}")
        try:
            node = ast.parse(expr, mode="eval")
            for child in ast.walk(node):
                if not isinstance(
                    child,
                    (
                        ast.Expression,
                        ast.BinOp,
                        ast.UnaryOp,
                        ast.Constant,
                        ast.Add,
                        ast.Sub,
                        ast.Mult,
                        ast.FloorDiv,
                        ast.USub,
                        ast.UAdd,
                    ),
                ):
                    raise ValueError(f"Unsafe AST node in expression: {type(child).__name__}")
            return str(int(eval(compile(node, "<template>", "eval"))))  # noqa: S307
        except (SyntaxError, TypeError, ZeroDivisionError) as exc:
            raise ValueError(f"Invalid arithmetic expression '{expr}': {exc}") from exc

    return re.sub(r"\{([^}]+)\}", _eval_expr, result)
