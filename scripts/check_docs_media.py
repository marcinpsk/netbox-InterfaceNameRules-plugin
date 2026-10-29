#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Check that the built site and the captured screenshots and recordings match.

`mkdocs build --strict` does not fail on a missing image in raw HTML, or on a missing video. Run this check on the built site: it
reads the `<img src>`, `<video src>`, `<video poster>` and `<source src>` of every HTML page, so the
renderer has already resolved each Markdown form, code block and comment. It fails when a relative media
URL of a page, or a page or README media URL under the `site_url` of mkdocs.yml, is not a file inside the
site, and when a captured screenshot or recording is on no page.

Not checked: `srcset` candidates.
Not checked: root-relative URLs (`/...`), which depend on the `site_url` path.
The README check renders the README with Python-Markdown, which MkDocs uses, and reads the same elements.
"""

from __future__ import annotations

import argparse
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import markdown
import yaml

#: The media root, relative to the site root.
MEDIA = Path("assets")
#: The captured files that a page must show, relative to the media root.
CAPTURES = ("screenshots/*.png", "recordings/*.mp4")
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


def _site_file(site: Path, site_url: str, source: str) -> Path:
    """Return the file of the site that *source*, a URL under *site_url*, names."""
    return (site / unquote(urlsplit(source).path.removeprefix(urlsplit(site_url).path))).resolve()


def _media(site: Path, site_url: str, page: Path) -> list[tuple[str, Path]]:
    """Return each media file of *page* in the site: its URL, and its file, resolved as a browser resolves it."""
    files = []
    for source in _sources(page.read_text(encoding="utf-8")):
        url = urlsplit(source)
        if source.startswith(site_url):
            files.append((source, _site_file(site, site_url, source)))
        elif not (url.scheme or url.netloc or not url.path or url.path.startswith("/")):
            files.append((source, (page.parent / unquote(url.path)).resolve()))
    return files


def _readme_problems(site: Path, site_url: str, readme: Path) -> list[str]:
    """Return one message for each README image under the site URL that the site does not hold."""
    html = markdown.markdown(readme.read_text(encoding="utf-8"))
    found = []
    for source in _sources(html):
        if not source.startswith(site_url):
            continue
        image = _site_file(site, site_url, source)
        if not image.is_relative_to(site) or not image.is_file():
            found.append(f"missing: {source} (shown by {readme.name})")
    return found


def problems(site_dir: Path, media_dir: Path, readme: Path, config: Path) -> list[str]:
    """Return one message for each media file that the site lacks and each capture that no page shows."""
    site = site_dir.resolve()
    media = (site_dir / MEDIA).resolve()
    site_url = yaml.safe_load(config.read_text(encoding="utf-8"))["site_url"]
    found = []
    shown = set()
    for page in sorted(site_dir.rglob("*.html")):
        for source, file in _media(site, site_url, page):
            if not file.is_relative_to(site) or not file.is_file():
                found.append(f"missing: {source} (shown by {page.relative_to(site_dir).as_posix()})")
            elif file.is_relative_to(media):
                shown.add(file.relative_to(media))
    found.extend(_readme_problems(site, site_url, readme))
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
