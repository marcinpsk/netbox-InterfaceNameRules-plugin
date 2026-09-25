# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Build name-template variables from NetBox rows."""

from .name_template import NamingContext, TemplateVariableCondition, TemplateVariableSource, variables_for_context


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


def numeric_suffix(value) -> str:
    """Return the number *value* ends with, as a decimal literal, or "0" when it has none.

    A device type may compose the parent into a bay position, so any position can arrive
    path-shaped, such as TenGigabitEthernet3/2/1. The result goes into arithmetic, so it is
    canonical: a zero-padded run such as "02" becomes "2", which Python rejects as a literal,
    and a non-ASCII digit run yields "0" rather than a value the evaluator cannot read.
    """
    digits = _extract_trailing_digits(str(value))
    if not digits.isascii():
        return "0"
    return str(int(digits)) if digits else "0"


def _resolve_bay_position(module_bay):
    """Return the raw and numeric positions for *module_bay*.

    A template expression such as ``{module}`` resolves from trailing digits in
    the bay name. The numeric position comes from ``numeric_suffix``, the one
    function every numeric variable is derived through, so a zero-padded
    position such as ``"02"`` reaches arithmetic as ``"2"``.
    """
    bay_position = module_bay.position or "0"
    if bay_position.startswith("{"):
        digits = _extract_trailing_digits(module_bay.name)
        bay_position = digits or "0"
    return bay_position, numeric_suffix(bay_position)


def _resolve_slot(module_bay, bay_position, parent_bay_position):
    """Return the slot value from the module-bay hierarchy.

    A nested bay takes the parent or grandparent position. A bay owned by an
    installed module takes that module's bay position. Other bays use the digits
    of their own position, as stored, because a slot is a position and not a
    number; ``slot_num`` is the counterpart arithmetic reads.
    """
    if module_bay.parent:
        parent_bay = module_bay.parent
        if parent_bay.parent and hasattr(parent_bay.parent, "installed_module"):
            return parent_bay.parent.position or parent_bay_position
        return parent_bay_position
    own_digits = _extract_trailing_digits(bay_position) or "0"
    if hasattr(module_bay, "module") and module_bay.module:
        owner_module = module_bay.module
        if hasattr(owner_module, "module_bay") and owner_module.module_bay:
            return owner_module.module_bay.position or own_digits
    return own_digits


def _filter_variables(context, source, values, vc_position):
    """Select catalogue values whose condition is satisfied."""
    return {
        variable.name: values[variable.name]
        for variable in variables_for_context(context, source=source)
        if variable.condition is None
        or (variable.condition == TemplateVariableCondition.VIRTUAL_CHASSIS_MEMBER and vc_position is not None)
    }


def build_bay_chain_variables(slot, bay_position, parent_bay_position, vc_position=None, *, bay_position_num=None):
    """Build the catalogue entries derived from resolved module-bay positions."""
    if bay_position_num is None:
        bay_position_num = numeric_suffix(bay_position)
    values = {
        "slot": slot,
        "slot_num": numeric_suffix(slot),
        "bay_position": bay_position,
        "bay_position_num": bay_position_num,
        "parent_bay_position": parent_bay_position,
        "parent_bay_position_num": numeric_suffix(parent_bay_position),
        "sfp_slot": bay_position_num,
        "vc_position": str(vc_position),
    }
    return _filter_variables(NamingContext.MODULE_MEMBER, TemplateVariableSource.MODULE_BAY_CHAIN, values, vc_position)


def build_variables(module_bay, device=None):
    """Build template variables from a module bay and optional device.

    The result includes slot, bay position, parent bay position, SFP slot, and a
    numeric counterpart for every position. A device type may compose the parent
    into a bay position, giving a path-shaped value such as
    ``TenGigabitEthernet3/2/1``, so arithmetic templates take the ``_num`` form.
    A virtual-chassis position is included only for a member device that has a
    position.

    A template that uses ``{vc_position}`` for a non-member device fails during
    evaluation because the variable is intentionally absent. Position zero is
    retained because it is a valid virtual-chassis position.
    """
    bay_position, bay_position_num = _resolve_bay_position(module_bay)

    parent_bay_position = "0"
    if module_bay.parent:
        parent_bay_position = module_bay.parent.position or "0"

    slot = _resolve_slot(module_bay, bay_position, parent_bay_position)

    vc_position = None
    if (
        device is not None
        and getattr(device, "virtual_chassis_id", None) is not None
        and device.vc_position is not None
    ):
        vc_position = device.vc_position
    return build_bay_chain_variables(
        slot,
        bay_position,
        parent_bay_position,
        vc_position=vc_position,
        bay_position_num=bay_position_num,
    )


def build_device_interface_variables(interface_name, vc_position):
    """Build device-interface variables from the current name and VC position."""
    values = {"base": interface_name, "port": interface_name.rsplit("/", 1)[-1], "vc_position": str(vc_position)}
    return _filter_variables(NamingContext.DEVICE_INTERFACE, None, values, vc_position)
