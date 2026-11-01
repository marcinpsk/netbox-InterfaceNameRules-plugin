# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The documentation screenshots, one test per view that README.md or a page of docs/ shows.

Each test checks that the view shows the demo data before it writes the images, so a broken
view fails the run instead of publishing an empty table or an error page.
"""

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from .conftest import (
    APPLY_RENAMES,
    APPLY_RULE,
    PLUGIN,
    ROWS,
    NetBoxAPI,
    interface_names,
    open_page,
    preview_results,
)

#: (screenshot, rule template, regex, channel count, bay position, expected names)
BUILD_RULES = (
    (
        "build-rule-juniper-breakout",
        "xe-0/0/{bay_position}:{channel}",
        "QSFP-4X10G-.*",
        4,
        "4",
        [f"xe-0/0/4:{channel}" for channel in range(4)],
    ),
    (
        "build-rule-sonic-breakout",
        "swp{bay_position_num}s{channel}",
        "QSFP-DD-400G-.*",
        4,
        "5",
        [f"swp5s{channel}" for channel in range(4)],
    ),
    ("build-rule-linux-eth", "eth{bay_position_num}", "SFP28-25G-.*", 0, "0", ["eth0"]),
)
#: (screenshot, VC member, interface names after the rename)
VC_MEMBERS = (
    ("vc-juniper-interfaces", "jnp-vc-2", [*(f"ge-1/0/{i}" for i in range(4)), "xe-1/1/0", "xe-1/1/1"]),
    ("vc-cisco-interfaces", "cisco-sw-2", [f"GigabitEthernet2/0/{i}" for i in range(1, 5)]),
    ("vc-arista-interfaces", "arista-sw-2", [f"Ethernet2/{i}" for i in range(1, 5)]),
)


def fill_build_rule(page: Page, template: str, pattern: str, channels: int, bay_position: str) -> None:
    """Fill the Build Rule form with a regex rule, without a scope."""
    page.locator("#id_name_template").fill(template)
    page.locator("#id_module_type_is_regex").check()
    page.locator("#id_module_type_pattern").fill(pattern)
    page.locator("#id_channel_count").fill(str(channels))
    page.locator("#id_channel_start").fill("0")
    page.locator("#id_var_bay_position").fill(bay_position)


def open_rule_list(page: Page, query: str) -> None:
    """Open the rule list with the reference panel collapsed, so that the image shows the rules."""
    open_page(page, f"{PLUGIN}/rules/?{query}")
    header = page.locator("#rules-help-header")
    if header.get_attribute("aria-expanded") == "true":
        header.click()
    expect(header).to_have_attribute("aria-expanded", "false")


def test_rule_list(page: Page, capture: Callable[..., None]) -> None:
    open_rule_list(page, "per_page=12")
    expect(page.locator(ROWS)).to_have_count(12)
    capture("rule-list")


# A regex rule scores scope × 1000 + len(pattern); the device type scope is 2 and the platform scope is 1.
@pytest.mark.parametrize(
    ("name", "fields", "score"),
    [
        ("rule-detail-juniper-breakout", {"pattern": "QSFP-4X10G-.*", "device_type": "ACX7024"}, 2 * 1000 + 13),
        ("rule-detail-sonic-catch-all", {"pattern": ".*", "platform": "SONiC"}, 1 * 1000 + 2),
    ],
)
def test_rule_detail(
    page: Page, api: NetBoxAPI, capture: Callable[..., None], name: str, fields: dict, score: int
) -> None:
    rule = api.rule(**fields)
    open_page(page, f"{PLUGIN}/rules/{rule['id']}/")
    expect(page.locator("strong", has_text="Priority score")).to_have_text(f"Priority score: {score}")
    table = page.locator("table.attr-table")
    columns = [text.strip() for text in table.locator("thead th").all_text_contents()]
    cells = table.locator("tbody td")
    expect(cells.nth(columns.index("Device Type"))).to_have_text(fields.get("device_type", "Any device"))
    expect(cells.nth(columns.index("Platform"))).to_have_text(fields.get("platform", "Any platform"))
    capture(name)


@pytest.mark.parametrize(
    ("name", "template", "pattern", "channels", "bay_position", "names"),
    BUILD_RULES,
    ids=[row[0] for row in BUILD_RULES],
)
def test_build_rule(page: Page, capture: Callable[..., None], name, template, pattern, channels, bay_position, names):
    open_page(page, f"{PLUGIN}/rules/test/")
    fill_build_rule(page, template, pattern, channels, bay_position)
    page.get_by_role("button", name="Check").click()
    expect(preview_results(page)).to_have_text(names)
    capture(name)


def test_apply_rules_list(page: Page, capture: Callable[..., None]) -> None:
    open_page(page, f"{PLUGIN}/rules/apply/")
    page.locator("#rule-search").fill("ACX7024")
    rows = page.locator("#rules-table tbody tr:visible")
    expect(rows).to_have_count(6)
    page.locator("#scan-all-btn").click()
    expect(page.locator("#scan-progress")).to_have_text("Done — 6 rules checked.")
    row = rows.filter(has=page.get_by_text(APPLY_RULE["pattern"], exact=True))
    expect(row.locator(".applicable-cell")).to_have_text("Yes")
    capture("apply-rules-list")


def test_apply_preview(page: Page, api: NetBoxAPI, capture: Callable[..., None]) -> None:
    rule = api.rule(**APPLY_RULE)
    open_page(page, f"{PLUGIN}/rules/{rule['id']}/apply/")
    rows = page.locator("#apply-form tbody tr")
    expect(rows.locator("td:nth-child(4)")).to_have_text([current for current, _new in APPLY_RENAMES])
    expect(rows.locator("td:nth-child(6)")).to_have_text([new for _current, new in APPLY_RENAMES])
    capture("apply-preview")


def test_vc_rules_list(page: Page, capture: Callable[..., None]) -> None:
    open_rule_list(page, "applies_to_device_interfaces=true")
    expect(page.locator(ROWS)).to_have_count(5)
    capture("vc-rules-list")


@pytest.mark.parametrize(("name", "member", "names"), VC_MEMBERS, ids=[row[0] for row in VC_MEMBERS])
def test_vc_member_interfaces(page: Page, api: NetBoxAPI, capture: Callable[..., None], name, member, names):
    device = api.one("dcim/devices", name=member)
    open_page(page, f"/dcim/devices/{device['id']}/interfaces/")
    assert interface_names(page, len(names)) == sorted(names)
    capture(name)
