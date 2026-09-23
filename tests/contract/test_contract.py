"""Seam 2: the Provider/Channel contract, checked against one Lab through the CLI.

By default it runs against a Fake Lab (and so is part of the normal test run).
To check a real Provider, describe one Lab in a TOML file, without an [.app]
table (the suite adds its own), and point the suite at it:

    VMLAB_CONTRACT_LAB_FILE=my-lab.toml VMLAB_CONTRACT_LAB=mac \\
        python3 -m unittest discover -s tests -p 'test_contract.py' -v

A real Lab runs in a project of its own under $VMLAB_HOME/contract/<lab>, so
its Guest is reused between runs and never touches your projects. Tests run
in order: up, Channels, exec, timeouts, deploy, screenshot, the UI contract
(in the OS's stock text editor; skipped on OSes that have no UI helper yet),
restore, down.

VMLAB_UI_HELPER=jxa runs the macOS UI part through the JXA fallback. A Linux
Lab runs it in its session: run the suite once with a Wayland Lab and once
with a Lab that sets session = "x11".
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

UI_OSES = ("macos", "linux", "windows")  # Guest OSes with a UI helper (vmlab.uihelpers)

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
        status = self.status()
        self.os = status["os"]
        # The Fake Provider emulates the UI contract on every OS; real Guests have helpers per OS.
        self.has_ui = status["provider"] == "fake" or self.os in UI_OSES
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

# The UI contract: every node has these keys on every OS.
UI = COMMANDS + """
import os
import uuid

NODE_KEYS = {"role", "name", "value", "description", "bounds", "focused", "enabled", "native_role", "children"}

def walk(node):
    yield node
    for child in node["children"]:
        yield from walk(child)
"""


def ui(test):
    """A UI contract test: skipped on Guest OSes that have no UI helper yet."""

    def wrapper(self):
        if not self.target.has_ui:
            self.skipTest("the UI contract is not implemented for %s Guests yet" % self.target.os)
        test(self)

    wrapper.__name__ = test.__name__
    return wrapper


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
        findings = json.loads(proc.stdout)
        channels = [f for f in findings if f["check"].startswith("Channel ")]
        self.assertTrue(channels, proc.stdout)
        self.assertEqual([f for f in channels if f["status"] != "ok"], [])
        # The Guest's first screenshots: a consent alert here would cover the app under test in every later one.
        alerts = [f for f in findings if f["check"].startswith("Screen Recording alert")]
        self.assertEqual([f for f in alerts if f["status"] != "ok"], [], proc.stdout)

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

    @ui
    def test_07_ui_tree_and_find_share_one_shape(self):
        self.assertPassed(*self.target.scenario("ui_tree.py", UI + """
def scenario(g):
    text = "vmlab contract " + uuid.uuid4().hex[:8]
    staged = g.stage_text(text)
    g.check("the editor is frontmost when staged", staged["frontmost"] == staged["app"], detail=staged)
    g.check("the staged text is selected", staged["selected"] == text, detail=staged)
    tree = g.tree(app=staged["app"])
    bad = [n for n in walk(tree) if not NODE_KEYS <= set(n)]
    g.check("every node has the shared shape", not bad, detail=bad[:1])
    g.check("the root is the desktop", tree["role"] == "desktop", detail=tree["role"])
    g.check("the tree holds just the editor", [a["role"] for a in tree["children"]] == ["application"], detail=[a["name"] for a in tree["children"]])
    found = g.find(role="textarea", text=text, app=staged["app"])["matches"]
    g.check("the text area is found by role and text", len(found) == 1 and found[0]["value"] == text, detail=found)
    g.check("a match has no children", found and "children" not in found[0])
"""))

    @ui
    def test_08_ui_input_clipboard_click_and_wait(self):
        self.assertPassed(*self.target.scenario("ui_input.py", UI + """
