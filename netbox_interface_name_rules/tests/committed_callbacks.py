# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Run the committed callbacks of the rename triggers that a test captured."""

from netbox_interface_name_rules.rename_triggers import DeviceTrigger, ModuleTrigger, PlanRunner


def run_the_reapply(callbacks):
    """Run the rename-trigger callbacks among *callbacks* in order: each trigger, then the plan runners.

    NetBox registers committed callbacks of its own, such as the search cache flush. A test that counts
    the queries of the reapply, or that makes some reads fail, must not run them.
    """
    for callback in callbacks:
        if isinstance(callback, (ModuleTrigger, DeviceTrigger, PlanRunner)):
            callback()
