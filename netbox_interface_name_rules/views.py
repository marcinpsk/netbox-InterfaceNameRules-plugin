# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import dataclasses
import logging
import re

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from netbox.views import generic
from utilities.views import ConditionalLoginRequiredMixin, register_model_view

from .filters import InterfaceNameRuleFilterSet
from .forms import InterfaceNameRuleFilterForm, InterfaceNameRuleForm, InterfaceNameRuleImportForm, RuleTestForm
from .models import InterfaceNameRule
from .tables import InterfaceNameRuleTable

logger = logging.getLogger(__name__)

try:
    _plugins_config = getattr(settings, "PLUGINS_CONFIG", {})
    APPLY_BATCH_LIMIT = max(1, int(_plugins_config.get("netbox_interface_name_rules", {}).get("apply_batch_limit", 50)))
except (ValueError, TypeError):
    APPLY_BATCH_LIMIT = 50


@dataclasses.dataclass
class RulePreview:
    """Lightweight stand-in for InterfaceNameRule used in the test/preview view."""

    module_type_is_regex: bool
    module_type_pattern: str
    module_type: object
    parent_module_type: object
    device_type: object
    platform: object
    name_template: str
    channel_count: int
    channel_start: int


class InterfaceNameRuleListView(generic.ObjectListView):
    """List view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    table = InterfaceNameRuleTable
    filterset = InterfaceNameRuleFilterSet
    filterset_form = InterfaceNameRuleFilterForm
    template_name = "netbox_interface_name_rules/interfacenamerule_list.html"

    def get_extra_context(self, request):
        """Inject feature-detection flags into the list template context."""
        from .utils import supports_module_path

        return {
            "supports_module_path": supports_module_path(),
        }


class InterfaceNameRuleCreateView(generic.ObjectEditView):
    """Create view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    form = InterfaceNameRuleForm


@register_model_view(InterfaceNameRule, "bulk_import", path="import", detail=False)
class InterfaceNameRuleBulkImportView(generic.BulkImportView):
    """Bulk import view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    model_form = InterfaceNameRuleImportForm


class InterfaceNameRuleView(generic.ObjectView):
    """Detail view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()


class InterfaceNameRuleEditView(generic.ObjectEditView):
    """Edit view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    form = InterfaceNameRuleForm


class InterfaceNameRuleDeleteView(generic.ObjectDeleteView):
    """Delete view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()


class InterfaceNameRuleBulkDeleteView(generic.BulkDeleteView):
    """Bulk delete view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    table = InterfaceNameRuleTable


class InterfaceNameRuleChangeLogView(generic.ObjectChangeLogView):
    """Change-log view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()


class InterfaceNameRuleDuplicateView(ConditionalLoginRequiredMixin, View):
    """Redirect to the add view pre-populated with a clone of the given rule."""

    def get(self, request, pk):
        """Redirect to the add view pre-populated with fields cloned from rule pk."""
        from utilities.querydict import prepare_cloned_fields

        rule = get_object_or_404(InterfaceNameRule.objects.all(), pk=pk)
        params = prepare_cloned_fields(rule)
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_add")
        return redirect(f"{url}?{params.urlencode()}")


