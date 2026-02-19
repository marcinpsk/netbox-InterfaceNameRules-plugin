# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label="Interface Name Rules",
    icon_class="mdi mdi-swap-horizontal",
    groups=(
        (
            "Rules",
            (
                PluginMenuItem(
                    link="plugins:netbox_interface_name_rules:interfacenamerule_list",
                    link_text="Interface Name Rules",
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_interface_name_rules:interfacenamerule_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                        PluginMenuButton(
                            link="plugins:netbox_interface_name_rules:interfacenamerule_bulk_import",
                            title="Import",
                            icon_class="mdi mdi-upload",
                        ),
                    ),
                ),
            ),
        ),
    ),
)
