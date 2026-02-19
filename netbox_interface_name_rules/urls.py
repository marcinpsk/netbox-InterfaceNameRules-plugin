from django.urls import path

from . import views

app_name = "netbox_interface_name_rules"

urlpatterns = [
    # List / CRUD
    path("rules/", views.InterfaceNameRuleListView.as_view(), name="interfacenamerule_list"),
    path("rules/add/", views.InterfaceNameRuleCreateView.as_view(), name="interfacenamerule_add"),
    path("rules/import/", views.InterfaceNameRuleBulkImportView.as_view(), name="interfacenamerule_bulk_import"),
    path("rules/bulk_delete/", views.InterfaceNameRuleBulkDeleteView.as_view(), name="interfacenamerule_bulk_delete"),
    path("rules/<int:pk>/", views.InterfaceNameRuleView.as_view(), name="interfacenamerule_detail"),
    path("rules/<int:pk>/edit/", views.InterfaceNameRuleEditView.as_view(), name="interfacenamerule_edit"),
    path("rules/<int:pk>/delete/", views.InterfaceNameRuleDeleteView.as_view(), name="interfacenamerule_delete"),
    path(
        "rules/<int:pk>/changelog/",
        views.InterfaceNameRuleChangeLogView.as_view(),
        name="interfacenamerule_changelog",
    ),
]
