#!/usr/bin/env python3
"""Rewrap Markdown: python3 tools/wrap_md.py [FILE.md ...] (default: every tracked .md)

One sentence per line; a sentence over 120 characters wraps at a phrase or a space.
Frontmatter, code blocks, tables, headings and HTML keep their lines, and no line breaks
inside inline code or before a word that would start a list, heading or quote.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

WIDTH = 120
PREFIX = re.compile(r"^(\s*(?:>\s?)*(?:(?:[-*+]|\d+[.)])\s+)?)")
DANGER = re.compile(r"^(?:[-*+]$|\d+[.)]$|#|>|<|\||=+$|-+$|```|~~~)")
ABBREV = {"e.g.", "i.e.", "vs.", "cf.", "etc.)"}


def words(text):
    out, cur, ticks = [], [], 0
    for w in text.split(" "):
        cur.append(w)
        ticks += w.count("`")
        if ticks % 2 == 0:
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return [w for w in out if w != ""]


def sentences(ws):
    out, cur = [], []
    for i, w in enumerate(ws):
        cur.append(w)
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        end = re.search(r"[.!?][)\"*_`]*$", w) and w.lower() not in ABBREV
        if nxt and end and re.match(r"[A-Z`*\[(\"'0-9]", nxt):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def wrap(ws, first, rest):
    lines, line, ind = [], [], first
    for w in ws:
        if line and len(ind) + len(" ".join(line + [w])) > WIDTH and not DANGER.match(w):
            # break after the last phrase boundary past the line's middle, if any
            cut = len(line)
            for j in range(len(line) - 1, 0, -1):
                head = " ".join(line[:j])
                if line[j - 1][-1:] in ",;:" and len(head) > WIDTH // 2 and not DANGER.match(line[j]) \
                        and head.count("(") <= head.count(")"):
                    cut = j
                    break
            lines.append(ind + " ".join(line[:cut]))
            line, ind = line[cut:], rest
        line.append(w)
    lines.append(ind + " ".join(line))
    return lines


def reflow(line):
    prefix = PREFIX.match(line).group(1)
    ws = words(line[len(prefix):])
    if not ws:
        return [line]
    hard = "  " if line.endswith("  ") else ""
    rest = re.sub(r"[^>]", " ", prefix) if ">" in prefix else " " * len(prefix)
    out = []
    for k, s in enumerate(sentences(ws)):
        out += wrap(s, prefix if k == 0 else rest, rest)
    out[-1] += hard
    return out


def fmt(text):
    out, fence, lines = [], None, text.split("\n")
    i = 0
    if lines and lines[0] == "---":
        end = lines.index("---", 1)
        out, i = lines[: end + 1], end + 1
    for line in lines[i:]:
        s = line.lstrip()
        if fence:
            out.append(line)
            if s.startswith(fence):
                fence = None
            continue
        if s.startswith(("```", "~~~")):
            fence = s[:3]
            out.append(line)
        elif not s or s.startswith(("|", "#", "<")) or line.startswith(("    ", "\t")) and not PREFIX.match(line).group(1).strip():
            out.append(line)
        else:
            out += reflow(line)
    return "\n".join(out)


if __name__ == "__main__":
    paths = sys.argv[1:] or [
        REPO / p for p in subprocess.run(["git", "ls-files", "*.md"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    ]
    for path in map(Path, paths):
        path.write_text(fmt(path.read_text()))
