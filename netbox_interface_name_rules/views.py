# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import dataclasses
import logging
import re

import yaml
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from netbox.views import generic
from netbox.views.generic.base import BaseMultiObjectView
from utilities.views import register_model_view

from .choices import BreakoutModeChoices
from .filters import InterfaceNameRuleFilterSet
from .forms import (
    InterfaceNameRuleBulkEditForm,
    InterfaceNameRuleFilterForm,
    InterfaceNameRuleForm,
    InterfaceNameRuleImportForm,
    RuleTestForm,
)
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
    parent_name_template: str = ""
    breakout_mode: str = BreakoutModeChoices.FLAT


try:
    from netbox.object_actions import AddObject, BulkDelete, BulkEdit, BulkExport, BulkImport

    class _YAMLOnlyExport(BulkExport):
        """Export action that only offers YAML (no CSV "Current View" option)."""

        template_name = "netbox_interface_name_rules/buttons/export_yaml_only.html"

    # No BulkRename: rules have no name field, and its button template renders nothing anyway.
    _LIST_VIEW_ACTIONS: tuple = (AddObject, BulkImport, _YAMLOnlyExport, BulkEdit, BulkDelete)
except ImportError:
    _LIST_VIEW_ACTIONS = None


class InterfaceNameRuleListView(generic.ObjectListView):
    """List view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    table = InterfaceNameRuleTable
    filterset = InterfaceNameRuleFilterSet
    filterset_form = InterfaceNameRuleFilterForm
    template_name = "netbox_interface_name_rules/interfacenamerule_list.html"
    if _LIST_VIEW_ACTIONS is not None:
        actions = _LIST_VIEW_ACTIONS

    def export_yaml(self):
        """Export all rules as a single YAML list (overrides NetBox's per-object concatenation)."""
        data = []
        for rule in self.queryset.order_by("pk").select_related(
            "module_type", "parent_module_type", "device_type", "platform"
        ):
            entry = {}
            for header, value in zip(InterfaceNameRule.csv_headers, rule.to_csv()):
                if (value != "" and value is not None) or header in {"name_template"}:
                    entry[header] = value
            data.append(entry)
        return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


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


class InterfaceNameRuleBulkEditView(generic.BulkEditView):
    """Bulk edit view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    filterset = InterfaceNameRuleFilterSet
    table = InterfaceNameRuleTable
    form = InterfaceNameRuleBulkEditForm


class InterfaceNameRuleBulkDeleteView(generic.BulkDeleteView):
    """Bulk delete view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()
    table = InterfaceNameRuleTable


class InterfaceNameRuleChangeLogView(generic.ObjectChangeLogView):
    """Change-log view for InterfaceNameRule."""

    queryset = InterfaceNameRule.objects.all()


class InterfaceNameRuleDuplicateView(generic.ObjectView):
    """Redirect to the add view pre-populated with a clone of the given rule."""

    queryset = InterfaceNameRule.objects.all()

    def get(self, request, **kwargs):
        """Redirect to the add view pre-populated with fields cloned from the given rule."""
        from utilities.querydict import prepare_cloned_fields

        rule = self.get_object(**kwargs)
        params = prepare_cloned_fields(rule)
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_add")
        return redirect(f"{url}?{params.urlencode()}")


class RuleTestView(BaseMultiObjectView):
    """Live-preview a name template with user-supplied variable values and optional DB lookup."""

    queryset = InterfaceNameRule.objects.all()
    template_name = "netbox_interface_name_rules/rule_test.html"

    def get_required_permission(self):
        """Return the permission required to access the rule test tool."""
        return "netbox_interface_name_rules.add_interfacenamerule"

    def get(self, request):
        """Render the test form, pre-populated from rule_id query param if given."""
        initial = {}
        loaded_rule = None
        rule_id = request.GET.get("rule_id")
        can_view = request.user.has_perm("netbox_interface_name_rules.view_interfacenamerule")
        if rule_id and not can_view:
            messages.warning(request, "You do not have permission to load an existing rule.")
        if rule_id and can_view:
            try:
                loaded_rule = (
                    InterfaceNameRule.objects.restrict(request.user, "view")
                    .select_related("module_type", "parent_module_type", "device_type", "platform")
                    .get(pk=int(rule_id))
                )
                initial = {
                    "name_template": loaded_rule.name_template,
                    "parent_name_template": loaded_rule.parent_name_template,
                    "breakout_mode": loaded_rule.breakout_mode,
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
        form = RuleTestForm(request.POST)
        preview_results = None
        db_preview = None
        db_total = 0
        error = None
        action = request.POST.get("action", "check")

        if form.is_valid():
            cd = form.cleaned_data
            if action == "save_rule":
                return self._handle_save_rule(request, cd)
            preview_results, error = self._evaluate_template_preview(cd)
            db_preview, db_total, db_error = self._fetch_db_preview(cd)
            if db_error and not error:
                error = db_error

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

    def _find_existing_rule(self, cd, user=None):
        """Return the first existing rule matching the form data, or None."""
        module_type_is_regex = cd.get("module_type_is_regex", False)
        qs = InterfaceNameRule.objects.restrict(user, "view") if user else InterfaceNameRule.objects.all()
        if module_type_is_regex:
            qs = qs.filter(module_type_is_regex=True, module_type_pattern=cd.get("module_type_pattern", ""))
        else:
            qs = qs.filter(module_type_is_regex=False, module_type=cd.get("module_type"))
        for field in ("parent_module_type", "device_type", "platform"):
            val = cd.get(field)
            if val:
                qs = qs.filter(**{field: val})
            else:
                qs = qs.filter(**{f"{field}__isnull": True})
        return qs.first()

    def _handle_save_rule(self, request, cd):
        """Find an existing matching rule or redirect to the add-rule form with pre-filled params."""
        from urllib.parse import urlencode

        name_template = cd["name_template"]
        channel_count = cd.get("channel_count") or 0
        channel_start = cd.get("channel_start") or 0
        module_type_is_regex = cd.get("module_type_is_regex", False)
        module_type = cd.get("module_type")

        # Skip duplicate detection when the user lacks view permission — we cannot
        # query existing rules without it, so add-only users always land on the
        # create form (potentially allowing duplicates).
        if request.user.has_perm("netbox_interface_name_rules.view_interfacenamerule"):
            existing = self._find_existing_rule(cd, request.user)
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
            "parent_name_template": cd.get("parent_name_template", ""),
            "breakout_mode": cd.get("breakout_mode") or BreakoutModeChoices.FLAT,
            "module_type_is_regex": "on" if module_type_is_regex else "",
            "channel_count": channel_count,
            "channel_start": channel_start,
        }
        if module_type_is_regex:
            params["module_type_pattern"] = cd.get("module_type_pattern", "")
        elif module_type:
            params["module_type"] = module_type.pk
        for field in ("parent_module_type", "device_type", "platform"):
            val = cd.get(field)
            if val:
                params[field] = val.pk
        add_url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_add")
        return redirect(f"{add_url}?{urlencode(params)}")

    def _evaluate_template_preview(self, cd):
        """Evaluate the rule's templates against form variables; return (preview_results, error).

        Each row carries the role the DB preview uses — ``parent``, ``channel`` or ``interface`` —
        so a channelized rule shows the parent it creates alongside the channels under it.
        """
        from .naming import evaluate_name_template

        name_template = cd["name_template"]
        channel_count = cd.get("channel_count") or 0
        channel_start = cd.get("channel_start") or 0
        variables = {
            "slot": cd.get("var_slot") or "1",
            "bay_position": cd.get("var_bay_position") or "1",
            "bay_position_num": cd.get("var_bay_position_num") or "1",
            "parent_bay_position": cd.get("var_parent_bay_position") or "1",
            "sfp_slot": cd.get("var_sfp_slot") or "1",
            "base": cd.get("var_base") or "Ethernet1",
        }

        def row(result, role, channel=None):
            """Describe one previewed name."""
            return {"source": variables["base"], "channel": channel, "result": result, "role": role}

        try:
            if channel_count > 0:
                preview_results = []
                if cd.get("breakout_mode") == BreakoutModeChoices.CHANNELIZED:
                    parent_template = cd.get("parent_name_template") or ""
                    parent_name = (
                        evaluate_name_template(parent_template, variables) if parent_template else variables["base"]
                    )
                    preview_results.append(row(parent_name, "parent"))
                for ch in range(channel_count):
                    channel = str(channel_start + ch)
                    preview_results.append(
                        row(
                            evaluate_name_template(name_template, {**variables, "channel": channel}), "channel", channel
                        )
                    )
            else:
                preview_results = [row(evaluate_name_template(name_template, variables), "interface")]
            return preview_results, None
        except Exception as exc:
            logger.exception("Template evaluation error: %s", exc)
            return None, type(exc).__name__

    def _fetch_db_preview(self, cd):
        """Run find_interfaces_for_rule against the DB; return (db_preview, db_total, error)."""
        from .engine import find_interfaces_for_rule

        name_template = cd["name_template"]
        channel_count = cd.get("channel_count") or 0
        channel_start = cd.get("channel_start") or 0
        module_type_is_regex = cd.get("module_type_is_regex", False)
        module_type = cd.get("module_type")
        module_type_pattern = cd.get("module_type_pattern", "")

        if not ((module_type_is_regex and module_type_pattern) or (not module_type_is_regex and module_type)):
            return None, 0, None

        fake = RulePreview(
            module_type_is_regex=module_type_is_regex,
            module_type_pattern=module_type_pattern,
            module_type=module_type,
            parent_module_type=cd.get("parent_module_type"),
            device_type=cd.get("device_type"),
            platform=cd.get("platform"),
            name_template=name_template,
            parent_name_template=cd.get("parent_name_template", ""),
            breakout_mode=cd.get("breakout_mode") or BreakoutModeChoices.FLAT,
            channel_count=channel_count,
            channel_start=channel_start,
        )
        try:
            db_preview, db_total = find_interfaces_for_rule(fake, limit=100)
            return db_preview, db_total, None
        except (re.error, ValueError) as exc:
            return [], 0, f"Invalid module type regex: {exc}"
        except Exception as exc:
            logger.exception("Unexpected error in find_interfaces_for_rule: %s", exc)
            return [], 0, f"Unexpected error: {type(exc).__name__}"


class RuleApplyListView(BaseMultiObjectView):
    """Display all rules with buttons to preview/apply each one."""

    queryset = InterfaceNameRule.objects.all()
    template_name = "netbox_interface_name_rules/rule_apply.html"

    def get_required_permission(self):
        """Return the permission required to access the apply-rules page."""
        return "netbox_interface_name_rules.view_interfacenamerule"

    def get(self, request):
        """Render the list of all rules with apply/preview buttons."""
        rules = self.queryset.select_related("module_type", "parent_module_type", "device_type", "platform").order_by(
            "pk"
        )
        return render(request, self.template_name, {"rules": rules, "batch_limit": APPLY_BATCH_LIMIT})


class RuleApplicableView(generic.ObjectView):
    """Return JSON indicating whether a rule would rename at least one interface.

    Called on demand from the Apply Rules page — NOT at page load — to avoid
    expensive full-scan queries blocking the initial render.
    """

    queryset = InterfaceNameRule.objects.all()

    def get(self, request, **kwargs):
        """Return JSON {"applicable": bool} for the rule identified by pk."""
        from .engine import has_applicable_interfaces

        rule = self.get_object(**kwargs)
        try:
            applicable = has_applicable_interfaces(rule)
        except Exception as exc:
            logger.exception("applicability scan failed for rule %s", kwargs.get("pk"))
            return JsonResponse(
                {"applicable": None, "error": f"scan failed: {type(exc).__name__}"},
                status=500,
            )
        return JsonResponse({"applicable": applicable})


class RuleApplyDetailView(generic.ObjectView):
    """Show a preview of changes for a specific rule and allow applying them."""

    queryset = InterfaceNameRule.objects.all()
    template_name = "netbox_interface_name_rules/rule_apply_detail.html"
    additional_permissions = ["dcim.change_interface"]

    def get(self, request, **kwargs):
        """Render a preview of all interfaces that would be renamed by this rule."""
        from .engine import find_convertible_families, find_interfaces_for_rule, supports_channelization

        rule = self.get_object(**kwargs)
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
        # A conversion-scan failure must not blank the unrelated apply preview above.
        try:
            # Each family scanned costs a dry-run conversion, so the scan takes the same batch cap.
            conversions, conversions_have_more = find_convertible_families(rule, limit=APPLY_BATCH_LIMIT)
        except (re.error, ValueError) as exc:
            logger.exception("Failed to compute the conversion preview for rule %s: %s", rule, exc)
            messages.error(request, f"Failed to compute the conversion preview: {exc}")
            conversions, conversions_have_more = [], False
        except Exception as exc:
            logger.exception("Unexpected error computing the conversion preview for rule %s: %s", rule, exc)
            messages.error(request, f"Failed to compute the conversion preview: {type(exc).__name__}")
            conversions, conversions_have_more = [], False
        return render(
            request,
            self.template_name,
            {
                "rule": rule,
                "preview": preview,
                "conversions": conversions,
                "conversions_have_more": conversions_have_more,
                "total_checked": total_checked,
                "batch_limit": APPLY_BATCH_LIMIT,
                "has_more": len(preview) >= APPLY_BATCH_LIMIT,
                "can_apply": request.user.has_perm("dcim.change_interface"),
                # Families exist only where NetBox models channelization; older releases count plain interfaces.
                "families_supported": supports_channelization(),
                "checked_unit": "families" if supports_channelization() else "interfaces",
            },
        )

    def _enqueue(self, request, rule, job_class, name):
        """Enqueue *job_class* against *rule* and report the outcome to the operator."""
        try:
            job = job_class.enqueue(
                # instance is intentionally omitted: InterfaceNameRule does not
                # inherit JobsMixin, so passing instance= would fail full_clean().
                # The job is still named and findable in Core → Jobs.
                name=name,
                user=request.user,
                rule_id=rule.pk,
            )
            messages.success(request, f"Background job enqueued (job #{job.pk}). Check Core → Jobs for status.")
        except Exception as e:
            logger.exception("Failed to enqueue background job for rule %s: %s", rule, e)
            messages.error(request, f"Failed to enqueue background job: {type(e).__name__}")

    def _convert(self, request, rule):
        """Convert the flat families the operator confirmed on this page."""
        from .engine import convert_flat_families

        convert_ids = [int(i) for i in request.POST.getlist("convert_ids") if i.isdigit()]
        if not convert_ids:
            messages.warning(request, "No families selected; nothing was converted.")
            return
        # Converting the first APPLY_BATCH_LIMIT of them would rewrite a set the operator never chose.
        if len(convert_ids) > APPLY_BATCH_LIMIT:
            messages.warning(
                request,
                f"{len(convert_ids)} families selected; synchronous conversion is limited to "
                f"{APPLY_BATCH_LIMIT} family(ies) per run, so nothing was converted. "
                f"Use Convert as Background Job to convert all of them.",
            )
            return
        conflicts = []
        try:
            count = convert_flat_families(rule, convert_ids, conflicts=conflicts)
        except Exception as e:
            logger.exception("Failed to convert families for rule %s: %s", rule, e)
            messages.error(request, f"Failed to convert families: {type(e).__name__}")
            return
        messages.success(request, f"Converted {count} interface family(ies) to the channelized topology.")
        if conflicts:
            messages.warning(request, f"{len(conflicts)} family(ies) skipped — the plugin log names each one.")

    def post(self, request, **kwargs):
        """Apply or convert (foreground batch or background job) and redirect back."""
        from .engine import apply_rule_to_existing

        rule = self.get_object(**kwargs)
        action = request.POST.get("action", "apply")

        if action == "convert":
            self._convert(request, rule)
        elif action == "convert_background":
            from .jobs import ConvertFlatFamiliesJob

            self._enqueue(request, rule, ConvertFlatFamiliesJob, f"Convert flat families: {rule}")
        elif action == "background":
            from .jobs import ApplyRuleJob

            self._enqueue(request, rule, ApplyRuleJob, f"Apply rule: {rule}")
        else:
            try:
                raw_ids = request.POST.getlist("interface_ids")
                interface_ids = [int(i) for i in raw_ids if i.isdigit()]
                if not interface_ids:
                    messages.warning(request, "No interfaces selected; nothing was applied.")
                else:
                    conflicts = []
                    count = apply_rule_to_existing(
                        rule, limit=APPLY_BATCH_LIMIT, interface_ids=interface_ids, conflicts=conflicts
                    )
                    messages.success(request, f"Applied rule: {count} interface(s) renamed.")
                    if conflicts:
                        messages.warning(
                            request,
                            f"{len(conflicts)} interface(s) skipped — the plugin log names each one.",
                        )
            except Exception as e:
                logger.exception("Failed to apply rule %s: %s", rule, e)
                messages.error(request, f"Failed to apply rule {rule}: {type(e).__name__}")

        return redirect("plugins:netbox_interface_name_rules:interfacenamerule_apply_detail", pk=rule.pk)


@register_model_view(InterfaceNameRule, name="toggle", path="toggle")
class RuleToggleView(generic.ObjectView):
    """POST /rules/<pk>/toggle/ — flip the enabled flag on a rule."""

    queryset = InterfaceNameRule.objects.all()

    def post(self, request, pk):
        """Toggle the enabled flag on the rule and return JSON or redirect."""
        rule = self.get_object(pk=pk)
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
        referer = request.META.get("HTTP_REFERER", "")
        if referer and url_has_allowed_host_and_scheme(
            referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(referer)
        return redirect("plugins:netbox_interface_name_rules:interfacenamerule_list")