class RuleTestView(ConditionalLoginRequiredMixin, View):
    """Live-preview a name template with user-supplied variable values and optional DB lookup."""

    template_name = "netbox_interface_name_rules/rule_test.html"

    def get(self, request):
        """Render the test form, pre-populated from rule_id query param if given."""
        initial = {}
        loaded_rule = None
        rule_id = request.GET.get("rule_id")
        if rule_id:
            try:
                loaded_rule = InterfaceNameRule.objects.select_related(
                    "module_type", "parent_module_type", "device_type", "platform"
                ).get(pk=int(rule_id))
                initial = {
                    "name_template": loaded_rule.name_template,
                    "module_type_is_regex": loaded_rule.module_type_is_regex,
                    "module_type": loaded_rule.module_type,
                    "module_type_pattern": loaded_rule.module_type_pattern,
                    "parent_module_type": loaded_rule.parent_module_type,
                    "device_type": loaded_rule.device_type,
                    "platform": loaded_rule.platform,
                    "channel_count": loaded_rule.channel_count,
                    "channel_start": loaded_rule.channel_start,
                }
            except (InterfaceNameRule.DoesNotExist, ValueError):
                pass
        return render(request, self.template_name, {"form": RuleTestForm(initial=initial), "loaded_rule": loaded_rule})

    def post(self, request):
        """Evaluate the submitted template and return a preview or redirect to save."""
        from urllib.parse import urlencode

        from .engine import evaluate_name_template, find_interfaces_for_rule

        form = RuleTestForm(request.POST)
        preview_results = None
        db_preview = None
        db_total = 0
        error = None
        action = request.POST.get("action", "check")

        if form.is_valid():
            cd = form.cleaned_data
            name_template = cd["name_template"]
            channel_count = cd.get("channel_count") or 0
            channel_start = cd.get("channel_start") or 0
            module_type_is_regex = cd.get("module_type_is_regex", False)
            module_type = cd.get("module_type")
            module_type_pattern = cd.get("module_type_pattern", "")

            # ── Save as Rule action ─────────────────────────────────────────────
            if action == "save_rule":
                qs = InterfaceNameRule.objects.all()
                if module_type_is_regex:
                    qs = qs.filter(module_type_is_regex=True, module_type_pattern=module_type_pattern)
                else:
                    qs = qs.filter(module_type_is_regex=False, module_type=module_type)
                for field in ("parent_module_type", "device_type", "platform"):
                    val = cd.get(field)
                    if val:
                        qs = qs.filter(**{field: val})
                    else:
                        qs = qs.filter(**{f"{field}__isnull": True})
                existing = qs.first()
                if existing:
                    messages.info(
                        request,
                        f"A matching rule already exists (#{existing.pk}). Redirecting to edit it.",
                    )
                    return redirect(
                        reverse("plugins:netbox_interface_name_rules:interfacenamerule_edit", args=[existing.pk])
                    )
                params = {
                    "name_template": name_template,
                    "module_type_is_regex": "on" if module_type_is_regex else "",
                    "channel_count": channel_count,
                    "channel_start": channel_start,
                }
                if module_type_is_regex:
                    params["module_type_pattern"] = module_type_pattern
                elif module_type:
                    params["module_type"] = module_type.pk
                for field in ("parent_module_type", "device_type", "platform"):
                    val = cd.get(field)
                    if val:
                        params[field] = val.pk
                add_url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_add")
                return redirect(f"{add_url}?{urlencode(params)}")

            # ── Check action (default) ──────────────────────────────────────────
            variables = {
                "slot": cd.get("var_slot") or "1",
                "bay_position": cd.get("var_bay_position") or "1",
                "bay_position_num": cd.get("var_bay_position_num") or "1",
                "parent_bay_position": cd.get("var_parent_bay_position") or "1",
                "sfp_slot": cd.get("var_sfp_slot") or "1",
                "base": cd.get("var_base") or "Ethernet1",
            }

            try:
                if channel_count > 0:
                    preview_results = []
                    for ch in range(channel_count):
                        vars_copy = dict(variables)
                        vars_copy["channel"] = str(channel_start + ch)
                        preview_results.append(
                            {
                                "source": variables["base"],
                                "channel": str(channel_start + ch),
                                "result": evaluate_name_template(name_template, vars_copy),
                            }
                        )
                else:
                    preview_results = [
                        {
                            "source": variables["base"],
                            "channel": None,
                            "result": evaluate_name_template(name_template, variables),
                        }
                    ]
            except Exception as exc:
                logger.exception("Template evaluation error: %s", exc)
                error = type(exc).__name__

            if (module_type_is_regex and module_type_pattern) or (not module_type_is_regex and module_type):
                fake = RulePreview(
                    module_type_is_regex=module_type_is_regex,
                    module_type_pattern=module_type_pattern,
                    module_type=module_type,
                    parent_module_type=cd.get("parent_module_type"),
                    device_type=cd.get("device_type"),
                    platform=cd.get("platform"),
                    name_template=name_template,
                    channel_count=channel_count,
                    channel_start=channel_start,
                )
                try:
                    db_preview, db_total = find_interfaces_for_rule(fake, limit=100)
                except (re.error, ValueError) as exc:
                    error = f"Invalid module type regex: {exc}"
                    db_preview, db_total = [], 0
                except Exception as exc:
                    logger.exception("Unexpected error in find_interfaces_for_rule: %s", exc)
                    error = f"Unexpected error: {type(exc).__name__}"
                    db_preview, db_total = [], 0

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "preview_results": preview_results,
                "db_preview": db_preview,
                "db_total": db_total,
                "error": error,
            },
        )


class RuleApplyListView(ConditionalLoginRequiredMixin, View):
    """Display all rules with buttons to preview/apply each one."""

    template_name = "netbox_interface_name_rules/rule_apply.html"

    def get(self, request):
        """Render the list of all rules with apply/preview buttons."""
        rules = InterfaceNameRule.objects.select_related(
            "module_type", "parent_module_type", "device_type", "platform"
        ).order_by("pk")
        return render(request, self.template_name, {"rules": rules, "batch_limit": APPLY_BATCH_LIMIT})


