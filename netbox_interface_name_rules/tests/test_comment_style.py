# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Reject a multi-line comment block that `comment_blocks.json` does not already record.

A run of two or more whole-line `#` comments is a block. Prose belongs in the commit message or the
pull request, and the code keeps a single line pointing at the non-obvious part. The record is a
permit list of the blocks that already existed, so adding one fails until it is deliberately recorded.

Removing a block needs no edit here: an entry with nothing left to permit grants nothing. Requiring
its removal would make every comment fix a two-file change and collide between branches.

Banners, blank `#` lines and pragmas separate rather than explain, so they neither count as a block
nor join two blocks. Migrations are excluded, matching the ruff `per-file-ignores` carve-out.
"""

import json
import pathlib
import tempfile
import tokenize
from collections import Counter

from django.test import SimpleTestCase

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
BASELINE = pathlib.Path(__file__).resolve().parent / "comment_blocks.json"

_PRAGMAS = ("# noqa", "# type:", "# ruff:", "# fmt:", "# pragma:", "# SPDX", "# Copyright")
_RULE_CHARACTERS = set("-=*_")


def _is_banner(text):
    """Return True for a `# ---` rule or a bare `#`, which separate rather than explain."""
    return not set(text.lstrip("#").strip()) - _RULE_CHARACTERS


def _own_line_comments(path):
    """Map each line number carrying a whole-line explanatory comment to its text."""
    lines = path.read_text(encoding="utf-8").splitlines()
    found = {}
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type != tokenize.COMMENT:
                continue
            row = token.start[0]
            # A trailing comment explains one statement, so it never joins the block above it.
            if not lines[row - 1].lstrip().startswith("#"):
                continue
            text = token.string.strip()
            if not _is_banner(text) and not text.startswith(_PRAGMAS):
                found[row] = text
    return found


def _blocks(path):
    """Yield every run of two or more consecutive whole-line comments, joined line by line."""
    comments = _own_line_comments(path)
    for row in sorted(comments):
        if row - 1 in comments:
            continue
        length = 0
        while row + length in comments:
            length += 1
        if length > 1:
            yield "\n".join(comments[row + offset] for offset in range(length))


def blocks_in_package():
    """Return the multi-line comment blocks the package holds, keyed by repository path."""
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        blocks = list(_blocks(path))
        if blocks:
            found[str(path.relative_to(PACKAGE))] = sorted(Counter(blocks).items())
    return found


class CommentStyleTest(SimpleTestCase):
    """A new multi-line comment block has to be recorded before it is allowed."""

    def test_no_unrecorded_multi_line_comment_block(self):
        recorded = {
            name: Counter(dict(entries))
            for name, entries in json.loads(BASELINE.read_text(encoding="utf-8")).items()
        }
        unrecorded = []
        for name, entries in blocks_in_package().items():
            permitted = recorded.get(name, Counter())
            for text, count in entries:
                if count > permitted[text]:
                    unrecorded.append(f"{name}: {text}")

        self.assertEqual(
            unrecorded,
            [],
            "Move the explanation to the commit message and keep one line, or record the block.",
        )

    def test_a_block_key_covers_every_line_of_the_run(self):
        """The key holds the whole run, so a later line cannot change without a baseline update."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "sample.py"
            path.write_text("# first line\n# second line\nvalue = 1\n", encoding="utf-8")

            self.assertEqual(list(_blocks(path)), ["# first line\n# second line"])
