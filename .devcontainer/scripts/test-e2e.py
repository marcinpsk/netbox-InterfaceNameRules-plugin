#!/usr/bin/env python3
"""
E2E Playwright test: verify SFP module installation into module bays and
automatic interface renaming by InterfaceNameRule.

Requires: playwright (pip install playwright && playwright install chromium)

Usage:
    python .devcontainer/scripts/test-e2e.py
    python .devcontainer/scripts/test-e2e.py --base-url http://127.0.0.1:8000

Tests:
    1. librenms-sync page loads for device 22 (prod-lab03c-ri5.arcos / S9610-36D)
    2. Module bays page shows Transceiver 0–35 with install links
    3. Install QSFP-100G-SR4 into Transceiver 0 via UI (TomSelect widget)
    4. Interface 'swp0' auto-created by InterfaceNameRule [rule: .* → swp{bay_position_num}]
    5. Install QSFP-100G-SR4 into Transceiver 5, verify interface 'swp5'
    6. librenms-sync page still works after module installation
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

import argparse
import os
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

# ── Constants (match devcontainer sample data) ────────────────────────────────
DEVICE_ID = 22  # prod-lab03c-ri5.arcos (S9610-36D)
MANUFACTURER_ID = 2  # Generic
MODULE_TYPE_MODEL = "QSFP-100G-SR4"
API_TIMEOUT = 10  # seconds for API calls during cleanup

# Transceiver bay IDs (populated by load-sample-data.py)
# bay_position_num is the numeric suffix of the bay name: "Transceiver 5" → 5
BAYS = [
    (486, "Transceiver 0", "swp0"),  # bay_position_num=0
    (491, "Transceiver 5", "swp5"),  # bay_position_num=5
]


def tomselect_pick(page, field_id: str, search_text: str) -> None:
    """
    Open a TomSelect widget (by its underlying <select> id), search, and pick
    the first matching option.

    NetBox uses TomSelect for FK/choice fields. The widget creates:
      - #{field_id}-ts-control  → the visible input
      - #{field_id}-ts-dropdown → the dropdown with .option elements
    """
    inp = page.locator(f"#{field_id}-ts-control")
    inp.wait_for(state="visible")
    inp.click()
    inp.fill(search_text)
    page.wait_for_selector(f"#{field_id}-ts-dropdown .option:not(.no-results)", timeout=5000)
    page.locator(f"#{field_id}-ts-dropdown .option").filter(has_text=search_text).first.click()
    page.wait_for_selector(f"#{field_id}-ts-dropdown", state="hidden", timeout=3000)


def run_tests(base_url: str) -> tuple[list[str], list[tuple[str, str]]]:
    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    def ok(name: str) -> None:
        passed.append(name)
        print(f"  ✓ {name}")

    def fail(name: str, err: Exception) -> None:
        failed.append((name, str(err)))
        print(f"  ✗ {name}: {err}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        # ── Login ─────────────────────────────────────────────────────────────
        page.goto(f"{base_url}/login/")
        page.fill('input[name="username"]', os.environ.get("NETBOX_USER", "admin"))
        page.fill('input[name="password"]', os.environ.get("NETBOX_PASSWORD", "admin"))
        page.click('button[type="submit"]')
        page.wait_for_url(f"{base_url}/**", timeout=10000)
        if "/login/" in page.url:
            raise RuntimeError(f"Login failed — still at {page.url!r}")
        print(f"  Logged in → {page.url}\n")

        # ── Test 1: librenms-sync page ────────────────────────────────────────
        try:
            page.goto(f"{base_url}/dcim/devices/{DEVICE_ID}/librenms-sync/")
            page.wait_for_load_state("networkidle", timeout=10000)
            assert page.url == f"{base_url}/dcim/devices/{DEVICE_ID}/librenms-sync/", f"redirected to {page.url}"
            title = page.locator("h1.page-title").inner_text(timeout=5000)
            assert "prod-lab03c-ri5.arcos" in title, f"title={title!r}"
            ok("librenms-sync page loads (device 22: prod-lab03c-ri5.arcos)")
        except Exception as e:
            fail("librenms-sync page loads", e)

        # ── Test 2: Module bays ───────────────────────────────────────────────
        try:
            page.goto(f"{base_url}/dcim/devices/{DEVICE_ID}/module-bays/")
            page.wait_for_load_state("networkidle", timeout=10000)
            assert page.locator("text=Transceiver 0").count() > 0, "Transceiver 0 missing"
            assert page.locator("text=Transceiver 35").count() > 0, "Transceiver 35 missing"
            for bay_id, bay_name, _ in BAYS:
                assert page.locator(f'a[href*="module_bay={bay_id}"]').count() > 0, (
                    f"no install link for bay {bay_id} ({bay_name})"
                )
            ok("module bays: Transceiver 0–35 visible with install links")
        except Exception as e:
            fail("module bays page", e)

        # ── Tests 3+4 / 5+6: Install + verify interface naming ───────────────
        for bay_id, bay_name, expected_iface in BAYS:
            # Install via UI
            try:
                page.goto(
                    f"{base_url}/dcim/modules/add/"
                    f"?device={DEVICE_ID}&module_bay={bay_id}"
                    f"&manufacturer={MANUFACTURER_ID}"
                    f"&return_url=/dcim/devices/{DEVICE_ID}/module-bays/"
                )
                page.wait_for_load_state("networkidle", timeout=10000)
                tomselect_pick(page, "id_module_type", MODULE_TYPE_MODEL)
                page.locator('button[name="_create"]').click()
                page.wait_for_load_state("networkidle", timeout=15000)
                assert page.locator(f"text={MODULE_TYPE_MODEL}").count() > 0, (
                    f"module not shown after install (url={page.url})"
                )
                ok(f"installed {MODULE_TYPE_MODEL} into {bay_name} via UI")
            except Exception as e:
                fail(f"install into {bay_name}", e)
                continue

            # Verify interface name created by InterfaceNameRule
            try:
                deadline = time.monotonic() + 5
                found = False
                while time.monotonic() < deadline:
                    page.goto(f"{base_url}/dcim/devices/{DEVICE_ID}/interfaces/")
                    try:
                        page.wait_for_load_state("networkidle", timeout=3000)
                    except Exception:
                        pass  # timeout is fine; proceed to check locator
                    if page.locator(f"text={expected_iface}").count() > 0:
                        found = True
                        break
                    time.sleep(0.5)
                assert found, f"'{expected_iface}' not found — InterfaceNameRule did not fire"
                ok(f"interface '{expected_iface}' auto-created (rule: S9610-36D .* → swp{{bay_position_num}})")
            except Exception as e:
                fail(f"interface '{expected_iface}' auto-created", e)

        # ── Test: librenms-sync still works after installs ────────────────────
        try:
            page.goto(f"{base_url}/dcim/devices/{DEVICE_ID}/librenms-sync/")
            page.wait_for_load_state("networkidle", timeout=10000)
            assert page.locator("text=Server Error").count() == 0, "500 error on sync page"
            ok("librenms-sync page works after module installation")
        except Exception as e:
            fail("librenms-sync after install", e)

        # ── Cleanup via API ───────────────────────────────────────────────────
        print("\n  [cleanup] removing test modules via API...")
        try:
            import urllib.request
            import urllib.parse
            import json as _json

            # Reuse the browser session cookie for API calls
            cookies = ctx.cookies()
            csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken"), "")
            session = next((c["value"] for c in cookies if c["name"] == "sessionid"), "")
            headers = {
                "X-CSRFToken": csrf,
                "Cookie": f"csrftoken={csrf}; sessionid={session}",
                "Content-Type": "application/json",
            }

            # List modules on device (follow pagination)
            module_ids = []
            next_url = f"{base_url}/api/dcim/modules/?device_id={DEVICE_ID}"
            while next_url:
                req = urllib.request.Request(next_url, headers=headers)
                with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                    data = _json.loads(resp.read())
                module_ids.extend(m["id"] for m in data.get("results", []))
                next_url = data.get("next")

            removed = 0
            for mid in module_ids:
                del_req = urllib.request.Request(
                    f"{base_url}/api/dcim/modules/{mid}/",
                    headers=headers,
                    method="DELETE",
                )
                try:
                    urllib.request.urlopen(del_req, timeout=API_TIMEOUT)
                    removed += 1
                except Exception as e:
                    print(f"  [cleanup] warning: failed to delete module {mid}: {e}")
            print(f"  [cleanup] removed {removed} module(s) ✓")
        except Exception as e:
            print(f"  [cleanup] warning: {e}")

        browser.close()

    return passed, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="NetBox module-install E2E test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    print(f"\n═══ NetBox Module Install E2E Test [{args.base_url}] ═══\n")
    passed, failed = run_tests(args.base_url)

    print(f"\n{'═' * 55}")
    print(f"Results: {len(passed)} passed / {len(failed)} failed")
    if failed:
        print("\nFAILED:")
        for name, err in failed:
            print(f"  ✗ {name}: {err}")
        sys.exit(1)
    else:
        print("✅ All tests passed!")


if __name__ == "__main__":
    main()
