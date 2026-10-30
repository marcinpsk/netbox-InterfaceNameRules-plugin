# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""scripts/check_docs_media.py on a temporary built site."""

import importlib.util
import itertools
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_docs_media.py"
_spec = importlib.util.spec_from_file_location("check_docs_media", SCRIPT)
check_docs_media = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docs_media)
_PNG = b"\x89PNG\r\n\x1a\n"
_SITE_URL = "https://example.github.io/project/"


def _tree(
    root: Path,
    pages: dict[str, str],
    captured: tuple[str, ...],
    published: tuple[str, ...] | None = None,
    readme: str = "# Project\n",
    site_url: str = _SITE_URL,
):
    """Write a built site, the captured media, a README and mkdocs.yml; return the site and the media root.

    A name without a directory is a screenshot; a published file is in the site under assets/.
    """
    site, media = root / "site", root / "docs" / "assets"
    site.mkdir(parents=True, exist_ok=True)
    for name, body in pages.items():
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_text(f"<!doctype html><html><body>{body}</body></html>", encoding="utf-8")
    media.mkdir(parents=True, exist_ok=True)
    for name in captured:
        (media / _media_path(name)).parent.mkdir(parents=True, exist_ok=True)
        (media / _media_path(name)).write_bytes(_PNG)
    for name in captured if published is None else published:
        (site / "assets" / _media_path(name)).parent.mkdir(parents=True, exist_ok=True)
        (site / "assets" / _media_path(name)).write_bytes(_PNG)
    (root / "README.md").write_text(readme, encoding="utf-8")
    (root / "mkdocs.yml").write_text(f"site_name: project\nsite_url: {site_url}\n", encoding="utf-8")
    return site, media


def _media_path(name: str) -> str:
    return name if "/" in name else f"screenshots/{name}"


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


@pytest.fixture
def _check(capsys):
    def check(site: Path, media: Path) -> Result:
        returncode = check_docs_media.main(site.parent)
        captured = capsys.readouterr()
        return Result(returncode, captured.out, captured.err)

    return check


_LEASES = (
    '<img alt="Leases" src="../assets/screenshots/leases-light.png#only-light">'
    "<img alt='Leases' src='../assets/screenshots/leases-dark.png#only-dark' />"
)


def test_a_site_whose_pages_and_captures_match_passes(tmp_path: Path, _check) -> None:
    site, media = _tree(
        tmp_path,
        {
            "leases/index.html": _LEASES,
            "index.html": '<img src="assets/logo.png"><img src="https://example.com/a.png">',
        },
        ("leases-light.png", "leases-dark.png"),
    )
    (site / "assets" / "logo.png").write_bytes(_PNG)

    result = _check(site, media)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Each local media file of the site exists, and each of the 2 captures is on a page.\n"


def test_a_page_that_shows_a_screenshot_the_site_lacks_fails(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"leases/index.html": _LEASES}, ("leases-light.png",))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "missing: ../assets/screenshots/leases-dark.png#only-dark (shown by leases/index.html)\n"


def test_a_captured_screenshot_that_no_page_shows_fails(tmp_path: Path, _check) -> None:
    site, media = _tree(
        tmp_path, {"leases/index.html": _LEASES}, ("leases-light.png", "leases-dark.png", "subnets-light.png")
    )

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "unreferenced: screenshots/subnets-light.png\n"


def test_a_relative_src_resolves_against_its_own_page(tmp_path: Path, _check) -> None:
    # From a nested page, "assets/..." without "../" names a file that the site does not hold.
    page = '<img src="assets/screenshots/leases-light.png">'
    site, media = _tree(tmp_path, {"guide/leases/index.html": page}, ("leases-light.png",))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == (
        "missing: assets/screenshots/leases-light.png (shown by guide/leases/index.html)\n"
        "unreferenced: screenshots/leases-light.png\n"
    )


def test_a_wrong_src_fails_also_when_another_page_shows_the_same_capture(tmp_path: Path, _check) -> None:
    page = '<img src="assets/screenshots/shot.png">'
    site, media = _tree(tmp_path, {"index.html": page, "guide/index.html": page}, ("shot.png",))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "missing: assets/screenshots/shot.png (shown by guide/index.html)\n"


