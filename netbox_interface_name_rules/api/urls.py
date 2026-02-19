# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_interface_name_rules"

router = NetBoxRouter()
router.register("rules", views.InterfaceNameRuleViewSet)

urlpatterns = router.urls
