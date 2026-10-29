#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Check that the built site and the captured screenshots and recordings match.

`mkdocs build --strict` does not fail on a missing image in raw HTML, or on a missing video. Run this
check on the built site: it reads the `<img src>`, `<video src>`, `<video poster>` and `<source src>` of
every HTML page, so the renderer has already resolved each Markdown form, code block and comment.

It fails when a media URL of a page is not a file inside the site. A relative URL resolves against its
page, and a root-relative URL (`/...`) or a URL under the `site_url` of mkdocs.yml (compared after
decoding) resolves against the site path. It also fails when a README media URL under the `site_url` is
not in the site, and when a page or the README shows no captured file: a screenshot, a recording, its
poster or its GIF.

Not checked: `srcset` candidates.
The README check renders the README as CommonMark, as GitHub and PyPI do, and reads the same elements.
"""

from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import SplitResult, unquote, urlsplit

import yaml
from markdown_it import MarkdownIt

#: The media root, relative to the site root.
MEDIA = Path("assets")
#: The captured files that a page must show, relative to the media root.
CAPTURES = ("screenshots/*.png", "recordings/*.mp4", "recordings/*.png", "recordings/*.gif")
#: The attributes that name a media file, for each element.
_MEDIA_ATTRIBUTES = {"img": ("src",), "video": ("src", "poster"), "source": ("src",)}


class _MediaSources(HTMLParser):
    """Collect the media URL of each element; a comment is not an element."""

    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = _MEDIA_ATTRIBUTES.get(tag, ())
        self.sources.extend(value for name, value in attrs if name in names and value)


def _sources(html: str) -> list[str]:
    parser = _MediaSources()
    parser.feed(html)
    return parser.sources


def _site_file(site: Path, site_path: str, path: str) -> Path | None:
    """Return the file of the site that the URL *path* names, or None when the path is outside *site_path*."""
    path = unquote(path)
    if not path.startswith(site_path):
        return None
    return (site / path.removeprefix(site_path)).resolve()


def _under_site_url(url: SplitResult, site_url: SplitResult) -> bool:
    """Return whether the absolute *url* names the host of the site and, once decoded, a path under the site."""
    same_host = url.scheme in ("", site_url.scheme) and url.netloc.lower() == site_url.netloc.lower()
    return bool(url.netloc) and same_host and unquote(url.path).startswith(site_url.path)


def _is_site_file(site: Path, file: Path | None) -> bool:
    return file is not None and file.is_relative_to(site) and file.is_file()


def _media(site: Path, site_url: str, page: Path) -> list[tuple[str, Path | None]]:
    """Return each media file of *page* in the site: its URL, and its file, resolved as a browser resolves it."""
    site_parts = urlsplit(site_url)
    files = []
    for source in _sources(page.read_text(encoding="utf-8")):
        url = urlsplit(source)
        if _under_site_url(url, site_parts) or (url.path.startswith("/") and not (url.scheme or url.netloc)):
            files.append((source, _site_file(site, site_parts.path, url.path)))
        elif not (url.scheme or url.netloc or not url.path):
            files.append((source, (page.parent / unquote(url.path)).resolve()))
    return files


def _readme_media(site: Path, site_url: str, readme: Path) -> list[tuple[str, Path | None]]:
    """Return each README media URL under the site URL, and the file of the site that it names."""
    html = MarkdownIt("commonmark").render(readme.read_text(encoding="utf-8"))
    site_parts = urlsplit(site_url)
    return [
        (source, _site_file(site, site_parts.path, urlsplit(source).path))
        for source in _sources(html)
        if _under_site_url(urlsplit(source), site_parts)
    ]


def problems(site_dir: Path, media_dir: Path, readme: Path, config: Path) -> list[str]:
    """Return one message for each media file that the site lacks and each capture that nothing shows."""
    site = site_dir.resolve()
    media = (site_dir / MEDIA).resolve()
    site_url = yaml.safe_load(config.read_text(encoding="utf-8"))["site_url"]
    shown_by = [
        (page.relative_to(site_dir).as_posix(), source, file)
        for page in sorted(site_dir.rglob("*.html"))
        for source, file in _media(site, site_url, page)
    ]
    shown_by += [(readme.name, source, file) for source, file in _readme_media(site, site_url, readme)]
    found = []
    shown = set()
    for name, source, file in shown_by:
        if not _is_site_file(site, file):
            found.append(f"missing: {source} (shown by {name})")
        elif file.is_relative_to(media):
            shown.add(file.relative_to(media))
    captured = {file.relative_to(media_dir) for pattern in CAPTURES for file in media_dir.glob(pattern)}
    found.extend(f"unreferenced: {file.as_posix()}" for file in sorted(captured - shown))
    return found


def main(argv: list[str] | None = None) -> int:
    """Print each problem and return 1 when there is one."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("site_dir", type=Path, help="the built site, for example site")
    parser.add_argument("media_dir", type=Path, help="the captured media root, docs/assets")
    parser.add_argument("--readme", type=Path, default=Path("README.md"), help="the README (default: README.md)")
    parser.add_argument("--config", type=Path, default=Path("mkdocs.yml"), help="the site configuration (mkdocs.yml)")
    arguments = parser.parse_args(argv)
    for directory in (arguments.site_dir, arguments.media_dir):
        if not directory.is_dir():
            parser.error(f"{directory} is not a directory")
    found = problems(arguments.site_dir, arguments.media_dir, arguments.readme, arguments.config)
    if found:
        sys.stderr.write("".join(f"{message}\n" for message in found))
        return 1
    count = sum(1 for pattern in CAPTURES for _file in arguments.media_dir.glob(pattern))
    sys.stdout.write(f"Each local media file of the site exists, and each of the {count} captures is on a page.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
