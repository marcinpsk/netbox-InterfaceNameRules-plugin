# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The documentation recordings, one test per workflow that a page of docs/ shows as a clip.

Each test checks the state of the view at each step, so a broken view fails the run instead of
publishing a clip of an error. The fixture of each recording undoes its change afterwards, so the
screenshots and a second run see the demo data again.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager

import pytest
from playwright.sync_api import expect

from .conftest import APPLY_RENAMES, APPLY_RULE, PLUGIN, NetBoxAPI, interface_names, preview_results
from .recording import Clip

Recorder = Callable[..., AbstractContextManager[Clip]]
#: The device that the virtual chassis recording adds to cisco-stack, at this position.
VC_DEVICE = "cisco-sw-3"
VC_POSITION = 3


@pytest.fixture
def apply_rule(api: NetBoxAPI) -> Iterator[dict]:
    """Return the rule of the Apply recording; afterwards give the interfaces of its device their old names."""
    rule = api.rule(**APPLY_RULE)
    device = api.one("dcim/devices", name="acx7024-1")
    old_names = {
        interface["id"]: interface["name"] for interface in api.list("dcim/interfaces", device_id=device["id"])
    }
    assert sorted(old_names.values()) == [current for current, _new in APPLY_RENAMES]
    try:
        yield rule
    finally:
        # The plugin does not react to an interface rename, so this only undoes the Apply.
        for pk, name in old_names.items():
            api.update("dcim/interfaces", pk, {"name": name})


@pytest.fixture
def vc_device(api: NetBoxAPI) -> Iterator[dict]:
    """Create a standalone CISCO-C9K, and delete it afterwards."""
    assert not api.list("dcim/devices", name=VC_DEVICE), f"delete the device {VC_DEVICE} of an earlier run"
    member = api.one("dcim/devices", name="cisco-sw-1")
    device = api.create(
        "dcim/devices",
        {
            "name": VC_DEVICE,
            "device_type": member["device_type"]["id"],
            "role": member["role"]["id"],
            "site": member["site"]["id"],
        },
    )
    try:
        yield device
    finally:
        api.delete("dcim/devices", device["id"])


def test_build_rule(record: Recorder) -> None:
    with record("build-rule", f"{PLUGIN}/rules/test/") as clip:
        page = clip.page
        clip.type(page.locator("#id_name_template"), "xe-0/0/{bay_position}:{channel}")
        clip.click(page.locator("#id_module_type_is_regex"))
        clip.type(page.locator("#id_module_type_pattern"), "QSFP-4X10G-.*")
        clip.type(page.locator("#id_channel_count"), "4")
        clip.type(page.locator("#id_var_bay_position"), "4")
        clip.click(page.get_by_role("button", name="Check"))
        results = preview_results(page)
        expect(results).to_have_text([f"xe-0/0/4:{channel}" for channel in range(4)])
        clip.point(results.last)


def test_apply_rules(record: Recorder, apply_rule: dict) -> None:
    with record("apply-rules", f"{PLUGIN}/rules/apply/", gif=True) as clip:
        page = clip.page
        clip.type(page.locator("#rule-search"), "ACX7024")
        row = page.locator("#rules-table tbody tr:visible").filter(
            has=page.get_by_text(APPLY_RULE["pattern"], exact=True)
        )
        clip.click(row.get_by_role("link", name="Preview & Apply"))
        checks = page.locator("#apply-form tbody .row-check")
        expect(checks).to_have_count(len(APPLY_RENAMES))
        # Leave the last interface out of the Apply.
        clip.click(checks.last)
        expect(page.locator("#apply-btn-label")).to_have_text(f"Apply Now ({len(APPLY_RENAMES) - 1} selected)")
        # The page asks to confirm the Apply.
        page.once("dialog", lambda dialog: dialog.accept())
        clip.click(page.locator("#apply-btn"))
        expect(page.locator(".toast-body", has_text="interface(s) renamed")).to_have_count(1)
        expect(checks).to_have_count(1)
        clip.click(page.locator("#apply-form tbody").get_by_role("link", name="acx7024-1"))
        clip.click(page.locator("ul.nav-tabs a", has_text="Interfaces"))
        renamed = [new for _current, new in APPLY_RENAMES[:-1]]
        assert interface_names(page, len(APPLY_RENAMES)) == sorted([APPLY_RENAMES[-1][0], *renamed])
        clip.point(page.get_by_role("link", name=renamed[0], exact=True))


def test_vc_rename(record: Recorder, vc_device: dict) -> None:
    with record("vc-rename", f"/dcim/devices/{vc_device['id']}/interfaces/") as clip:
        page = clip.page
        assert interface_names(page, 4) == [f"GigabitEthernet1/0/{i}" for i in range(1, 5)]
        clip.point(page.get_by_role("link", name="GigabitEthernet1/0/1", exact=True))
        clip.click(page.locator(f'a[href^="/dcim/devices/{vc_device["id"]}/edit/"]'))
        chassis = page.locator("#id_virtual_chassis-ts-control")
        clip.click(chassis)
        chassis.press_sequentially("cisco", delay=90)
        clip.click(page.locator("#id_virtual_chassis-ts-dropdown .option", has_text="cisco-stack"))
        clip.type(page.locator("#id_vc_position"), str(VC_POSITION))
        clip.click(page.get_by_role("button", name="Save"))
        clip.click(page.locator("ul.nav-tabs a", has_text="Interfaces"))
        renamed = [f"GigabitEthernet{VC_POSITION}/0/{i}" for i in range(1, 5)]
        expect(page.get_by_role("link", name=renamed[0], exact=True)).to_be_visible()
        assert interface_names(page, 4) == renamed
        clip.point(page.get_by_role("link", name=renamed[0], exact=True))
