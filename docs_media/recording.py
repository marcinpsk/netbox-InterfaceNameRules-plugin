# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The helpers of the documentation recordings: a visible pointer, paced steps and the encoding.

Playwright records each clip as WebM. The static ffmpeg of imageio-ffmpeg cuts the blank start and
writes an H.264 MP4, a PNG poster and, for the README, an animated GIF.
"""

import subprocess
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import Locator, Page

#: Headless Chromium draws no pointer, so each page draws one that follows the mouse events.
#: The position stays in session storage, so the pointer stays in place across a navigation.
CURSOR_SCRIPT = """addEventListener("DOMContentLoaded", () => {
    const key = "docs-media-cursor";
    const [x, y] = JSON.parse(sessionStorage.getItem(key) || "[-40, -40]");
    const cursor = document.createElement("div");
    cursor.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="30" viewBox="0 0 22 30">
        <path d="M1 1 L1 24 L7 18 L11 28 L15 26 L11 17 L19 17 Z" fill="#111" stroke="#fff" stroke-width="1.5"/></svg>`;
    Object.assign(cursor.style, {
        position: "fixed", left: "0", top: "0", zIndex: "2147483647", pointerEvents: "none",
        transform: `translate(${x}px, ${y}px)`,
    });
    const ring = document.createElement("div");
    Object.assign(ring.style, {
        position: "absolute", left: "-14px", top: "-14px", width: "28px", height: "28px",
        borderRadius: "50%", background: "rgba(13, 110, 253, 0.45)", opacity: "0",
    });
    cursor.prepend(ring);
    document.body.append(cursor);
    addEventListener("mousemove", event => {
        cursor.style.transform = `translate(${event.clientX}px, ${event.clientY}px)`;
        sessionStorage.setItem(key, JSON.stringify([event.clientX, event.clientY]));
    }, true);
    addEventListener("mousedown", () => {
        ring.animate([{opacity: 1, transform: "scale(0.6)"}, {opacity: 0, transform: "scale(1.8)"}], 450);
    }, true);
});"""

#: The pause between two steps, in seconds, so that a reader can follow each one.
STEP_PAUSE = 1.2
#: The pause on the result at the end of a clip, in seconds.
END_PAUSE = 3.0
#: The number of mouse events from one point to the next; each one draws the pointer once.
MOVE_STEPS = 30
#: The delay between two typed characters, in milliseconds.
TYPE_DELAY = 90
MP4_CRF = 28
#: The shortest and the longest clip, in seconds: a clip outside them lost its steps or its pace.
CLIP_SECONDS = (6.0, 40.0)
GIF_WIDTH = 800
GIF_FPS = 8
#: The flat colors of the UI need no more colors and no dithering, which keeps the GIF small.
GIF_COLORS = 64
#: The README GIF loads on each PyPI and GitHub view, so its size has a limit.
GIF_MAX_BYTES = 2 * 1024 * 1024
_FFMPEG_TIMEOUT = 300


class Clip:
    """Drive one recorded page at the pace of a person."""

    def __init__(self, page: Page) -> None:
        self.page = page

    def pause(self, seconds: float = STEP_PAUSE) -> None:
        self.page.wait_for_timeout(seconds * 1000)

    def point(self, target: Locator) -> None:
        """Move the pointer to the center of *target* in visible steps."""
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        assert box is not None, f"{target} is not visible"
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=MOVE_STEPS)
        self.pause(STEP_PAUSE / 2)

    def click(self, target: Locator) -> None:
        self.point(target)
        target.click()
        self.pause()

    def type(self, target: Locator, text: str) -> None:
        """Replace the value of the text field *target* one character at a time."""
        self.point(target)
        target.click()
        target.select_text()
        target.press("Backspace")
        target.press_sequentially(text, delay=TYPE_DELAY)
        self.pause()


def _ffmpeg(*arguments: str | Path) -> None:
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *map(str, arguments)],
        check=True,
        timeout=_FFMPEG_TIMEOUT,
    )


def encode(webm: Path, start: float, output: Path, gif: bool = False) -> None:
    """Write *webm* from *start* seconds as <output>.mp4 and the poster <output>.png, and as <output>.gif if asked."""
    trim = ("-ss", f"{start:.3f}", "-i", webm)
    mp4 = output.with_suffix(".mp4")
    h264 = ("-an", "-c:v", "libx264", "-preset", "slow", "-crf", str(MP4_CRF), "-pix_fmt", "yuv420p")
    _ffmpeg(*trim, *h264, "-movflags", "+faststart", mp4)
    _frames, seconds = imageio_ffmpeg.count_frames_and_secs(str(mp4))
    assert CLIP_SECONDS[0] <= seconds <= CLIP_SECONDS[1], f"{mp4.name} lasts {seconds:.1f} s, not {CLIP_SECONDS} s"
    _ffmpeg(*trim, "-frames:v", "1", output.with_suffix(".png"))
    if gif:
        palette = (
            f"fps={GIF_FPS},scale={GIF_WIDTH}:-1:flags=lanczos,split[a][b];"
            f"[a]palettegen=max_colors={GIF_COLORS}:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle"
        )
        _ffmpeg(*trim, "-vf", palette, "-loop", "0", output.with_suffix(".gif"))
        size = output.with_suffix(".gif").stat().st_size
        assert size <= GIF_MAX_BYTES, f"{output.name}.gif has {size} bytes, more than {GIF_MAX_BYTES}"
