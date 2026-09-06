# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Write interface rows the way an actor outside this plugin would."""


def rename_out_of_band(interface, name):
    """Rename *interface* through a real model save and return it.

    This is what an operator's edit, an import or another plugin does to a row this plugin later
    finds changed. The plugin registers receivers on Module and Device only, never on Interface, so
    a plain save runs NetBox's own machinery and none of ours: it is already the out-of-band write.
    A queryset update would skip NetBox's machinery too, and would need a database that permits raw
    bulk writes to interface rows.
    """
    interface.name = name
    interface.save()
    return interface
