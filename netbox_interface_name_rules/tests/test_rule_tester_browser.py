# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Exercise rule-kind changes with browser form submission semantics."""

import pytest
from django.urls import reverse

from . import test_views


class RuleTesterBrowserTest(test_views.ViewTestBase):
    def test_switching_rule_kind_submits_only_active_matching_fields(self):
        playwright = pytest.importorskip("playwright.sync_api")
        url = reverse("plugins:netbox_interface_name_rules:interfacenamerule_test")
        response = self.client.post(
            url,
            {
                "applies_to_device_interfaces": "on",
                "module_type_pattern": "Ethernet.*",
                "name_template": "{base}",
                "var_vc_position": "2",
            },
        )
        self.assertEqual(response.status_code, 200)
        submissions = []
        with playwright.sync_playwright() as runner:
            try:
                browser = runner.chromium.launch()
            except playwright.Error as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest("The Playwright Chromium browser is not installed.")
                raise
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.abort())
                page.set_content(response.content.decode())
                page.locator("#id_applies_to_device_interfaces").uncheck()
                page.locator("#id_module_type").select_option(str(self.module_type.pk))
                data = page.locator('form[method="post"]').evaluate("form => Object.fromEntries(new FormData(form))")
                self.assertNotIn("module_type_pattern", data)
                submissions.append(data)

                page.locator("#id_module_type_is_regex").check()
                data = page.locator('form[method="post"]').evaluate("form => Object.fromEntries(new FormData(form))")
                self.assertNotIn("module_type", data)
                self.assertEqual(data["module_type_pattern"], "Ethernet.*")
                submissions.append(data)
            finally:
                browser.close()
        for data in submissions:
            with self.subTest(fields=data):
                submitted = self.client.post(url, {**data, "action": "check"})
                self.assertFalse(submitted.context["form"].errors)
