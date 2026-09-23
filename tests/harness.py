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


# Tests never reach the Host's real hypervisors (`vmlab clean` would list, and could
# delete, their VMs) or its Keychain; a test that needs one passes a scripted stand-in instead.
NO_HYPERVISORS = {name: "/nonexistent/" + name for name in ("VMLAB_TART", "VMLAB_VMRUN", "VMLAB_VMCLI", "VMLAB_SECURITY")}


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
        self.fake_user_home = self.root / "_user_home"  # HOME, so tests never see the real ~/.claude
        self.fake_user_home.mkdir()
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

    def vmlab(self, *args, cwd=None, timeout=60, pyz=None, env=None, bare=False):
        proc = self.vmlab_background(*args, cwd=cwd, pyz=pyz, env=env, bare=bare)
        try:
            proc.stdout, proc.stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        return Result(proc)

    def vmlab_background(self, *args, cwd=None, pyz=None, env=None, bare=False):
        """Start vmlab without waiting for it; the caller collects it with communicate().

        bare: only PATH from this process's environment, as a terminal or CI job without an agent would have.
        """
        inherited = {"PATH": os.environ["PATH"]} if bare else os.environ
        env = dict(inherited, VMLAB_HOME=str(self.home), HOME=str(self.fake_user_home), **dict(NO_HYPERVISORS, **(env or {})))
        return subprocess.Popen(
            [sys.executable, str(pyz or zipapp_path())] + [str(a) for a in args],
            cwd=str(cwd or self.root / "app"),
            env=env,
            stdin=subprocess.DEVNULL,  # never a terminal: vmlab must not wait for an answer
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def vmlab_vendored(self, *args, **kwargs):
        """Run the project's own copy, .vmlab/vmlab.pyz, as CI would."""
        return self.vmlab(*args, pyz=self.dir / "vmlab.pyz", **kwargs)

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
