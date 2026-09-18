# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""NetBox configuration that loads this plugin alone.

A development container installs several plugins into one virtualenv. Loading them all makes a run
depend on trees this repository does not control, so the tests pin the plugin list to this package.
"""

from netbox import configuration as _configuration

for _name in dir(_configuration):
    if _name.isupper():
        globals()[_name] = getattr(_configuration, _name)

PLUGINS = ["netbox_interface_name_rules"]
PLUGINS_CONFIG = {
    "netbox_interface_name_rules": getattr(_configuration, "PLUGINS_CONFIG", {}).get("netbox_interface_name_rules", {}),
}
