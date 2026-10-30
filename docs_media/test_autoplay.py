# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The autoplay script of the documentation site, on a static page: no NetBox sign-in."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page

SCRIPT = Path(__file__).resolve().parents[1] / "docs" / "assets" / "js" / "autoplay-videos.js"
# The viewport is 600 px high: 80 px of the 200 px "partial" video (40 %) is on the screen.
PAGE = """
<style>body { margin: 0 } video { display: block; width: 200px; height: 200px }</style>
<video id="control" muted></video>
<div style="height: 320px"></div>
<video id="partial" muted></video>
<div style="height: 2000px"></div>
"""
# Record each play() and pause() call; the videos have no source, so a real play() cannot start.
SPY = """
window.calls = [];
HTMLMediaElement.prototype.play = function () { window.calls.push(`play:${this.id}`); return Promise.resolve(); };
HTMLMediaElement.prototype.pause = function () { window.calls.push(`pause:${this.id}`); };
"""

# Chromium reports isIntersecting only from the first threshold on; the specification reports any intersection.
SPEC_OBSERVER = """
window.observers = [];
window.IntersectionObserver = class {
  constructor(callback) { this.callback = callback; window.observers.push(this); }
  observe() {}
  unobserve() {}
};
"""


def open_docs_page(browser: Browser, *scripts: str) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 800, "height": 600})
    page = context.new_page()
    page.set_content("".join(f"<script>{script}</script>" for script in (SPY, *scripts)) + PAGE)
    page.add_script_tag(path=str(SCRIPT))
    yield page
    context.close()


@pytest.fixture
def docs_page(browser: Browser) -> Iterator[Page]:
    yield from open_docs_page(browser)


@pytest.fixture
def spec_page(browser: Browser) -> Iterator[Page]:
    yield from open_docs_page(browser, SPEC_OBSERVER)


def calls_after(page: Page, call: str) -> list[str]:
    """Wait until *call* is recorded, and return every call so far."""
    page.wait_for_function("call => window.calls.includes(call)", arg=call)
    return page.evaluate("window.calls")


def test_a_video_that_is_less_than_half_on_the_screen_does_not_play(docs_page: Page) -> None:
    # The first observer callback reports both videos, so the control play() means that the partial one was seen.
    assert "play:partial" not in calls_after(docs_page, "play:control")


def test_a_video_that_is_less_than_half_on_the_screen_does_not_play_by_the_specification(spec_page: Page) -> None:
    spec_page.evaluate(
        """() => window.observers[0].callback(
          [{ target: document.getElementById("partial"), isIntersecting: true, intersectionRatio: 0.4 }]
        )"""
    )

    assert spec_page.evaluate("window.calls") == ["pause:partial"]


def test_a_video_plays_when_half_of_it_comes_on_the_screen(docs_page: Page) -> None:
    calls_after(docs_page, "play:control")
    docs_page.mouse.wheel(0, 120)

    calls_after(docs_page, "play:partial")


def test_reduced_motion_turned_on_after_load_pauses_the_videos(docs_page: Page) -> None:
    calls_after(docs_page, "play:control")

    docs_page.emulate_media(reduced_motion="reduce")
    calls_after(docs_page, "pause:control")
    docs_page.evaluate("window.calls = []")
    docs_page.emulate_media(reduced_motion="no-preference")

    calls_after(docs_page, "play:control")
