#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Take documentation screenshots using Playwright.

Captures:
  - docs/screenshots/vc-rules-list.png         — rules list filtered to demo-vc device-level rules
  - docs/screenshots/vc-juniper-interfaces.png  — jnp-vc-2 interfaces (ge-1/0/N, xe-1/1/N)
  - docs/screenshots/vc-cisco-interfaces.png    — cisco-sw-2 interfaces (GigabitEthernet2/0/N)
  - docs/screenshots/vc-arista-interfaces.png   — arista-sw-2 interfaces (Ethernet2/N)

All demo devices — no ITC-Lab or production data in screenshots.
"""

import os
import sys

sys.path.insert(0, "/usr/local/lib/python3.12/dist-packages")

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8000"
SCREENSHOTS_DIR = "/workspaces/netbox-InterfaceNameRules-plugin/docs/screenshots"
USERNAME = "admin"
PASSWORD = "admin"

# Device IDs (from devcontainer load-sample-data.py)
DEVICE_JNP_VC2 = 37
DEVICE_CISCO_SW2 = 39
DEVICE_ARISTA_SW2 = 41
DEVICE_DEMO_VC2 = 35


def login(page):
    page.goto(f"{BASE_URL}/login/")
    page.fill("input[name='username']", USERNAME)
    page.fill("input[name='password']", PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_url(f"{BASE_URL}/**", timeout=10000)


def _close_debug_toolbar(page):
    """Remove Django Debug Toolbar elements from the DOM."""
    page.evaluate("""
        ['djdt-toolbar', 'djShowToolBarButton', 'djHideToolBarButton'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.parentNode.removeChild(el);
        });
    """)
    page.wait_for_timeout(100)


def screenshot_rules_list(page):
    """Capture rules list showing device-level rules with filter icon."""
    # Set collapsed state before loading so help card is hidden on load
    page.goto(f"{BASE_URL}/plugins/interface-name-rules/rules/")
    page.evaluate("localStorage.setItem('rulesHelpCollapsed', '1')")
    page.goto(f"{BASE_URL}/plugins/interface-name-rules/rules/?applies_to_device_interfaces=True")
    # Wait for the actual rules table (not the help card's variable table)
    page.wait_for_selector("table.table-hover", timeout=8000)
    # Wait for at least one filter icon in the rules table
    page.wait_for_selector("table.table-hover .mdi-filter-outline", timeout=5000)
    _close_debug_toolbar(page)
    page.screenshot(
        path=f"{SCREENSHOTS_DIR}/vc-rules-list.png",
        full_page=False,
        clip={"x": 290, "y": 60, "width": 1100, "height": 640},
    )
    print("  ✓ vc-rules-list.png")


def screenshot_interfaces(page, device_id, device_name, filename):
    """Capture device interfaces page."""
    page.goto(f"{BASE_URL}/dcim/devices/{device_id}/interfaces/")
    page.wait_for_selector("table.table-hover", timeout=8000)
    page.wait_for_selector("tbody tr", timeout=5000)
    _close_debug_toolbar(page)
    page.screenshot(
        path=f"{SCREENSHOTS_DIR}/{filename}",
        full_page=False,
        clip={"x": 290, "y": 60, "width": 1100, "height": 500},
    )
    print(f"  ✓ {filename} ({device_name})")


def main():
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()

        print("🖼  Logging in…")
        login(page)
        print(f"  ✓ Logged in as {USERNAME}")

        print("🖼  Taking screenshots…")
        screenshot_rules_list(page)
        screenshot_interfaces(page, DEVICE_JNP_VC2, "jnp-vc-2", "vc-juniper-interfaces.png")
        screenshot_interfaces(page, DEVICE_CISCO_SW2, "cisco-sw-2", "vc-cisco-interfaces.png")
        screenshot_interfaces(page, DEVICE_ARISTA_SW2, "arista-sw-2", "vc-arista-interfaces.png")
        screenshot_interfaces(page, DEVICE_DEMO_VC2, "demo-vc-2", "vc-demo-interfaces.png")

        browser.close()

    print("✅ Done — screenshots saved to docs/screenshots/")


if __name__ == "__main__":
    main()
