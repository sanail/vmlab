"""Seam 2: the Provider/Channel contract, checked against one Lab through the CLI.

By default it runs against a Fake Lab (and so is part of the normal test run).
To check a real Provider, describe one Lab in a TOML file, without an [.app]
table (the suite adds its own), and point the suite at it:

    VMLAB_CONTRACT_LAB_FILE=my-lab.toml VMLAB_CONTRACT_LAB=mac \\
        python3 -m unittest discover -s tests -p 'test_contract.py' -v

A real Lab runs in a project of its own under $VMLAB_HOME/contract/<lab>, so
its Guest is reused between runs and never touches your projects. Tests run
in order: up, Channels, exec, timeouts, deploy, screenshot, restore, down.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from harness import zipapp_path

FAKE_LAB = """
[labs.contract]
provider = "fake"
os = "linux"
"""


class Target:
    """The Lab under test and the throwaway project around it."""

    def __init__(self):
        lab_file = os.environ.get("VMLAB_CONTRACT_LAB_FILE")
        self.lab = os.environ.get("VMLAB_CONTRACT_LAB", "contract")
        self.env = dict(os.environ)
        if lab_file:
            home = Path(os.environ.get("VMLAB_HOME") or Path.home() / ".vmlab")
            self.root = home / "contract" / self.lab
            lab_toml = Path(lab_file).read_text(encoding="utf-8")
        else:
            self.tmp = tempfile.mkdtemp(prefix="vmlab-contract-")
            self.root = Path(self.tmp) / "project"
            self.env["VMLAB_HOME"] = str(Path(self.tmp) / "vmlab_home")
            lab_toml = FAKE_LAB
        self.scenarios = self.root / "contract-scenarios"  # outside .vmlab/scenarios: Ad-hoc runs
        (self.root / ".vmlab").mkdir(parents=True, exist_ok=True)
        self.scenarios.mkdir(exist_ok=True)
        config = self.root / ".vmlab" / "vmlab.toml"
        config.write_text(lab_toml, encoding="utf-8")
        self.os = self.status()["os"]
        (self.root / "contract-artifact.txt").write_text("contract-bytes", encoding="utf-8")
        install = (
            'Copy-Item -LiteralPath $env:VMLAB_ARTIFACT -Destination (Join-Path $HOME "contract-installed.txt")'
            if self.os == "windows"
            else 'cp "$VMLAB_ARTIFACT" ~/contract-installed.txt'
        )
        config.write_text(
            lab_toml + '\n[labs.%s.app]\nartifact = "contract-artifact.txt"\ninstall = %s\n' % (self.lab, json.dumps(install)),
            encoding="utf-8",
        )

    def vmlab(self, *args, timeout=900):
        proc = subprocess.run(
            [sys.executable, str(zipapp_path())] + [str(a) for a in args],
            cwd=str(self.root),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc

    def status(self):
        proc = self.vmlab("status", "--json")
        assert proc.returncode == 0, proc.stderr
        [row] = [r for r in json.loads(proc.stdout) if r["lab"] == self.lab]
        return row

    def scenario(self, name, body, *flags):
        """Run a Scenario as an Ad-hoc run on the Lab; return (process, its report)."""
        path = self.scenarios / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        before = set(self._run_dirs())
        proc = self.vmlab("run", path, "--lab", self.lab, *flags)
        [run_dir] = set(self._run_dirs()) - before
        return proc, json.loads((run_dir / "report.json").read_text(encoding="utf-8"))

    def _run_dirs(self):
        runs = self.root / ".vmlab" / "runs"
        return list(runs.iterdir()) if runs.exists() else []

    def cleanup(self):
        if hasattr(self, "tmp"):
            self.vmlab("down")
            shutil.rmtree(self.tmp, ignore_errors=True)


# Per-OS commands, so one Scenario covers every Guest.
COMMANDS = """
def cmd(g, posix, windows):
    return ["powershell", "-NoProfile", "-Command", windows] if g.os == "windows" else ["sh", "-c", posix]
