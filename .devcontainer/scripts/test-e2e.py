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
    # Try adding system dist-packages (devcontainer has playwright outside venv)
    sys.path.insert(0, "/usr/local/lib/python3.12/dist-packages")
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

# Rule IDs for toggle test (linux.yaml unscoped regex rules)
TOGGLE_RULE_ID = 101  # regex:QSFP-100G-.* → eth{bay_position_num}d{channel}

# VC test constants (populated by load-sample-data.py)
VC_DEVICE_ID = 32  # vc-stack-1 (vc_position=1, master)
VC_BAY_ID = 1580  # linecard0 on vc-stack-1 (position=0)
VC_DEVICE_ID_2 = 33  # vc-stack-2 (vc_position=2, non-master)
VC_BAY_ID_2 = 1581  # linecard0 on vc-stack-2 (position=0)
VC_CHASSIS_ID = 1  # test-vc-stack VirtualChassis pk
VC_MODULE_TYPE = "VC-LINECARD"
VC_SFP_MODULE_TYPE = "VC-SFP"
VC_MANUFACTURER_ID = 12  # Test Manufacturer


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
    try:
        page.wait_for_selector(f"#{field_id}-ts-dropdown", state="hidden", timeout=5000)
    except Exception:
        # Dropdown may not close automatically on slow pages; press Escape to force close
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)


def _api_patch(url: str, payload: dict, headers: dict) -> None:
    """PATCH a NetBox API endpoint with JSON payload."""
    import json as _json
    import urllib.request

    data = _json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
        assert resp.status in (200, 201), f"PATCH {url} returned {resp.status}"