def scenario(g):
    mod = "cmd" if g.os == "macos" else "ctrl"
    tag = uuid.uuid4().hex[:8]
    staged = g.stage_text("copy me " + tag, then=mod + "+c")
    app = staged["app"]
    g.check("the chord pressed in the staging call copied the selection", g.clipboard()["text"] == "copy me " + tag, detail=g.clipboard())
    g.set_clipboard("clip " + tag)
    g.check("the clipboard can be set", g.clipboard()["text"] == "clip " + tag)
    # The JXA fallback types through System Events, which follows the keyboard layout: ASCII only.
    typed = ("typed u " if os.environ.get("VMLAB_UI_HELPER") == "jxa" else "typed \u00fc ") + tag
    g.check("type reports what it typed", g.type(typed)["typed"] == len(typed))
    waited = g.wait_for(role="textarea", text=typed, app=app, timeout=10)
    g.check("typing replaced the selection", waited["met"], detail=waited)
    g.screenshot("before the click")  # evidence, should something cover the text area
    clicked = g.click(role="textarea", text=typed, app=app)
    g.check("the click landed on the text area", clicked["element"]["role"] == "textarea", detail=clicked)
    g.press(mod + "+a")
    g.press(mod + "+c")
    g.check("chords select and copy after a click", g.clipboard()["text"] == typed, detail=g.clipboard())
    g.check("wait-for a vanished element times out unmet", not g.wait_for(text="no such element " + tag, timeout=1)["met"])
    g.check("wait-for gone", g.wait_for(text="no such element " + tag, gone=True, timeout=1)["met"])
    g.exec(cmd(g, "touch ~/contract-ui-" + tag, "New-Item -Force (Join-Path $HOME contract-ui-" + tag + ")"))
    g.check("wait-for a file", g.wait_for(file="~/contract-ui-" + tag, timeout=10)["met"])
    title = staged["file"].replace("\\\\", "/").rsplit("/", 1)[-1]  # a Windows path too
    focused = g.focus(app, window=title)
    g.check("focus raises the window and fronts the app", (focused["app"], focused["frontmost"]) == (app, app) and (focused["window"] == title if g.os == "macos" else title in focused["window"]), detail=focused)
"""))

    @ui
    def test_09_ui_a_cold_webkit_page_is_read_on_the_first_try(self):
        # WebKit builds its accessibility tree lazily and hands it to the next client
        # to connect. Safari is started afresh and waited for through the window
        # server, not Accessibility, so the Scenario's first tree read is cold.
        if self.target.os != "macos":
            self.skipTest("WebKit apps are a macOS trap")
        if os.environ.get("VMLAB_UI_HELPER") == "jxa":
            self.skipTest("the JXA fallback does not wake lazy WebKit trees (documented)")
        self.assertPassed(*self.target.scenario("ui_webkit.py", UI + """
WINDOWS = (
    'ObjC.import("CoreGraphics"); '
    'const l = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly, 0))); '
    'l.filter(w => w.kCGWindowOwnerName == "Safari").map(w => w.kCGWindowName || "").join(",")'
)

def scenario(g):
    tag = uuid.uuid4().hex[:8]
    page = "<html><head><title>vmlab webkit %s</title></head><body><h1>Hello from WebKit %s</h1><button>Press %s</button></body></html>" % (tag, tag, tag)
    started = g.exec(["sh", "-c",
        'pkill -x Safari; n=0; while pgrep -x Safari >/dev/null && [ $n -lt 100 ]; do sleep 0.1; n=$((n+1)); done; '
        'printf %s "$1" > /tmp/vmlab-webkit.html && open -a Safari /tmp/vmlab-webkit.html', "sh", page])
    g.check("Safari started with the page", started.ok, detail=started.stderr)
    shown = g.exec(["sh", "-c",
        'n=0; until osascript -l JavaScript -e "$1" | grep -q "$2"; do [ $n -ge 300 ] && exit 1; sleep 0.1; n=$((n+1)); done',
        "sh", WINDOWS, "vmlab webkit " + tag])
    g.check("the page's window is on screen", shown.ok, detail=shown.stderr[-500:])
    found = g.find(text="Hello from WebKit " + tag, app="Safari")["matches"]
    g.check("the first read sees the page's text", [m["role"] for m in found] == ["heading", "text"], detail=found)
    button = g.find(role="button", text="Press " + tag, app="Safari")["matches"]
    g.check("and its button, by role", len(button) == 1, detail=button)
    g.exec(["pkill", "-x", "Safari"])
"""))

    @ui
    def test_10_ui_cli_prints_the_same_json(self):
        proc = self.target.vmlab("ui", "clipboard", "--set", "from the CLI", "--lab", self.target.lab)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"text": "from the CLI"})
        proc = self.target.vmlab("ui", "tree", "--lab", self.target.lab)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["role"], "desktop")
        proc = self.target.vmlab("ui", "press", "ctrl+nokey", "--lab", self.target.lab)
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_11_restore_returns_to_clean_state(self):
        self.assertPassed(*self.target.scenario("dirty.py", COMMANDS + """
def scenario(g):
    g.check("marker written", g.exec(cmd(g, "touch ~/contract-marker", "New-Item -Force (Join-Path $HOME contract-marker)")).ok)
"""))
        self.assertPassed(*self.target.scenario("clean.py", COMMANDS + """
def scenario(g):
    r = g.exec(cmd(g, "test -e ~/contract-marker", "if (Test-Path (Join-Path $HOME contract-marker)) { exit 0 } else { exit 1 }"))
    g.check("marker gone after restore", not r.ok)
""", "--fresh"))

    def test_12_down_stops_the_guest(self):
        proc = self.target.vmlab("down", self.target.lab)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.target.status()["running"])


if __name__ == "__main__":
    unittest.main()
