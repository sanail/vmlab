"""Seam 1 harness: drive the built vmlab zipapp as a subprocess against a temp project.

Tests only use what a user or CI would see: exit codes, stdout/stderr and the
files vmlab writes into the project. Nothing here imports vmlab itself.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_BUILD_DIR = None


def zipapp_path():
    """Build the zipapp once per test process with the documented build command."""
    global _BUILD_DIR
    if _BUILD_DIR is None:
        _BUILD_DIR = Path(tempfile.mkdtemp(prefix="vmlab-build-"))
        subprocess.run(
            [sys.executable, str(REPO / "tools" / "build.py"), "--out", str(_BUILD_DIR / "vmlab.pyz")],
            check=True,
            capture_output=True,
        )
    return _BUILD_DIR / "vmlab.pyz"


class Result:
    def __init__(self, proc):
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    def __repr__(self):
        return "Result(code=%r)\n--- stdout\n%s\n--- stderr\n%s" % (self.code, self.out, self.err)


class Project:
    """A throwaway project directory with its own vmlab home."""

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "_vmlab_home"
        self.dir = self.root / "app" / ".vmlab"
        self.dir.mkdir(parents=True)

    @property
    def config_path(self):
        return self.dir / "vmlab.toml"

    def config(self, text):
        self.config_path.write_text(textwrap.dedent(text))
        return self

    def scenario(self, name, body):
        path = self.dir / "scenarios" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))
        return path

    def vmlab(self, *args, cwd=None, timeout=60):
        env = dict(os.environ, VMLAB_HOME=str(self.home))
        proc = subprocess.run(
            [sys.executable, str(zipapp_path())] + [str(a) for a in args],
            cwd=str(cwd or self.root / "app"),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return Result(proc)

    def run_dirs(self):
        runs = self.dir / "runs"
        return sorted(p for p in runs.iterdir() if p.is_dir()) if runs.exists() else []

    def only_run_dir(self):
        dirs = self.run_dirs()
        assert len(dirs) == 1, "expected exactly one run folder, found %r" % dirs
        return dirs[0]

    def report(self, run_dir=None):
        return json.loads(((run_dir or self.only_run_dir()) / "report.json").read_text())


FAKE_LAB = """
[labs.mac]
provider = "fake"
os = "macos"
arch = "arm64"
"""


class VmlabTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="vmlab-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.project = Project(tmp)

    def assertExit(self, result, code):
        self.assertEqual(result.code, code, repr(result))