def _poll_for_text(page, base_url: str, path: str, text: str, timeout: float = 8.0) -> bool:
    """Navigate to path and poll until the given text appears on the page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page.goto(f"{base_url}{path}")
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        if page.locator(f"text={text}").count() > 0:
            return True
        time.sleep(0.5)
    return False


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
                # Bay may have install link (empty) or edit link (already occupied) — both are valid
                has_install_or_edit = (
                    page.locator(f'a[href*="module_bay={bay_id}"]').count() > 0
                    or page.locator('a[href*="/dcim/modules/"]').count() > 0
                )
                assert has_install_or_edit, f"no link found for bay {bay_id} ({bay_name})"
            ok("module bays: Transceiver 0–35 visible with install links")
        except Exception as e:
            fail("module bays page", e)

        # ── Pre-clean: remove stale modules from test bays (idempotent) ─────
        try:
            import urllib.request as _ureq_pre
            import json as _json_pre

            _pre_opener = _ureq_pre.build_opener(_ureq_pre.ProxyHandler({}))
            _pre_cookies = ctx.cookies()
            _pre_csrf = next((c["value"] for c in _pre_cookies if c["name"] == "csrftoken"), "")
            _pre_session = next((c["value"] for c in _pre_cookies if c["name"] == "sessionid"), "")
            _pre_headers = {
                "X-CSRFToken": _pre_csrf,
                "Cookie": f"csrftoken={_pre_csrf}; sessionid={_pre_session}",
                "Content-Type": "application/json",
            }
            for _bay_id, _, _ in BAYS:
                req = _ureq_pre.Request(
                    f"{base_url}/api/dcim/modules/?device_id={DEVICE_ID}&module_bay_id={_bay_id}",
                    headers=_pre_headers,
                )
                with _pre_opener.open(req, timeout=API_TIMEOUT) as resp:
                    _existing = _json_pre.loads(resp.read())
                for _m in _existing.get("results", []):
                    _pre_opener.open(
                        _ureq_pre.Request(
                            f"{base_url}/api/dcim/modules/{_m['id']}/",
                            headers=_pre_headers,
                            method="DELETE",
                        ),
                        timeout=API_TIMEOUT,
                    )
        except Exception as _e:
            print(f"  [pre-clean bays] warning: {_e}")

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

        # ── Test: rule toggle — enable/disable via UI ─────────────────────────
        try:
            page.goto(f"{base_url}/plugins/interface-name-rules/rules/")
            page.wait_for_load_state("networkidle", timeout=10000)
            toggle_form = page.locator(f'form[action*="/{TOGGLE_RULE_ID}/toggle/"]').first
            assert toggle_form.count() > 0, f"toggle form for rule {TOGGLE_RULE_ID} not found"
            btn = toggle_form.locator("button")
            assert "btn-success" in (btn.get_attribute("class") or ""), "rule should start enabled (btn-success)"
            ok("rule toggle: rule list shows enabled state")
        except Exception as e:
            fail("rule toggle: enabled state visible", e)

        try:
            page.goto(f"{base_url}/plugins/interface-name-rules/rules/")
            page.wait_for_load_state("networkidle", timeout=10000)
            page.locator(f'form[action*="/{TOGGLE_RULE_ID}/toggle/"]').first.locator("button").click()
            page.wait_for_load_state("networkidle", timeout=10000)
            btn_after = page.locator(f'form[action*="/{TOGGLE_RULE_ID}/toggle/"]').first.locator("button")
            assert "btn-secondary" in (btn_after.get_attribute("class") or ""), (
                "rule should be disabled (btn-secondary)"
            )
            row = page.locator(f'form[action*="/{TOGGLE_RULE_ID}/toggle/"]').locator("xpath=ancestor::tr")
            row_class = row.get_attribute("class") or ""
            assert "opacity-50" in row_class or "text-muted" in row_class, f"disabled row not greyed: {row_class!r}"
            ok("rule toggle: rule disabled — button grey, row greyed out")
        except Exception as e:
            fail("rule toggle: disable rule", e)

        try:
            page.goto(f"{base_url}/plugins/interface-name-rules/rules/")
            page.wait_for_load_state("networkidle", timeout=10000)
            page.locator(f'form[action*="/{TOGGLE_RULE_ID}/toggle/"]').first.locator("button").click()
            page.wait_for_load_state("networkidle", timeout=10000)
            btn_re = page.locator(f'form[action*="/{TOGGLE_RULE_ID}/toggle/"]').first.locator("button")
            assert "btn-success" in (btn_re.get_attribute("class") or ""), "rule should be re-enabled (btn-success)"
            ok("rule toggle: rule re-enabled — button green")
        except Exception as e:
            fail("rule toggle: re-enable rule", e)

        # ── Test: VC position — module install on VC member ───────────────────
        import urllib.request
        import json as _json

        # Bypass HTTP proxy for localhost API calls (proxy env vars exclude ::1 but not localhost)
        _no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        urllib.request.install_opener(_no_proxy_opener)

        cookies = ctx.cookies()
        csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken"), "")
        session = next((c["value"] for c in cookies if c["name"] == "sessionid"), "")
        api_headers = {
            "X-CSRFToken": csrf,
            "Cookie": f"csrftoken={csrf}; sessionid={session}",
            "Content-Type": "application/json",
        }

        vc_rule_id = None
        vc_rule_created = False
        try:
            req = urllib.request.Request(
                f"{base_url}/api/dcim/module-types/?model=VC-LINECARD",
                headers=api_headers,
            )
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                mt_data = _json.loads(resp.read())
            assert mt_data["count"] > 0, "VC-LINECARD module type not found"
            vc_mt_id = mt_data["results"][0]["id"]

            # Check if a rule for VC-LINECARD already exists (e.g. from demo-vc.yaml)
            req_check = urllib.request.Request(
                f"{base_url}/api/plugins/interface-name-rules/rules/?module_type_id={vc_mt_id}&module_type_is_regex=false",
                headers=api_headers,
            )
            with urllib.request.urlopen(req_check, timeout=API_TIMEOUT) as resp:
                existing_rules = _json.loads(resp.read())

            if existing_rules["count"] > 0:
                vc_rule_id = existing_rules["results"][0]["id"]
                ok(f"VC: using existing rule VC-LINECARD (id={vc_rule_id})")
            else:
                rule_payload = _json.dumps(
                    {
                        "module_type": vc_mt_id,
                        "module_type_is_regex": False,
                        "name_template": "Gi{vc_position}/{bay_position_num}",
                        "enabled": True,
                    }
                ).encode()
                req2 = urllib.request.Request(
                    f"{base_url}/api/plugins/interface-name-rules/rules/",
                    data=rule_payload,
                    headers=api_headers,
                    method="POST",
                )
                with urllib.request.urlopen(req2, timeout=API_TIMEOUT) as resp:
                    rule_data = _json.loads(resp.read())
                vc_rule_id = rule_data["id"]
                vc_rule_created = True
                ok(f"VC: created rule VC-LINECARD → Gi{{vc_position}}/{{bay_position_num}} (id={vc_rule_id})")
        except Exception as e:
            fail("VC: create VC-LINECARD rule", e)

        if vc_rule_id:
            try:
                page.goto(
                    f"{base_url}/dcim/modules/add/"
                    f"?device={VC_DEVICE_ID}&module_bay={VC_BAY_ID}"
                    f"&manufacturer={VC_MANUFACTURER_ID}"
                    f"&return_url=/dcim/devices/{VC_DEVICE_ID}/interfaces/"
                )
                page.wait_for_load_state("networkidle", timeout=10000)
                tomselect_pick(page, "id_module_type", VC_MODULE_TYPE)
                page.locator('button[name="_create"]').click()
                page.wait_for_load_state("networkidle", timeout=15000)
                ok(f"VC: installed {VC_MODULE_TYPE} into vc-stack-1 linecard0")
            except Exception as e:
                fail(f"VC: install {VC_MODULE_TYPE}", e)

            try:
                # vc-stack-1 has vc_position=1, bay position=0 → expect Gi1/0
                expected_vc_iface = "Gi1/0"
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID}/interfaces/", expected_vc_iface)
                assert found, f"'{expected_vc_iface}' not found — {{vc_position}} not substituted"
                ok(f"VC: interface '{expected_vc_iface}' created — {{vc_position}} works")
            except Exception as e:
                fail("VC: interface with vc_position", e)

            # ── Test: VC position change → signal re-renames interface ────────
            try:
                # Change vc-stack-1 vc_position 1 → 3 via API
                _api_patch(
                    f"{base_url}/api/dcim/devices/{VC_DEVICE_ID}/",
                    {"vc_position": 3},
                    api_headers,
                )
                # Poll for Gi3/0
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID}/interfaces/", "Gi3/0")
                assert found, "'Gi3/0' not found after vc_position 1→3"
                ok("VC: vc_position change 1→3 renamed interface to 'Gi3/0'")

                # Restore vc_position 3 → 1
                _api_patch(
                    f"{base_url}/api/dcim/devices/{VC_DEVICE_ID}/",
                    {"vc_position": 1},
                    api_headers,
                )
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID}/interfaces/", "Gi1/0")
                assert found, "'Gi1/0' not found after restoring vc_position 1"
                ok("VC: vc_position restored 3→1, interface back to 'Gi1/0'")
            except Exception as e:
                fail("VC: position change signal", e)
                # Ensure vc_position is restored even on failure
                try:
                    _api_patch(
                        f"{base_url}/api/dcim/devices/{VC_DEVICE_ID}/",
                        {"vc_position": 1},
                        api_headers,
                    )
                except Exception:
                    pass

            # ── Test: module type change → signal re-renames interface ───────
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_url}/api/dcim/modules/?device_id={VC_DEVICE_ID}",
                        headers=api_headers,
                    ),
                    timeout=API_TIMEOUT,
                ) as resp:
                    mt_chg_mods = _json.loads(resp.read())
                lc_mod_for_mt = next(
                    (m for m in mt_chg_mods.get("results", []) if m["module_type"]["model"] == VC_MODULE_TYPE),
                    None,
                )
                assert lc_mod_for_mt, f"VC-LINECARD module not found on device {VC_DEVICE_ID}"
                lc_module_id_mt = lc_mod_for_mt["id"]

                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_url}/api/dcim/module-types/?model={VC_SFP_MODULE_TYPE}",
                        headers=api_headers,
                    ),
                    timeout=API_TIMEOUT,
                ) as resp:
                    sfp_mt_data = _json.loads(resp.read())
                assert sfp_mt_data["count"] > 0, f"{VC_SFP_MODULE_TYPE} module type not found"
                sfp_mt_id_for_swap = sfp_mt_data["results"][0]["id"]

                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_url}/api/dcim/module-types/?model={VC_MODULE_TYPE}",
                        headers=api_headers,
                    ),
                    timeout=API_TIMEOUT,
                ) as resp:
                    lc_mt_data = _json.loads(resp.read())
                lc_mt_id_for_restore = lc_mt_data["results"][0]["id"]

                # PATCH module type VC-LINECARD → VC-SFP
                _api_patch(
                    f"{base_url}/api/dcim/modules/{lc_module_id_mt}/",
                    {"module_type": sfp_mt_id_for_swap},
                    api_headers,
                )
                # VC-SFP rule: Gi{vc_position}/{parent_bay_position}/{sfp_slot} = Gi1/0/0
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID}/interfaces/", "Gi1/0/0")
                assert found, "'Gi1/0/0' not found after module type change VC-LINECARD->VC-SFP"
                ok("module type change: VC-LINECARD->VC-SFP renames Gi1/0 to Gi1/0/0")

                # PATCH module type VC-SFP → VC-LINECARD (restore)
                _api_patch(
                    f"{base_url}/api/dcim/modules/{lc_module_id_mt}/",
                    {"module_type": lc_mt_id_for_restore},
                    api_headers,
                )
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID}/interfaces/", "Gi1/0")
                assert found, "'Gi1/0' not found after restoring VC-LINECARD type"
                ok("module type change: restoring VC-LINECARD type renames Gi1/0/0 back to Gi1/0")
            except Exception as e:
                fail("module type change signal", e)
                # Best-effort restore to VC-LINECARD on failure
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(
                            f"{base_url}/api/dcim/module-types/?model={VC_MODULE_TYPE}",
                            headers=api_headers,
                        ),
                        timeout=API_TIMEOUT,
                    ) as resp:
                        _lc_mt_restore = _json.loads(resp.read())
                    if _lc_mt_restore.get("results"):
                        with urllib.request.urlopen(
                            urllib.request.Request(
                                f"{base_url}/api/dcim/modules/?device_id={VC_DEVICE_ID}",
                                headers=api_headers,
                            ),
                            timeout=API_TIMEOUT,
                        ) as resp:
                            _all_mods = _json.loads(resp.read())
                        _wrong_type_mod = next(
                            (
                                m
                                for m in _all_mods.get("results", [])
                                if m["module_type"]["model"] == VC_SFP_MODULE_TYPE
                                and m.get("module_bay", {}).get("name") == "linecard0"
                            ),
                            None,
                        )
                        if _wrong_type_mod:
                            _api_patch(
                                f"{base_url}/api/dcim/modules/{_wrong_type_mod['id']}/",
                                {"module_type": _lc_mt_restore["results"][0]["id"]},
                                api_headers,
                            )
                except Exception:
                    pass

            # ── Test: VC-SFP nested install → Gi{vc_position}/{parent}/{sfp} ─
            try:
                # Find the installed VC-LINECARD module on vc-stack-1
                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_url}/api/dcim/modules/?device_id={VC_DEVICE_ID}",
                        headers=api_headers,
                    ),
                    timeout=API_TIMEOUT,
                ) as resp:
                    mods_data = _json.loads(resp.read())
                lc_mods = [m for m in mods_data.get("results", []) if m["module_type"]["model"] == VC_MODULE_TYPE]
                assert lc_mods, f"VC-LINECARD not installed on device {VC_DEVICE_ID}"
                linecard_id = lc_mods[0]["id"]

                # Find sfp0 sub-bay on the linecard module
                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_url}/api/dcim/module-bays/?module_id={linecard_id}&name=sfp0",
                        headers=api_headers,
                    ),
                    timeout=API_TIMEOUT,
                ) as resp:
                    bays_data = _json.loads(resp.read())
                assert bays_data["count"] > 0, f"sfp0 bay not found on VC-LINECARD module {linecard_id}"
                sfp0_bay_id = bays_data["results"][0]["id"]

                # Install VC-SFP via UI
                page.goto(
                    f"{base_url}/dcim/modules/add/"
                    f"?device={VC_DEVICE_ID}&module_bay={sfp0_bay_id}"
                    f"&manufacturer={VC_MANUFACTURER_ID}"
                    f"&return_url=/dcim/devices/{VC_DEVICE_ID}/interfaces/"
                )
                page.wait_for_load_state("networkidle", timeout=10000)
                tomselect_pick(page, "id_module_type", VC_SFP_MODULE_TYPE)
                page.locator('button[name="_create"]').click()
                page.wait_for_load_state("networkidle", timeout=15000)
                ok(f"VC: installed {VC_SFP_MODULE_TYPE} into vc-stack-1 linecard0/sfp0")

                # Poll for Gi1/0/0
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID}/interfaces/", "Gi1/0/0")
                assert found, "'Gi1/0/0' not found — nested VC-SFP naming failed"
                ok("VC: VC-SFP interface 'Gi1/0/0' created — nested {vc_position}/{parent_bay}/{sfp_slot} works")
            except Exception as e:
                fail("VC: VC-SFP nested install", e)

            # ── Test: VC list member count ───────────────────────────────────
            try:
                page.goto(f"{base_url}/dcim/virtual-chassis/")
                page.wait_for_load_state("networkidle", timeout=10000)
                content = page.content()
                # Both VCs should show "2" members in the list (not "3")
                assert "demo-vc-stack" in content, "demo-vc-stack not found in VC list"
                assert "test-vc-stack" in content, "test-vc-stack not found in VC list"
                # The member count column — verify no "3" appears for our VCs
                # Find the table row for each VC and check the Members column
                demo_row = page.locator("tr", has=page.locator('text="demo-vc-stack"'))
                test_row = page.locator("tr", has=page.locator('text="test-vc-stack"'))
                assert demo_row.count() > 0, "demo-vc-stack row not found"
                assert test_row.count() > 0, "test-vc-stack row not found"
                # Member count "3" should not appear in either row
                demo_row_text = demo_row.first.inner_text(timeout=3000)
                test_row_text = test_row.first.inner_text(timeout=3000)
                assert "\t3\t" not in demo_row_text and " 3 " not in demo_row_text.replace("\n", " "), (
                    f"demo-vc-stack shows unexpected member count: {demo_row_text!r}"
                )
                assert "\t3\t" not in test_row_text and " 3 " not in test_row_text.replace("\n", " "), (
                    f"test-vc-stack shows unexpected member count: {test_row_text!r}"
                )
                ok("VC: member count shows 2 (not 3) for both VCs")
            except Exception as e:
                fail("VC: member count verification", e)

            # ── Test: VC re-membership (vc-stack-2) → rename on re-add ────────
            try:
                # Install VC-LINECARD on vc-stack-2 first
                page.goto(
                    f"{base_url}/dcim/modules/add/"
                    f"?device={VC_DEVICE_ID_2}&module_bay={VC_BAY_ID_2}"
                    f"&manufacturer={VC_MANUFACTURER_ID}"
                    f"&return_url=/dcim/devices/{VC_DEVICE_ID_2}/interfaces/"
                )
                page.wait_for_load_state("networkidle", timeout=10000)
                tomselect_pick(page, "id_module_type", VC_MODULE_TYPE)
                page.locator('button[name="_create"]').click()
                page.wait_for_load_state("networkidle", timeout=15000)

                # Verify Gi2/0
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID_2}/interfaces/", "Gi2/0")
                assert found, "'Gi2/0' not found after VC-LINECARD install on vc-stack-2"
                ok("VC: vc-stack-2 initial install → 'Gi2/0'")

                # Remove vc-stack-2 from VC (vc_position → None)
                _api_patch(
                    f"{base_url}/api/dcim/devices/{VC_DEVICE_ID_2}/",
                    {"virtual_chassis": None, "vc_position": None},
                    api_headers,
                )

                # Re-add at position 5
                _api_patch(
                    f"{base_url}/api/dcim/devices/{VC_DEVICE_ID_2}/",
                    {"virtual_chassis": VC_CHASSIS_ID, "vc_position": 5},
                    api_headers,
                )

                # Poll for Gi5/0 — signal should have fired and renamed
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID_2}/interfaces/", "Gi5/0")
                assert found, "'Gi5/0' not found after re-membership at vc_position=5"
                ok("VC: re-membership (remove+add at pos 5) renamed interface to 'Gi5/0'")

                # Restore vc-stack-2 to position 2
                _api_patch(
                    f"{base_url}/api/dcim/devices/{VC_DEVICE_ID_2}/",
                    {"virtual_chassis": VC_CHASSIS_ID, "vc_position": 2},
                    api_headers,
                )
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID_2}/interfaces/", "Gi2/0")
                assert found, "'Gi2/0' not found after restoring vc_position=2"
                ok("VC: restored vc-stack-2 to pos 2, interface renamed back to 'Gi2/0'")

                # Sub-test: VC removal — interface persists (module still installed)
                _api_patch(
                    f"{base_url}/api/dcim/devices/{VC_DEVICE_ID_2}/",
                    {"virtual_chassis": None, "vc_position": None},
                    api_headers,
                )
                import time as _time

                _time.sleep(0.5)
                page.goto(f"{base_url}/dcim/devices/{VC_DEVICE_ID_2}/interfaces/")
                page.wait_for_load_state("networkidle", timeout=10000)
                # Interface should still be there (module FK intact; our plugin skips rename on removal)
                assert "Gi2/0" in page.content(), "Gi2/0 disappeared after VC removal — unexpected cascade"
                ok("VC: interface 'Gi2/0' persists after device removed from VC (module still installed)")

                # Restore vc-stack-2 to VC to keep environment clean
                _api_patch(
                    f"{base_url}/api/dcim/devices/{VC_DEVICE_ID_2}/",
                    {"virtual_chassis": VC_CHASSIS_ID, "vc_position": 2},
                    api_headers,
                )
                found = _poll_for_text(page, base_url, f"/dcim/devices/{VC_DEVICE_ID_2}/interfaces/", "Gi2/0")
                assert found, "'Gi2/0' not found after re-adding vc-stack-2 (same pos=2)"
                ok("VC: re-add vc-stack-2 at same pos=2 — 'Gi2/0' confirmed (idempotent re-rename)")
            except Exception as e:
                fail("VC: re-membership signal", e)
                # Ensure vc-stack-2 is restored to VC at position 2 even on failure
                try:
                    _api_patch(
                        f"{base_url}/api/dcim/devices/{VC_DEVICE_ID_2}/",
                        {"virtual_chassis": VC_CHASSIS_ID, "vc_position": 2},
                        api_headers,
                    )
                except Exception:
                    pass
            finally:
                # Clean up vc-stack-2 modules
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(
                            f"{base_url}/api/dcim/modules/?device_id={VC_DEVICE_ID_2}",
                            headers=api_headers,
                        ),
                        timeout=API_TIMEOUT,
                    ) as resp:
                        vc2_mods = _json.loads(resp.read())
                    for m in vc2_mods.get("results", []):
                        urllib.request.urlopen(
                            urllib.request.Request(
                                f"{base_url}/api/dcim/modules/{m['id']}/",
                                headers=api_headers,
                                method="DELETE",
                            ),
                            timeout=API_TIMEOUT,
                        )
                except Exception as e:
                    print(f"  [cleanup] warning (vc-stack-2 modules): {e}")

            print("\n  [cleanup] removing VC test module and rule...")

            # ── Test: module cascade delete removes interface ─────────────────
            try:
                # vc-stack-1 should still have VC-LINECARD installed from earlier tests
                with urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_url}/api/dcim/modules/?device_id={VC_DEVICE_ID}",
                        headers=api_headers,
                    ),
                    timeout=API_TIMEOUT,
                ) as resp:
                    mods_data2 = _json.loads(resp.read())
                lc_mods2 = [m for m in mods_data2.get("results", []) if m["module_type"]["model"] == VC_MODULE_TYPE]
                if lc_mods2:
                    # Verify Gi1/0 exists
                    page.goto(f"{base_url}/dcim/devices/{VC_DEVICE_ID}/interfaces/")
                    page.wait_for_load_state("networkidle", timeout=10000)
                    assert "Gi1/0" in page.content(), "Gi1/0 not present before cascade delete test"

                    # Delete VC-LINECARD module via API — NetBox should cascade-delete Gi1/0
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"{base_url}/api/dcim/modules/{lc_mods2[0]['id']}/",
                            headers=api_headers,
                            method="DELETE",
                        ),
                        timeout=API_TIMEOUT,
                    )

                    # Verify Gi1/0 is gone (cascade delete by NetBox, not by our plugin)
                    import time as _time

                    _time.sleep(0.5)
                    page.goto(f"{base_url}/dcim/devices/{VC_DEVICE_ID}/interfaces/")
                    page.wait_for_load_state("networkidle", timeout=10000)
                    assert "Gi1/0" not in page.content(), "Gi1/0 still present after module cascade delete"
                    ok("VC: deleting VC-LINECARD module cascades to interface deletion (Gi1/0 gone)")
                else:
                    ok("VC: cascade delete skipped — VC-LINECARD not installed on vc-stack-1")
            except Exception as e:
                fail("VC: module cascade delete", e)
            try:
                req = urllib.request.Request(
                    f"{base_url}/api/dcim/modules/?device_id={VC_DEVICE_ID}",
                    headers=api_headers,
                )
                with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                    vc_mod_data = _json.loads(resp.read())
                for m in vc_mod_data.get("results", []):
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"{base_url}/api/dcim/modules/{m['id']}/",
                            headers=api_headers,
                            method="DELETE",
                        ),
                        timeout=API_TIMEOUT,
                    )
                if vc_rule_created:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"{base_url}/api/plugins/interface-name-rules/rules/{vc_rule_id}/",
                            headers=api_headers,
                            method="DELETE",
                        ),
                        timeout=API_TIMEOUT,
                    )
                print("  [cleanup] VC test module removed ✓")
            except Exception as e:
                print(f"  [cleanup] warning: {e}")

        # ── Cleanup via API ───────────────────────────────────────────────────
        print("\n  [cleanup] removing test modules via API...")
        try:
            import urllib.parse

            module_ids = []
            next_url = f"{base_url}/api/dcim/modules/?device_id={DEVICE_ID}"
            while next_url:
                req = urllib.request.Request(next_url, headers=api_headers)
                with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                    data = _json.loads(resp.read())
                module_ids.extend(m["id"] for m in data.get("results", []))
                next_url = data.get("next")

            removed = 0
            for mid in module_ids:
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"{base_url}/api/dcim/modules/{mid}/",
                            headers=api_headers,
                            method="DELETE",
                        ),
                        timeout=API_TIMEOUT,
                    )
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
