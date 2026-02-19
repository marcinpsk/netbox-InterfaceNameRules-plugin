from netbox.views import generic
from utilities.views import register_model_view

from .filters import InterfaceNameRuleFilterSet
from .forms import InterfaceNameRuleFilterForm, InterfaceNameRuleForm, InterfaceNameRuleImportForm
from .models import InterfaceNameRule
from .tables import InterfaceNameRuleTable


class InterfaceNameRuleListView(generic.ObjectListView):
    queryset = InterfaceNameRule.objects.all()
    table = InterfaceNameRuleTable
    filterset = InterfaceNameRuleFilterSet
    filterset_form = InterfaceNameRuleFilterForm
    template_name = "netbox_interface_name_rules/interfacenamerule_list.html"

    def get_extra_context(self, request):
        from .utils import MODULE_PATH_MIN_VERSION, supports_module_path

        return {
            "supports_module_path": supports_module_path(),
            "module_path_min_version": MODULE_PATH_MIN_VERSION,
        }


class InterfaceNameRuleCreateView(generic.ObjectEditView):
    queryset = InterfaceNameRule.objects.all()
    form = InterfaceNameRuleForm


@register_model_view(InterfaceNameRule, "bulk_import", path="import", detail=False)
class InterfaceNameRuleBulkImportView(generic.BulkImportView):
    queryset = InterfaceNameRule.objects.all()
    model_form = InterfaceNameRuleImportForm


class InterfaceNameRuleView(generic.ObjectView):
    queryset = InterfaceNameRule.objects.all()


class InterfaceNameRuleEditView(generic.ObjectEditView):
    queryset = InterfaceNameRule.objects.all()
    form = InterfaceNameRuleForm


class InterfaceNameRuleDeleteView(generic.ObjectDeleteView):
    queryset = InterfaceNameRule.objects.all()


class InterfaceNameRuleBulkDeleteView(generic.BulkDeleteView):
    queryset = InterfaceNameRule.objects.all()
    table = InterfaceNameRuleTable


class InterfaceNameRuleChangeLogView(generic.ObjectChangeLogView):
    queryset = InterfaceNameRule.objects.all()