def test_a_missing_image_outside_the_screenshots_fails(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"index.html": '<img src="assets/logo.png">'}, ())

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "missing: assets/logo.png (shown by index.html)\n"


def test_an_existing_image_outside_the_site_fails(tmp_path: Path, _check) -> None:
    # The deploy uploads only the site, so a file that escapes it is missing for the reader.
    page = '<img src="assets/screenshots/shot.png"><img src="../assets/screenshots/shot.png">'
    site, media = _tree(tmp_path, {"index.html": page}, ("shot.png",))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "missing: ../assets/screenshots/shot.png (shown by index.html)\n"


def test_a_nested_page_two_levels_down_finds_its_screenshot(tmp_path: Path, _check) -> None:
    page = '<img src="../../assets/screenshots/leases-light.png">'
    site, media = _tree(tmp_path, {"guide/leases/index.html": page}, ("leases-light.png",))

    assert _check(site, media).returncode == 0


def test_an_image_inside_an_html_comment_is_not_on_the_page(tmp_path: Path, _check) -> None:
    page = '<!-- <img src="../assets/screenshots/leases-light.png"> -->'
    site, media = _tree(tmp_path, {"leases/index.html": page}, ("leases-light.png",))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "unreferenced: screenshots/leases-light.png\n"


def test_a_site_that_was_not_built_is_a_usage_error(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {}, ())
    site.rmdir()

    result = _check(site, media)

    assert result.returncode == 2
    assert result.stderr == f"{site} is not a directory: build the site first\n"


def test_the_site_directory_comes_from_mkdocs_yml(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"index.html": '<img src="assets/screenshots/shot.png">'}, ("shot.png",))
    site.rename(tmp_path / "public")
    config = tmp_path / "mkdocs.yml"
    config.write_text(config.read_text(encoding="utf-8") + "site_dir: public\n", encoding="utf-8")

    result = _check(site, media)

    assert result.returncode == 0, result.stderr


_CLIP = (
    '<video controls muted loop playsinline preload="none" poster="../assets/recordings/clip.png">'
    '<source src="../assets/recordings/clip.mp4" type="video/mp4"></video>'
)
_CLIP_FILES = ("recordings/clip.mp4", "recordings/clip.png")


def test_a_page_whose_recording_and_poster_exist_passes(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"leases/index.html": _CLIP}, _CLIP_FILES)

    result = _check(site, media)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Each local media file of the site exists, and each of the 2 captures is on a page.\n"


def test_a_captured_gif_that_no_page_and_no_readme_shows_fails(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"leases/index.html": _CLIP}, (*_CLIP_FILES, "recordings/clip.gif"))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "unreferenced: recordings/clip.gif\n"


def test_a_captured_poster_that_no_page_shows_fails(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"leases/index.html": _CLIP}, (*_CLIP_FILES, "recordings/other.png"))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "unreferenced: recordings/other.png\n"


def test_a_gif_that_only_the_readme_shows_is_on_a_page(tmp_path: Path, _check) -> None:
    readme = f"![A clip]({_SITE_URL}assets/recordings/clip.gif)\n"
    site, media = _tree(tmp_path, {"leases/index.html": _CLIP}, (*_CLIP_FILES, "recordings/clip.gif"), readme=readme)

    result = _check(site, media)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Each local media file of the site exists, and each of the 3 captures is on a page.\n"


def test_a_gif_inside_a_readme_code_block_is_not_on_a_page(tmp_path: Path, _check) -> None:
    image = f"![A clip]({_SITE_URL}assets/recordings/clip.gif)"
    blocks = {
        "tilde": f"~~~markdown\n{image}\n\n{image}\n~~~\n",
        "backtick": f"```markdown\n{image}\n\n{image}\n```\n",
        "longer-close": f"```markdown\n{image}\n\n{image}\n`````\n",
        "blockquote": f"> ```markdown\n> {image}\n>\n> {image}\n> ```\n",
    }
    for name, block in blocks.items():
        site, media = _tree(
            tmp_path / name,
            {"leases/index.html": _CLIP},
            (*_CLIP_FILES, "recordings/clip.gif"),
            readme=f"# Project\n\n{block}",
        )

        result = _check(site, media)

        assert result.returncode == 1, name
        assert result.stderr == "unreferenced: recordings/clip.gif\n", name


