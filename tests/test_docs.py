"""The repository's Markdown reads in any editor and diffs by sentence: no prose line over 120 characters."""

import subprocess
import unittest

from harness import REPO

WIDTH = 120


def long_lines(text):
    fence, lines = None, text.split("\n")
    start = lines.index("---", 1) + 1 if lines[0] == "---" else 0
    for n, line in enumerate(lines[start:], start + 1):
        s = line.lstrip()
        if fence:
            fence = None if s.startswith(fence) else fence
        elif s.startswith(("```", "~~~")):
            fence = s[:3]
        elif len(line) > WIDTH and not s.startswith("|") and " " in s:  # a table row or one long word cannot wrap
            yield n, line


class DocsTest(unittest.TestCase):
    def test_markdown_prose_lines_fit_120_characters(self):
        docs = subprocess.run(["git", "ls-files", "*.md"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
        found = ["%s:%d: %s…" % (doc, n, line[:60]) for doc in docs for n, line in long_lines((REPO / doc).read_text())]
        self.assertEqual(found, [], "python3 tools/wrap_md.py rewraps them")


if __name__ == "__main__":
    unittest.main()
