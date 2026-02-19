from netbox.plugins import PluginMenuButton, PluginMenuItem

menu_items = (
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
)