def test_a_root_relative_video_src_that_the_site_lacks_fails(tmp_path: Path, _check) -> None:
    page = '<video src="/project/assets/recordings/missing.mp4"></video>'
    site, media = _tree(tmp_path, {"leases/index.html": page}, ())

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "missing: /project/assets/recordings/missing.mp4 (shown by leases/index.html)\n"


def test_a_root_relative_src_under_the_site_path_shows_its_capture(tmp_path: Path, _check) -> None:
    site, media = _tree(
        tmp_path, {"guide/index.html": '<img src="/project/assets/screenshots/shot.png">'}, ("shot.png",)
    )

    result = _check(site, media)

    assert result.returncode == 0, result.stderr


def test_a_root_relative_src_with_an_encoded_site_path_shows_its_capture(tmp_path: Path, _check) -> None:
    site, media = _tree(
        tmp_path, {"guide/index.html": '<img src="/%70roject/assets/screenshots/shot.png?v=2#x">'}, ("shot.png",)
    )

    result = _check(site, media)

    assert result.returncode == 0, result.stderr


def test_an_encoded_site_url_that_the_site_lacks_fails(tmp_path: Path, _check) -> None:
    url = "https://example.github.io/%70roject/assets/missing.png?v=2#x"
    site, media = _tree(tmp_path, {"index.html": f'<img src="{url}">'}, (), readme=f"![Missing]({url})\n")

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == f"missing: {url} (shown by index.html)\nmissing: {url} (shown by README.md)\n"


def test_a_root_relative_src_outside_the_site_path_fails(tmp_path: Path, _check) -> None:
    # The deploy publishes the site under its path only, so another path of the host is not in it.
    site, media = _tree(tmp_path, {"index.html": '<img src="/other/assets/logo.png">'}, ())
    (site / "other" / "assets").mkdir(parents=True)
    (site / "other" / "assets" / "logo.png").write_bytes(_PNG)

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "missing: /other/assets/logo.png (shown by index.html)\n"


def test_a_video_src_that_the_site_lacks_fails(tmp_path: Path, _check) -> None:
    page = '<video src="assets/recordings/clip.mp4" poster="../assets/recordings/clip.png"></video>'
    site, media = _tree(tmp_path, {"leases/index.html": page}, _CLIP_FILES)

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == (
        "missing: assets/recordings/clip.mp4 (shown by leases/index.html)\nunreferenced: recordings/clip.mp4\n"
    )


def test_a_video_poster_that_the_site_lacks_fails(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"leases/index.html": _CLIP}, _CLIP_FILES, published=("recordings/clip.mp4",))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == (
        "missing: ../assets/recordings/clip.png (shown by leases/index.html)\nunreferenced: recordings/clip.png\n"
    )


def test_a_video_source_that_the_site_lacks_fails(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"leases/index.html": _CLIP}, _CLIP_FILES, published=("recordings/clip.png",))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == (
        "missing: ../assets/recordings/clip.mp4 (shown by leases/index.html)\nunreferenced: recordings/clip.mp4\n"
    )


def test_a_captured_recording_that_no_page_shows_fails(tmp_path: Path, _check) -> None:
    site, media = _tree(tmp_path, {"leases/index.html": _CLIP}, (*_CLIP_FILES, "recordings/other.mp4"))

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == "unreferenced: recordings/other.mp4\n"


def test_a_page_image_under_the_site_url_that_the_site_lacks_fails(tmp_path: Path, _check) -> None:
    page = f'<img src="{_SITE_URL}assets/missing.png">'
    site, media = _tree(tmp_path, {"guide/index.html": page}, ())

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == f"missing: {_SITE_URL}assets/missing.png (shown by guide/index.html)\n"


