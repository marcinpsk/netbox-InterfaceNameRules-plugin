# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Sign-in, REST API, capture and recording fixtures of the documentation media suite.

The suite drives a running NetBox that holds the demo data of scripts/load_demo_data.py (pass its URL
with --base-url). It writes each screenshot in the light and the dark color mode of NetBox to
docs/assets/screenshots/, and each recording to docs/assets/recordings/. Each recording undoes its
change, so the suite can run again on the same database.
"""

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import APIRequestContext, Browser, BrowserContext, Locator, Page, Playwright, expect

from .recording import CURSOR_SCRIPT, END_PAUSE, Clip, encode

MEDIA_DIR = Path(__file__).resolve().parents[1] / "docs" / "assets"
PLUGIN = "/plugins/interface-name-rules"
#: The superuser that the documentation build creates.
CREDENTIALS = ("admin", "admin")
ROWS = "#object_list table > tbody > tr"
#: The rule that Apply Rules previews on the demo ACX7024, and the (current, new) interface names.
APPLY_RULE = {"pattern": "QSFP-100G-.*", "device_type": "ACX7024"}
APPLY_RENAMES = [(str(bay), f"et-0/0/{bay}") for bay in range(4)]
VIEWPORT = {"width": 1440, "height": 900}
COLOR_MODES = ("light", "dark")
#: A neutral fill for a masked value in each color mode.
_MASK_COLORS = {"light": "#e9ecef", "dark": "#2c3036"}
#: NetBox keeps the color mode in local storage and on the html element (setmode.js, colorMode.ts).
_SET_COLOR_MODE = """mode => {
    localStorage.setItem("netbox-color-mode", mode);
    document.documentElement.setAttribute("data-bs-theme", mode);
}"""
#: The elements whose text differs on each run: the object identifier, the timestamps, and the time and host
#: name in the footer. No image may show them.
RUN_VALUES = ".breadcrumb + code, li:has(> .mdi-calendar-plus), li:has(> .mdi-calendar-edit), #footer-stamp"
_HIDE_RUN_VALUES = f"{RUN_VALUES} {{ visibility: hidden !important; }}"
#: Remove the sticky top bar of a screenshot, which covers a scrolled element, and hide the values of the run.
_PREPARE_PAGE = (
    """() => {
    const style = document.createElement("style");
    style.textContent = `header.navbar.sticky-top { display: none !important; } """
    + _HIDE_RUN_VALUES
    + """`;
    document.head.append(style);
}"""
)
#: Hide the values of the run on each page of a recording, from its first frame.
_HIDE_RUN_VALUES_SCRIPT = (
    """(() => {
    const style = document.createElement("style");
    style.textContent = `"""
    + _HIDE_RUN_VALUES
    + """`;
    const add = () => {
        if (!document.documentElement) return false;
        document.documentElement.append(style);
        return true;
    };
    // The script can run before the html element exists.
    if (!add()) new MutationObserver((_records, observer) => add() && observer.disconnect()).observe(document, {childList: true});
})();"""
)
_NEXT_PAINT = "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
_BODY_LUMINANCE = """() => {
    const [r, g, b] = getComputedStyle(document.body).backgroundColor.match(/\\d+/g).map(Number);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}"""


class NetBoxAPI:
    """The REST API of NetBox, signed in with the session of the suite."""

    def __init__(self, request: APIRequestContext) -> None:
        self.request = request

    def _json(self, response, expected: int) -> dict:
        assert response.status == expected, f"{response.url} answered {response.status}: {response.text()}"
        return response.json() if response.status != 204 else {}

    def list(self, endpoint: str, **filters: str) -> list[dict]:
        response = self.request.get(f"/api/{endpoint}/", params={**filters, "limit": 1000})
        return self._json(response, 200)["results"]

    def one(self, endpoint: str, **filters: str) -> dict:
        (found,) = self.list(endpoint, **filters)
        return found

    def create(self, endpoint: str, data: dict) -> dict:
        return self._json(self.request.post(f"/api/{endpoint}/", data=data), 201)

    def update(self, endpoint: str, pk: int, data: dict) -> dict:
        return self._json(self.request.patch(f"/api/{endpoint}/{pk}/", data=data), 200)

    def delete(self, endpoint: str, pk: int) -> None:
        self._json(self.request.delete(f"/api/{endpoint}/{pk}/"), 204)

    def rule(self, pattern: str, device_type: str | None = None, platform: str | None = None) -> dict:
        """Return the one module rule with the regex *pattern*, scoped to the device type and platform names."""
        filters = {"module_type_pattern": pattern, "applies_to_device_interfaces": "false"}
        if device_type:
            filters["device_type_id"] = self.one("dcim/device-types", model=device_type)["id"]
        if platform:
            filters["platform_id"] = self.one("dcim/platforms", name=platform)["id"]
        rules = self.list("plugins/interface-name-rules/rules", **filters)
        # The pattern filter matches a part of the pattern, and an empty scope filter matches each scope.
        (found,) = [
            rule
            for rule in rules
            if rule["module_type_pattern"] == pattern
            and rule["device_type"] == filters.get("device_type_id")
            and rule["platform"] == filters.get("platform_id")
        ]
        return found


@pytest.fixture(scope="session")
def signed_in_state(browser: Browser, base_url: str) -> dict:
    """Sign in once, and return the storage state that every context of the suite starts with."""
    context = browser.new_context(base_url=base_url)
    page = context.new_page()
    page.goto("/login/")
    page.get_by_label("Username").fill(CREDENTIALS[0])
    page.get_by_label("Password").fill(CREDENTIALS[1])
    page.get_by_role("button", name="Sign In").click()
    expect(page).not_to_have_url(f"{base_url}/login/")
    state = context.storage_state()
    context.close()
    return state


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict, signed_in_state: dict) -> dict:
    """Fix the viewport, the pixel ratio and the locale, so that each run draws the same image."""
    return {
        **browser_context_args,
        "storage_state": signed_in_state,
        "viewport": VIEWPORT,
        "device_scale_factor": 1,
        "locale": "en-US",
        "timezone_id": "UTC",
        "reduced_motion": "reduce",
    }


@pytest.fixture(scope="session")
def api(playwright: Playwright, base_url: str, signed_in_state: dict) -> Iterator[NetBoxAPI]:
    # Django accepts a session write when the X-CSRFToken header repeats the csrftoken cookie.
    token = next(cookie["value"] for cookie in signed_in_state["cookies"] if cookie["name"] == "csrftoken")
    request = playwright.request.new_context(
        base_url=base_url, storage_state=signed_in_state, extra_http_headers={"X-CSRFToken": token}
    )
    yield NetBoxAPI(request)
    request.dispose()


@pytest.fixture(scope="session")
def screenshots_dir() -> Path:
    directory = MEDIA_DIR / "screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture(scope="session")
def recordings_dir() -> Path:
    directory = MEDIA_DIR / "recordings"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def open_page(page: Page, url: str) -> None:
    response = page.goto(url)
    assert response is not None and response.ok, f"{url} answered {response and response.status}"
    page.wait_for_load_state("networkidle")


def preview_results(page: Page) -> Locator:
    """Return the result names of the Manual Variable Preview of Build Rule."""
    card = page.locator(".card").filter(has=page.get_by_role("heading", name="Manual Variable Preview"))
    return card.locator("tbody tr td:last-child code")


def interface_names(page: Page, count: int) -> list[str]:
    """Return the sorted names of the interface table, when it shows *count* rows."""
    names = page.locator(f"{ROWS} > td:nth-child(2) > a")
    expect(names).to_have_count(count)
    return sorted(name.strip() for name in names.all_inner_texts())


def assert_run_values_hidden(page: Page) -> None:
    values = page.locator(RUN_VALUES)
    for index in range(values.count()):
        expect(values.nth(index)).to_be_hidden()


@pytest.fixture
def capture(page: Page, screenshots_dir: Path) -> Callable[..., None]:
    """Return a function that writes one view as <name>-light.png and <name>-dark.png.

    The image shows the page content without the navigation, unless the caller names another element.
    """

    def _capture(name: str, target: Locator | None = None, mask: Sequence[Locator] = ()) -> None:
        page.wait_for_load_state("networkidle")
        page.evaluate(_PREPARE_PAGE)
        # Keep the pointer off the content, so that no row or button shows its hover state.
        page.mouse.move(0, 0)
        assert_run_values_hidden(page)
        target = target or page.locator("#page-content")
        luminance = {}
        for mode in COLOR_MODES:
            page.emulate_media(color_scheme=mode)
            page.evaluate(_SET_COLOR_MODE, mode)
            expect(page.locator("html")).to_have_attribute("data-bs-theme", mode)
            luminance[mode] = page.evaluate(_BODY_LUMINANCE)
            target.screenshot(
                path=screenshots_dir / f"{name}-{mode}.png",
                animations="disabled",
                caret="hide",
                mask=list(mask),
                mask_color=_MASK_COLORS[mode],
            )
        assert luminance["dark"] < luminance["light"], f"{name}: the dark image is not darker: {luminance}"
        light, dark = (screenshots_dir / f"{name}-{mode}.png" for mode in COLOR_MODES)
        assert light.read_bytes() != dark.read_bytes(), f"{name}: the light and the dark image are the same"

    return _capture


@pytest.fixture
def record(
    new_context: Callable[..., BrowserContext], output_path: str, recordings_dir: Path, base_url: str
) -> Callable[..., AbstractContextManager[Clip]]:
    """Return a context manager that records one workflow from *url* as <name>.mp4 with the poster <name>.png.

    The clip starts on the rendered start page: the recorded context starts signed in, and the
    encoding cuts the time until the start page is ready. A failed step leaves the WebM in test-results.
    """

    @contextmanager
    def _record(name: str, url: str, gif: bool = False) -> Iterator[Clip]:
        context = new_context(
            color_scheme="light", record_video_dir=Path(output_path) / "video", record_video_size=VIEWPORT
        )
        context.add_init_script(script=CURSOR_SCRIPT)
        context.add_init_script(script=_HIDE_RUN_VALUES_SCRIPT)
        recorded = context.new_page()
        # The video timeline starts with the screencast of the page.
        started = time.monotonic()
        open_page(recorded, url)
        expect(recorded.locator("html")).to_have_attribute("data-bs-theme", "light")
        # Chromium moves the pointer of a new page to the top left corner, so start it in the middle.
        recorded.mouse.move(VIEWPORT["width"] / 2, VIEWPORT["height"] / 2)
        # Wait until the browser paints the ready page, so that the cut does not show a partly drawn page.
        recorded.evaluate(_NEXT_PAINT)
        start = time.monotonic() - started
        assert_run_values_hidden(recorded)
        clip = Clip(recorded)
        clip.pause()
        yield clip
        assert_run_values_hidden(recorded)
        clip.pause(END_PAUSE)
        video = recorded.video
        assert video is not None, "the recorded context records no video"
        context.close()
        encode(Path(video.path()), start, recordings_dir / name, gif=gif)

    return _record
