"""Core renaming engine — rule lookup and interface rename logic.

This module is imported lazily by signals.py so that model imports happen
after Django is fully initialised.
"""

import ast
import re


def apply_interface_name_rules(module, module_bay):
    """Apply InterfaceNameRule rename after module installation.

    Looks up a matching rule for (module_type, parent_module_type, device_type)
    and renames interfaces created by NetBox's template instantiation.

    Returns:
        Number of interfaces renamed/created, or 0 if no rule matched.
    """
    from dcim.models import Interface

    from .models import InterfaceNameRule

    module_type = module.module_type

    # Determine parent module type (if installed inside another module)
    parent_module_type = None
    if module_bay.parent:
        parent_bay = module_bay.parent
        if hasattr(parent_bay, "installed_module") and parent_bay.installed_module:
            parent_module_type = parent_bay.installed_module.module_type

    # Look up rule: most specific first, then broader matches
    device_type = module.device.device_type if module.device else None
    rule = _find_matching_rule(module_type, parent_module_type, device_type)

    if not rule:
        return 0

    variables = _build_variables(module_bay)
    interfaces = Interface.objects.filter(module=module)
    renamed = 0

    for iface in interfaces:
        variables["base"] = iface.name
        renamed += _apply_rule_to_interface(rule, iface, variables, module)

    return renamed


def _find_matching_rule(module_type, parent_module_type, device_type):
    """Find the most specific InterfaceNameRule matching the context.

    Priority order:
      1. module_type + parent_module_type + device_type
      2. module_type + parent_module_type (any device)
      3. module_type + device_type (any parent)
      4. module_type only (universal)
    """
    from .models import InterfaceNameRule

    candidates = [
        (parent_module_type, device_type),
        (parent_module_type, None),
        (None, device_type),
        (None, None),
    ]

    for pmt, dt in candidates:
        if pmt is None and dt is None:
            # Most generic — both nullable
            rule = InterfaceNameRule.objects.filter(
                module_type=module_type,
                parent_module_type__isnull=True,
                device_type__isnull=True,
            ).first()
        elif pmt is None:
            rule = InterfaceNameRule.objects.filter(
                module_type=module_type,
                parent_module_type__isnull=True,
                device_type=dt,
            ).first()
        elif dt is None:
            rule = InterfaceNameRule.objects.filter(
                module_type=module_type,
                parent_module_type=pmt,
                device_type__isnull=True,
            ).first()
        else:
            rule = InterfaceNameRule.objects.filter(
                module_type=module_type,
                parent_module_type=pmt,
                device_type=dt,
            ).first()
        if rule:
            return rule

    return None


def _build_variables(module_bay):
    """Build template variable dict from module bay context."""
    bay_position = module_bay.position or "0"
    # If position is a template expression (e.g., {module}), extract from bay name
    if bay_position.startswith("{"):
        match = re.search(r"(\d+)$", module_bay.name)
        bay_position = match.group(1) if match else "0"
    # Extract numeric-only version for arithmetic (e.g., "swp1" → "1")
    bay_position_num_match = re.search(r"(\d+)$", bay_position)
    bay_position_num = bay_position_num_match.group(1) if bay_position_num_match else bay_position

    parent_bay_position = "0"
    sfp_slot = bay_position_num
    slot = bay_position_num

    if module_bay.parent:
        parent_bay = module_bay.parent
        parent_bay_position = parent_bay.position or "0"
        # slot is typically the top-level module position
        if parent_bay.parent and hasattr(parent_bay.parent, "installed_module"):
            grandparent = parent_bay.parent
            slot = grandparent.position or parent_bay_position
        else:
            slot = parent_bay_position
    elif hasattr(module_bay, "module") and module_bay.module:
        # Bay belongs to an installed module (not nested, but module-scoped)
        owner_module = module_bay.module
        if hasattr(owner_module, "module_bay") and owner_module.module_bay:
            slot = owner_module.module_bay.position or bay_position

    return {
        "slot": slot,
        "bay_position": bay_position,
        "bay_position_num": bay_position_num,
        "parent_bay_position": parent_bay_position,
        "sfp_slot": sfp_slot,
    }


def _apply_rule_to_interface(rule, iface, variables, module):
    """Apply a single rule to an interface, handling breakout channels.

    Returns number of interfaces renamed/created.
    """
    from dcim.models import Interface

    count = 0

    if rule.channel_count > 0:
        # Breakout: rename base and create additional channel interfaces
        for ch in range(rule.channel_count):
            variables["channel"] = str(rule.channel_start + ch)
            new_name = evaluate_name_template(rule.name_template, variables)
            if ch == 0:
                iface.name = new_name
                iface.full_clean()
                iface.save()
                count += 1
            else:
                breakout_iface = Interface(
                    device=module.device,
                    module=module,
                    name=new_name,
                    type=iface.type,
                    enabled=iface.enabled,
                )
                breakout_iface.full_clean()
                breakout_iface.save()
                count += 1
    else:
        # Simple rename (converter offset, platform naming, etc.)
        new_name = evaluate_name_template(rule.name_template, variables)
        if new_name != iface.name:
            iface.name = new_name
            iface.full_clean()
            iface.save()
            count += 1

    return count


def evaluate_name_template(template: str, variables: dict) -> str:
    """Evaluate a name template with variable substitution and arithmetic.

    Supports templates like:
        "GigabitEthernet{slot}/{8 + ({parent_bay_position} - 1) * 2 + {sfp_slot}}"

    Variables are substituted first, then any brace-enclosed expression
    containing arithmetic operators is safely evaluated via AST.
    """
    # First pass: substitute all simple variables
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))

    # Second pass: evaluate any remaining brace-enclosed arithmetic expressions
    def _eval_expr(match):
        expr = match.group(1).strip()
        # Only allow digits, arithmetic operators, parentheses, and whitespace
        if not re.match(r"^[\d\s\+\-\*\/\(\)]+$", expr):
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
                        ast.Div,
                        ast.FloorDiv,
                        ast.Mod,
                        ast.USub,
                        ast.UAdd,
                    ),
                ):
                    raise ValueError(f"Unsafe AST node in expression: {type(child).__name__}")
            return str(eval(compile(node, "<template>", "eval")))  # noqa: S307
        except (SyntaxError, TypeError) as e:
            raise ValueError(f"Invalid arithmetic expression '{expr}': {e}") from e

    result = re.sub(r"\{([^}]+)\}", _eval_expr, result)
    return result