"""


class ContractTest(unittest.TestCase):
    target = None

    @classmethod
    def setUpClass(cls):
        cls.target = Target()

    @classmethod
    def tearDownClass(cls):
        cls.target.cleanup()

    def assertPassed(self, proc, report):
        failures = [
            (s["name"], s["error"], [c for c in s["checks"] if not c["passed"]]) for s in report["scenarios"] if s["status"] != "passed"
        ]
        self.assertEqual((proc.returncode, report["status"]), (0, "passed"), "%s\n%s\n%s" % (failures, report["error"], proc.stdout))

    def test_01_up_makes_the_guest_running_and_reachable(self):
        proc = self.target.vmlab("up", self.target.lab)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.target.status()["running"])

    def test_02_every_channel_works(self):
        proc = self.target.vmlab("doctor", self.target.lab, "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        channels = [f for f in json.loads(proc.stdout) if f["check"].startswith("Channel ")]
        self.assertTrue(channels, proc.stdout)
        self.assertEqual([f for f in channels if f["status"] != "ok"], [])

    def test_03_exec_passes_stdout_stderr_exit_code_and_env(self):
        self.assertPassed(*self.target.scenario("exec.py", COMMANDS + """
def scenario(g):
    r = g.exec(cmd(g, "echo out; echo err >&2; exit 3", "Write-Output out; [Console]::Error.WriteLine('err'); exit 3"))
    g.check("stdout", r.stdout.strip() == "out", detail=repr(r.stdout))
    g.check("stderr", r.stderr.strip() == "err", detail=repr(r.stderr))
    g.check("exit code", r.code == 3, detail=r.code)
    g.check("served by a Channel", bool(r.channel))
    r = g.exec(cmd(g, 'printf %s "$CONTRACT_VAR"', "Write-Output $env:CONTRACT_VAR"), env={"CONTRACT_VAR": "a b'c"})
    g.check("env", r.stdout.strip() == "a b'c", detail=repr(r.stdout))
    r = g.exec(cmd(g, "printf %s \\"$1\\" sh 'quote \\"me\\"'", "Write-Output 'quote \\"me\\"'"))
    g.check("quoting survives", 'quote "me"' in r.stdout, detail=repr(r.stdout))
"""))

    def test_04_a_timeout_kills_the_call(self):
        started = time.time()
        proc, report = self.target.scenario("timeout.py", COMMANDS + """
def scenario(g):
    g.exec(cmd(g, "sleep 30", "Start-Sleep 30"), timeout=2)
    g.check("unreachable", True)
""")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("timed out after 2s", report["scenarios"][0]["error"])
        self.assertLess(time.time() - started, 25)

    def test_05_deploy_delivers_and_installs_the_artifact(self):
        self.assertPassed(*self.target.scenario("deploy.py", COMMANDS + """
def scenario(g):
    r = g.exec(cmd(g, "cat ~/contract-installed.txt", "Get-Content (Join-Path $HOME contract-installed.txt)"))
    g.check("installed copy has the artifact's bytes", r.stdout.strip() == "contract-bytes", detail=repr(r.stdout + r.stderr))
"""))

    def test_06_screenshot_is_a_png(self):
        proc, report = self.target.scenario("screenshot.py", """
def scenario(g):
    g.check("screenshot taken", g.screenshot("contract").endswith(".png"))
""")
        self.assertPassed(proc, report)
        [shot] = report["scenarios"][0]["screenshots"]
        self.assertTrue((Path(report["run_dir"]) / shot).read_bytes().startswith(b"\x89PNG"))

    def test_07_restore_returns_to_clean_state(self):
        self.assertPassed(*self.target.scenario("dirty.py", COMMANDS + """
def scenario(g):
    g.check("marker written", g.exec(cmd(g, "touch ~/contract-marker", "New-Item -Force (Join-Path $HOME contract-marker)")).ok)
"""))
        self.assertPassed(*self.target.scenario("clean.py", COMMANDS + """
def scenario(g):
    r = g.exec(cmd(g, "test -e ~/contract-marker", "if (Test-Path (Join-Path $HOME contract-marker)) { exit 0 } else { exit 1 }"))
    g.check("marker gone after restore", not r.ok)
""", "--fresh"))

    def test_08_down_stops_the_guest(self):
        proc = self.target.vmlab("down", self.target.lab)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.target.status()["running"])


if __name__ == "__main__":
    unittest.main()
