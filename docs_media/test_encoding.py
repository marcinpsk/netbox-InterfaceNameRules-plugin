# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The encoding of a recording, on clips that ffmpeg generates: no browser and no NetBox."""

import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import pytest

from docs_media.recording import GIF_FPS, encode

SECONDS = 7
# A white page with the small noise of each frame of a WebM recording, as the WebM of Playwright has.
NOISY_PAGE = f"color=white:size=1280x800:rate=25:duration={SECONDS},format=yuv420p,noise=alls=5:allf=t"
# A blue box that comes on the page after 4 s, as a dialog does.
BOX = "drawbox=x=400:y=300:w=400:h=200:color=blue:thickness=fill:enable='gte(t,4)'"


def webm(path: Path, video_filter: str) -> Path:
    ffmpeg = (imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error")
    subprocess.run(
        [
            *ffmpeg,
            "-f",
            "lavfi",
            "-i",
            video_filter,
            "-c:v",
            "libvpx",
            "-b:v",
            "1M",
            "-deadline",
            "realtime",
            "-cpu-used",
            "8",
            str(path),
        ],
        check=True,
    )
    return path


def gif_frames(path: Path) -> list[np.ndarray]:
    reader = imageio_ffmpeg.read_frames(str(path))
    width, height = next(reader)["size"]
    return [np.frombuffer(frame, np.uint8).reshape(height, width, 3) for frame in reader]


def changed_share(frame: np.ndarray, other: np.ndarray) -> float:
    return float((frame != other).any(axis=2).mean())


def test_the_gif_of_a_page_that_does_not_change_stays_the_same(tmp_path: Path) -> None:
    encode(webm(tmp_path / "page.webm", NOISY_PAGE), 0, tmp_path / "page", gif=True)

    frames = gif_frames(tmp_path / "page.gif")
    assert len(frames) >= (SECONDS - 1) * GIF_FPS
    # Only the rare noise above the hold limit changes a pixel.
    assert max(changed_share(frame, frames[0]) for frame in frames) < 0.001


def test_the_gif_shows_a_change_of_the_page(tmp_path: Path) -> None:
    encode(webm(tmp_path / "box.webm", f"{NOISY_PAGE},{BOX}"), 0, tmp_path / "box", gif=True)

    frames = gif_frames(tmp_path / "box.gif")
    # The GIF is 800 px wide: the box is at 250 to 500 px, 187 to 312 px.
    assert frames[0][250, 375].min() > 200
    red, green, blue = frames[-1][250, 375]
    assert blue > 200
    assert max(red, green) < 80


def test_a_gif_that_ffmpeg_cannot_write_fails_the_encoding(tmp_path: Path) -> None:
    # Each write to /dev/full fails with "No space left on device".
    (tmp_path / "page.gif").symlink_to("/dev/full")
    page = webm(tmp_path / "page.webm", NOISY_PAGE)

    with pytest.raises(subprocess.CalledProcessError):
        encode(page, 0, tmp_path / "page", gif=True)