class RuleApplicableView(ConditionalLoginRequiredMixin, View):
    """Return JSON indicating whether a rule would rename at least one interface.

    Called on demand from the Apply Rules page — NOT at page load — to avoid
    expensive full-scan queries blocking the initial render.
    """

    def get(self, request, pk):
        """Return JSON {"applicable": bool} for the rule identified by pk."""
        from .engine import has_applicable_interfaces

        rule = get_object_or_404(InterfaceNameRule, pk=pk)
        try:
            applicable = has_applicable_interfaces(rule)
        except Exception as exc:
            logger.exception("applicability scan failed for rule %s", pk)
            # Only expose the exception class name to avoid leaking internals
            # (SQL, file paths, etc.).  Full details are in the server log.
            return JsonResponse(
                {"applicable": None, "error": f"scan failed: {type(exc).__name__}"},
                status=500,
            )
        return JsonResponse({"applicable": applicable})


class RuleApplyDetailView(ConditionalLoginRequiredMixin, View):
    """Show a preview of changes for a specific rule and allow applying them."""

    template_name = "netbox_interface_name_rules/rule_apply_detail.html"

    def get(self, request, pk):
        """Render a preview of all interfaces that would be renamed by this rule."""
        from .engine import find_interfaces_for_rule

        rule = get_object_or_404(InterfaceNameRule, pk=pk)
        try:
            preview, total_checked = find_interfaces_for_rule(rule, limit=APPLY_BATCH_LIMIT)
        except (re.error, ValueError) as exc:
            logger.exception("Failed to compute preview for rule %s: %s", rule, exc)
            messages.error(request, f"Failed to compute preview: {exc}")
            preview, total_checked = [], 0
        except Exception as exc:
            logger.exception("Unexpected error computing preview for rule %s: %s", rule, exc)
            messages.error(request, f"Failed to compute preview: {type(exc).__name__}")
            preview, total_checked = [], 0
        return render(
            request,
            self.template_name,
            {
                "rule": rule,
                "preview": preview,
                "total_checked": total_checked,
                "batch_limit": APPLY_BATCH_LIMIT,
                "has_more": len(preview) >= APPLY_BATCH_LIMIT,
                "can_apply": request.user.has_perm("dcim.change_interface"),
            },
        )

    def post(self, request, pk):
        """Apply the rule (foreground batch or background job) and redirect back."""
        from .engine import apply_rule_to_existing

        if not request.user.has_perm("dcim.change_interface"):
            raise PermissionDenied

        rule = get_object_or_404(InterfaceNameRule, pk=pk)
        action = request.POST.get("action", "apply")

        if action == "background":
            from .jobs import ApplyRuleJob

            try:
                job = ApplyRuleJob.enqueue(
                    # instance is intentionally omitted: InterfaceNameRule does not
                    # inherit JobsMixin, so passing instance= would fail full_clean().
                    # The job is still named and findable in Core → Jobs.
                    name=f"Apply rule: {rule}",
                    user=request.user,
                    rule_id=rule.pk,
                )
                messages.success(request, f"Background job enqueued (job #{job.pk}). Check Core → Jobs for status.")
            except Exception as e:
                logger.exception("Failed to enqueue background job for rule %s: %s", rule, e)
                messages.error(request, f"Failed to enqueue background job: {type(e).__name__}")
        else:
            try:
                raw_ids = request.POST.getlist("interface_ids")
                interface_ids = [int(i) for i in raw_ids if i.isdigit()]
                if not interface_ids:
                    messages.warning(request, "No interfaces selected; nothing was applied.")
                else:
                    count = apply_rule_to_existing(rule, limit=APPLY_BATCH_LIMIT, interface_ids=interface_ids)
                    messages.success(request, f"Applied rule: {count} interface(s) renamed.")
            except Exception as e:
                logger.exception("Failed to apply rule %s: %s", rule, e)
                messages.error(request, f"Failed to apply rule {rule}: {type(e).__name__}")

        return redirect("plugins:netbox_interface_name_rules:interfacenamerule_apply_detail", pk=rule.pk)


class RuleToggleView(ConditionalLoginRequiredMixin, View):
    """POST /rules/<pk>/toggle/ — flip the enabled flag on a rule."""

    def post(self, request, pk):
        """Toggle the enabled flag on the rule and return JSON or redirect."""
        rule = get_object_or_404(InterfaceNameRule, pk=pk)
        if not request.user.has_perm("netbox_interface_name_rules.change_interfacenamerule"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"error": "Permission denied"}, status=403)
            raise PermissionDenied
        rule.enabled = not rule.enabled
        rule.save(update_fields=["enabled"])
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"enabled": rule.enabled, "pk": pk})
        state = "enabled" if rule.enabled else "disabled"
        messages.success(request, f"Rule '{rule}' {state}.")
        from django.utils.http import url_has_allowed_host_and_scheme

        referer = request.META.get("HTTP_REFERER", "")
        if referer and url_has_allowed_host_and_scheme(
            referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(referer)
        return redirect("plugins:netbox_interface_name_rules:interfacenamerule_list")
