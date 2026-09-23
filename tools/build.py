#!/usr/bin/env python3
"""Build the vmlab zipapp and the skill package: python3 tools/build.py [--out dist/vmlab.pyz]

The skill lands next to the zipapp, in skill/vmlab/: the documents from skill/ plus
the zipapp as scripts/vmlab.pyz. Install it by copying that folder into an agent's
skills folder (e.g. ~/.claude/skills/).
"""

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def build(out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        shutil.copytree(
            REPO / "src" / "vmlab",
            Path(staging) / "vmlab",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        zipapp.create_archive(
            staging, target=out, interpreter="/usr/bin/env python3", main="vmlab.cli:_zipapp_main"
        )
    return out


def build_skill(pyz):
    skill = Path(pyz).parent / "skill" / "vmlab"
    if skill.exists():
        shutil.rmtree(skill)
    shutil.copytree(REPO / "skill", skill)
    (skill / "scripts").mkdir()
    shutil.copy2(pyz, skill / "scripts" / "vmlab.pyz")
    return skill


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(REPO / "dist" / "vmlab.pyz"))
    pyz = build(parser.parse_args().out)
    print(pyz)
    print(build_skill(pyz))
