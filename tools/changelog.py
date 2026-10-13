#!/usr/bin/env python3
"""Print a version's release notes from CHANGELOG.md: python3 tools/changelog.py VERSION

The notes are the section under the heading `## VERSION`, each wrapped bullet joined back
onto one line, since a GitHub Release shows every line break. Exits 1 when it is missing or empty.
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(version, text):
    match = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(version), text, re.M | re.S)
    return re.sub(r"\n[ \t]+", " ", match.group(1)).strip() if match else ""


if __name__ == "__main__":
    notes = section(sys.argv[1], CHANGELOG.read_text())
    if not notes:
        sys.exit("CHANGELOG.md has no notes under `## %s`" % sys.argv[1])
    print(notes)
