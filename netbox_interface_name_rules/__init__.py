# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.plugins import PluginConfig

__version__ = "1.0.0"


class InterfaceNameRulesConfig(PluginConfig):
    name = "netbox_interface_name_rules"
    verbose_name = "Interface Name Rules"
    description = "Automatic interface renaming when modules are installed into bays."
    version = __version__
    base_url = "interface-name-rules"
    min_version = "4.2.0"
    required_settings = []
    default_settings = {}

    def ready(self):
        super().ready()
        from . import signals  # noqa: F401 — registers post_save handler


config = InterfaceNameRulesConfig
