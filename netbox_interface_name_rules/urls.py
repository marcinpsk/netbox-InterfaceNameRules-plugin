# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.urls import path

from . import views

app_name = "netbox_interface_name_rules"

urlpatterns = [
    # List / CRUD
    path("rules/", views.InterfaceNameRuleListView.as_view(), name="interfacenamerule_list"),
    path("rules/add/", views.InterfaceNameRuleCreateView.as_view(), name="interfacenamerule_add"),
    path("rules/import/", views.InterfaceNameRuleBulkImportView.as_view(), name="interfacenamerule_bulk_import"),
    path("rules/bulk_delete/", views.InterfaceNameRuleBulkDeleteView.as_view(), name="interfacenamerule_bulk_delete"),
    # Rule tester (Build Rule)
    path("rules/test/", views.RuleTestView.as_view(), name="interfacenamerule_test"),
    # Apply rules to existing interfaces
    path("rules/apply/", views.RuleApplyListView.as_view(), name="interfacenamerule_apply"),
    # Per-rule parameterised routes
    path("rules/<int:pk>/", views.InterfaceNameRuleView.as_view(), name="interfacenamerule_detail"),
    path(
        "rules/<int:pk>/duplicate/", views.InterfaceNameRuleDuplicateView.as_view(), name="interfacenamerule_duplicate"
    ),
    path("rules/<int:pk>/edit/", views.InterfaceNameRuleEditView.as_view(), name="interfacenamerule_edit"),
    path("rules/<int:pk>/delete/", views.InterfaceNameRuleDeleteView.as_view(), name="interfacenamerule_delete"),
    path(
        "rules/<int:pk>/changelog/",
        views.InterfaceNameRuleChangeLogView.as_view(),
        name="interfacenamerule_changelog",
    ),
    path("rules/<int:pk>/apply/", views.RuleApplyDetailView.as_view(), name="interfacenamerule_apply_detail"),
    path("rules/<int:pk>/applicable/", views.RuleApplicableView.as_view(), name="interfacenamerule_applicable"),
    path("rules/<int:pk>/toggle/", views.RuleToggleView.as_view(), name="interfacenamerule_toggle"),
]