def test_a_page_image_under_the_site_url_shows_its_capture(tmp_path: Path, _check) -> None:
    page = f'<img src="{_SITE_URL}assets/screenshots/shot.png"><img src="https://example.github.io/other/a.png">'
    site, media = _tree(tmp_path, {"guide/index.html": page}, ("shot.png",))

    result = _check(site, media)

    assert result.returncode == 0, result.stderr


def test_a_readme_image_under_the_site_url_that_the_site_holds_passes(tmp_path: Path, _check) -> None:
    readme = f"![A clip]({_SITE_URL}assets/recordings/clip.gif)\n![Badge](https://img.example.com/badge.svg)\n"
    site, media = _tree(tmp_path, {}, ("recordings/clip.gif",), readme=readme)

    assert _check(site, media).returncode == 0


def test_a_readme_image_under_the_site_url_that_the_site_lacks_fails(tmp_path: Path, _check) -> None:
    readme = f'![A clip]({_SITE_URL}assets/recordings/clip.gif "Sync")\n<img src="{_SITE_URL}assets/logo.png">\n'
    site, media = _tree(tmp_path, {}, (), readme=readme)

    result = _check(site, media)

    assert result.returncode == 1
    assert result.stderr == (
        f"missing: {_SITE_URL}assets/recordings/clip.gif (shown by README.md)\n"
        f"missing: {_SITE_URL}assets/logo.png (shown by README.md)\n"
    )


def test_a_readme_image_url_keeps_its_balanced_parentheses(tmp_path: Path, _check) -> None:
    readme = f"![Demo]({_SITE_URL}assets/demo(1).png)\n![Clip](<{_SITE_URL}assets/recordings/my clip.gif>)\n"
    site, media = _tree(tmp_path, {}, ("recordings/my clip.gif",), readme=readme)
    (site / "assets" / "demo(1).png").write_bytes(_PNG)

    result = _check(site, media)

    assert result.returncode == 0, result.stderr


def test_a_readme_image_inside_an_html_comment_is_not_checked(tmp_path: Path, _check) -> None:
    readme = f"<!--\n![A clip]({_SITE_URL}assets/recordings/old.gif)\n-->\n# Project\n"
    site, media = _tree(tmp_path, {}, (), readme=readme)

    result = _check(site, media)

    assert result.returncode == 0, result.stderr


# Equal spellings of the site URL https://example.github.io/my project/: default port, host case, percent-encoding.
_SITE_URL_SPELLINGS = (
    "https://example.github.io/my%20project/",
    "https://example.github.io:443/my%20project/",
    "https://Example.GitHub.io/%6Dy%20project/",
)
_MEDIA_URL_SPELLINGS = (
    *(f"{url}assets/screenshots/shot.png" for url in _SITE_URL_SPELLINGS),
    "//example.github.io/my%20project/assets/screenshots/shot.png",
    "/my%20project/assets/screenshots/sho%74.png",
)


@pytest.mark.parametrize(("site_url", "src"), itertools.product(_SITE_URL_SPELLINGS, _MEDIA_URL_SPELLINGS))
def test_each_spelling_of_the_site_url_finds_the_capture_or_reports_it_missing(
    tmp_path: Path, _check, site_url: str, src: str
) -> None:
    readme = "# Project\n" if src.startswith("/") and not src.startswith("//") else f"![Shot]({src})\n"
    pages = {"guide/index.html": f'<img src="{src}">'}
    shown = _tree(tmp_path / "shown", pages, ("shot.png",), readme=readme, site_url=site_url)
    lacking = _tree(tmp_path / "lacking", pages, ("other.png",), (), readme=readme, site_url=site_url)

    assert _check(*shown).returncode == 0, _check(*shown).stderr
    assert f"missing: {src} (shown by guide/index.html)\n" in _check(*lacking).stderr


@pytest.mark.parametrize("port", ["8443", "0"])
def test_an_image_on_another_port_of_the_site_host_is_not_in_the_site(tmp_path: Path, _check, port: str) -> None:
    readme = f"![Shot](https://example.github.io:{port}/project/assets/missing.png)\n"
    site, media = _tree(tmp_path, {}, (), readme=readme)

    assert _check(site, media).returncode == 0
