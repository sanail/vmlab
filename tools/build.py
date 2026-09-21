#!/usr/bin/env python3
"""Build the vmlab zipapp: python3 tools/build.py [--out dist/vmlab.pyz]"""

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO / "dist" / "vmlab.pyz"))
    print(build(parser.parse_args().out))
