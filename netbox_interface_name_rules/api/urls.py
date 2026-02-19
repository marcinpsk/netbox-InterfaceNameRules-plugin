from netbox.api.routers import NetBoxRouter

from . import views

app_name = "netbox_interface_name_rules"

router = NetBoxRouter()
router.register("rules", views.InterfaceNameRuleViewSet)

urlpatterns = router.urls
