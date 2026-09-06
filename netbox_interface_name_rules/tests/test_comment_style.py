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
    lines = path.read_text().splitlines()
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


def _block_first_lines(path):
    """Yield the first line of every run of two or more consecutive whole-line comments."""
    comments = _own_line_comments(path)
    for row in sorted(comments):
        if row - 1 in comments:
            continue
        length = 0
        while row + length in comments:
            length += 1
        if length > 1:
            yield comments[row]


def blocks_in_package():
    """Return the multi-line comment blocks the package holds, keyed by repository path."""
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        first_lines = list(_block_first_lines(path))
        if first_lines:
            found[str(path.relative_to(PACKAGE))] = sorted(Counter(first_lines).items())
    return found


class CommentStyleTest(SimpleTestCase):
    """A new multi-line comment block has to be recorded before it is allowed."""

    def test_no_unrecorded_multi_line_comment_block(self):
        recorded = {
            name: Counter({text: count for text, count in entries})
            for name, entries in json.loads(BASELINE.read_text()).items()
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
