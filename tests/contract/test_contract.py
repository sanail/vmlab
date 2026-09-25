"""Seam 2: the Provider/Channel contract, checked against one Lab through the CLI.

By default it runs against a Fake Lab (and so is part of the normal test run).
To check a real Provider, describe one Lab in a TOML file, without an [.app]
table (the suite adds its own), and point the suite at it:

    VMLAB_CONTRACT_LAB_FILE=my-lab.toml VMLAB_CONTRACT_LAB=mac \\
        python3 -m unittest discover -s tests -p 'test_contract.py' -v

A real Lab gets a project of its own under target/contract/<lab> in this
repo (ignored by git), so its Guest is reused between runs and never touches
your projects; its Base guest comes from $VMLAB_HOME as usual. Tests run
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
import uuid
from pathlib import Path

from harness import REPO, zipapp_path

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
            self.root = REPO / "target" / "contract" / self.lab
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
        self.provider = status["provider"]
        # The Fake Provider emulates the UI contract on every OS; real Guests have helpers per OS.
        self.has_ui = self.provider == "fake" or self.os in UI_OSES
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

# g.spawn: a small HTTP server under a name of its own, for wait_for(process=...): on POSIX a link
# to perl (macOS Guests have no Python without the developer tools), on Windows a copy of
# PowerShell. The Scenario sets TAG.
SPAWN = r'''
import base64
import time

PERL_SERVER = """
use IO::Socket::INET;
$| = 1;
my ($port, $tag) = @ARGV;
my $s = IO::Socket::INET->new(LocalAddr => "127.0.0.1", LocalPort => $port, Listen => 5, ReuseAddr => 1) or die "cannot listen: $!\n";
print "listening on $port\n";
while (my $c = $s->accept) {
    while (my $h = <$c>) { print "got: $h"; last if $h =~ /^\r?\n$/ }
    print $c "HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\nok $tag\n";
    close $c;
}
"""
PERL_CLIENT = 'my $c = IO::Socket::INET->new("127.0.0.1:$ARGV[0]") or exit 1; print $c "GET / HTTP/1.0\r\n\r\n"; print while <$c>'
WINDOWS_SERVER = "; ".join([
    "$l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, %d)", "$l.Start()", "'listening on %d'",
    "while ($true) { $c = $l.AcceptTcpClient(); $s = $c.GetStream(); $r = New-Object IO.StreamReader($s); "
    "do { $h = $r.ReadLine(); \"got: $h\" } while ($h); "
    "$b = [Text.Encoding]::ASCII.GetBytes(\"HTTP/1.0 200 OK`r`nContent-Type: text/plain`r`n`r`nok %s`n\"); $s.Write($b, 0, $b.Length); $c.Close() }",
])

def program(g, name):
    # The Guest path of a program called name.
    if g.os == "windows":
        script = "$p = Join-Path $env:TEMP '%s.exe'; Copy-Item -Force (Get-Command powershell.exe).Source $p; $p" % name
        return g.exec(cmd(g, "", script)).stdout.strip()
    return g.exec(["sh", "-c", 'p="${TMPDIR:-/tmp}/$1"; ln -sf "$(command -v perl)" "$p" && echo "$p"', "sh", name]).stdout.strip()

def server(g, name, port):
    # (a spawned HTTP server that answers "ok TAG", the argv that asks it)
    if g.os == "windows":
        spawned = g.spawn([program(g, name), "-NoProfile", "-Command", WINDOWS_SERVER % (port, port, TAG)])
        return spawned, ["curl.exe", "-s", "http://127.0.0.1:%d/" % port]
    # A child of the process spawned, so stopping it must end its process group.
    spawned = g.spawn(["sh", "-c", '"$@" & wait', "sh", program(g, name), "-e", PERL_SERVER, str(port), TAG])
    return spawned, ["perl", "-MIO::Socket::INET", "-e", PERL_CLIENT, str(port)]

def orphaned_server(g, name, port):
    # (a spawned process that starts an HTTP server as server() does, then exits, the argv that asks it)
    if g.os == "windows":
        script = WINDOWS_SERVER % (port, port, TAG)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        start = "Start-Process -NoNewWindow -FilePath '%s' -ArgumentList '-NoProfile','-EncodedCommand','%s'" % (program(g, name), encoded)
        return g.spawn(["powershell", "-NoProfile", "-Command", start]), ["curl.exe", "-s", "http://127.0.0.1:%d/" % port]
    spawned = g.spawn(["sh", "-c", '"$@" &', "sh", program(g, name), "-e", PERL_SERVER, str(port), TAG])
    return spawned, ["perl", "-MIO::Socket::INET", "-e", PERL_CLIENT, str(port)]

def remove_programs(g, *names):
    g.exec(cmd(g, 'cd "${TMPDIR:-/tmp}" && rm -f ' + " ".join(names),
               "Remove-Item -Force " + ", ".join("(Join-Path $env:TEMP %s.exe)" % n for n in names)))
'''


# Tray menus, against a fixture app of the Guest's own making (tray_fixture_*): the Scenario puts it
# into the Guest and starts it under a name of its own, which is the app's name. SOURCE is its source.
TRAY_FIXTURES = {"linux": "tray_fixture_linux.py", "macos": "tray_fixture_macos.swift", "windows": "tray_fixture_windows.ps1"}
TRAY = r'''
import re

NAME = "vmlab-tray-" + uuid.uuid4().hex[:4]  # 15 characters: all of it is a Linux process's name
HIDE = "Get-ChildItem 'HKCU:\\Control Panel\\NotifyIconSettings' | Where-Object { (Get-ItemProperty $_.PSPath).ExecutablePath -like '*\\%s.exe' } | ForEach-Object { Set-ItemProperty $_.PSPath IsPromoted 0 -Type DWord }"

def start(g):
    # Start the fixture; the Guest path of the file it records choices in.
    if g.os == "windows":
        record = g.put("%TEMP%\\" + NAME + ".txt", "")
        script = g.put("%TEMP%\\" + NAME + ".ps1", SOURCE)
        exe = g.exec(cmd(g, "", "$p = Join-Path $env:TEMP '%s.exe'; Copy-Item -Force (Get-Command powershell.exe).Source $p; $p" % NAME)).stdout.strip()
        g.spawn([exe, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", script, record])
        return record
    record = g.put("~/%s.txt" % NAME, "")
    exe = "/tmp/" + NAME
    if g.os == "macos":
        built = g.exec(["swiftc", "-o", exe, g.put("~/%s.swift" % NAME, SOURCE)], timeout=300)
        g.check("the fixture builds with the Guest's swiftc", built.ok, detail=built.stderr[-2000:])
        g.spawn([exe, record])
    else:
        g.exec(["sh", "-c", 'ln -sf "$(command -v python3)" "$1"', "sh", exe])
        g.spawn([exe, g.put("~/%s.py" % NAME, SOURCE), record])
    return record

def recorded(g, record):
    # The labels the fixture recorded, without mnemonics.
    return [re.sub("[_&\\ufeff]", "", line).strip() for line in g.get(record).splitlines() if line.strip()]

def refused(g, choose):
    try:
        g.tray(NAME, choose=choose)
    except Exception as exc:
        return str(exc)

def scenario(g):
    record = start(g)
    listed = g.tray(NAME, timeout=60)
    items = {i["name"]: i for i in listed["items"]}
    g.check("the Tray menu's items, in order, without the separator", list(items) == ["Open", "Settings", "Pinned", "Update"], detail=listed)
    g.check("checked items", items["Pinned"]["checked"] and not items["Open"]["checked"], detail=listed)
    g.check("a disabled item", not items["Update"]["enabled"] and items["Open"]["enabled"], detail=listed)
    submenu = items["Settings"]["children"]
    g.check("the submenu's items, or null where listing them needs a click",
            submenu is None or [(i["name"], i["checked"]) for i in submenu] == [("Advanced", False), ("Dark mode", True)], detail=submenu)
    g.check("reading chooses nothing", listed["chosen"] is None and recorded(g, record) == [], detail=[listed, recorded(g, record)])
    if g.os == "windows":
        g.exec(cmd(g, "", HIDE % NAME))  # among the hidden icons, as a new app's icon is at first
    chosen = g.tray(NAME, choose=["Settings", "Advanced"])
    g.check("a submenu's item is chosen", chosen["chosen"] == ["Settings", "Advanced"], detail=chosen)
    seen = g.wait_for(log=record, pattern="Advanced", timeout=20)
    g.check("and the app got it", seen["met"] and recorded(g, record) == ["Advanced"], detail=recorded(g, record))
    missing = refused(g, ["Settings", "Nope"])
    g.check("a missing label fails, naming the level's items", missing and "Advanced" in missing and "Dark mode" in missing, detail=missing)
    disabled = refused(g, "Update")
    g.check("a disabled item fails", disabled and "disabled" in disabled, detail=disabled)
    g.check("nothing else was chosen", recorded(g, record) == ["Advanced"], detail=recorded(g, record))
'''


# Notifications, each sent by a fixture of the Guest's own that installs nothing: macOS osascript
# (sent as Script Editor), Windows a toast under PowerShell's own AppUserModelID, Linux gdbus.
NOTIFY = r'''
POWERSHELL_AUMID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
APPS = {"macos": "com.apple.ScriptEditor2", "windows": POWERSHELL_AUMID, "linux": "vmlab-contract"}
TOAST = """param($Delay, $Title, $Body)
Start-Sleep -Seconds $Delay
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' + $Title + '</text><text>' + $Body + '</text></binding></visual></toast>')
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show([Windows.UI.Notifications.ToastNotification]::new($xml))
""" % POWERSHELL_AUMID
OSASCRIPT = ["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run"]
GDBUS = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications", "--object-path", "/org/freedesktop/Notifications",
         "--method", "org.freedesktop.Notifications.Notify", "vmlab-contract", "0", ""]

def sender(g, title, body, delay=0):
    # The argv that sends a Notification after delay seconds.
    if g.os == "windows":
        script = g.put("%TEMP%\\vmlab-toast.ps1", TOAST)
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, str(delay), title, body]
    send = OSASCRIPT + [title, body] if g.os == "macos" else GDBUS + [title, body, "[]", "{}", "5000"]
    return ["sh", "-c", 'sleep "$1"; shift; exec "$@"', "sh", str(delay)] + send

def guest_now(g):
    return float(g.exec(cmd(g, "date -u +%s", "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()")).stdout.strip())

def scenario(g):
    app, nonce = APPS[g.os], uuid.uuid4().hex[:8]
    sent = g.exec(sender(g, "vmlab contract", "sent " + nonce))
    g.check("the fixture sends a Notification", sent.ok, detail=sent.stderr[-2000:])
    posted = g.wait_for(notification="sent " + nonce, app=app, timeout=60)
    g.check("wait_for finds a Notification sent before it", posted["met"], detail=posted)
    [found] = g.notifications(text=nonce)["notifications"] or [None]
    g.check("it has its title, body and app", found and (found["title"], found["body"], found["app"].lower()) == ("vmlab contract", "sent " + nonce, app.lower()), detail=found)
    at = datetime.fromisoformat(found["time"].replace("Z", "+00:00")).timestamp() if found else 0
    g.check("its time is on the Guest's clock", abs(at - guest_now(g)) < 60, detail=[found, guest_now(g)])
    g.check("app narrows the list", g.notifications(app="com.example.none", text=nonce) == {"notifications": []})
    g.spawn(sender(g, "vmlab contract", "later " + nonce, delay=4))
    later = g.wait_for(notification="later " + nonce, app=app, timeout=60)
    g.check("wait_for is met by one sent during the wait", later["met"] and later["waited_s"] >= 3, detail=later)
    never = g.wait_for(notification="never " + nonce, timeout=3)
    g.check("one never sent is unmet at the timeout", not never["met"] and never["waited_s"] < 30, detail=never)
'''


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

    def test_06b_wait_for_a_command_and_a_process_that_quits(self):
        self.assertPassed(*self.target.scenario("wait_exec.py", COMMANDS + """
import base64
import uuid

def background(g, posix, windows):
    # Started and left running: the call returns at once.
    if g.os == "windows":
        encoded = base64.b64encode(windows.encode("utf-16-le")).decode("ascii")
        return cmd(g, "", "Start-Process -WindowStyle Hidden powershell -ArgumentList '-NoProfile','-EncodedCommand','%s'" % encoded)
    return ["sh", "-c", "nohup sh -c \\"$1\\" >/dev/null 2>&1 </dev/null &", "sh", posix]

def scenario(g):
    tag = uuid.uuid4().hex[:6]
    flag = "contract-wait-" + tag
    started = g.exec(background(g, "sleep 3; touch ~/" + flag, "Start-Sleep 3; New-Item -Force (Join-Path $HOME %s)" % flag))
    g.check("the delayed command started", started.ok, detail=started.stderr)
    probe = cmd(g, "test -e ~/" + flag + " && echo here", "if (Test-Path (Join-Path $HOME %s)) { 'here' } else { exit 1 }" % flag)
    waited = g.wait_for(exec=probe, timeout=60)
    g.check("an exec condition turns true mid-wait", waited["met"] and waited["waited_s"] >= 1 and waited["code"] == 0, detail=waited)
    g.check("its result carries the output", waited["stdout"].strip() == "here", detail=waited)
    matched = g.wait_for(exec=cmd(g, "echo ready 7; exit 2", "'ready 7'; exit 2"), pattern=r"ready \\d", timeout=10)
    g.check("a pattern decides whatever the exit code", matched["met"] and matched["code"] == 2, detail=matched)
    try:
        g.wait_for(exec=["no-such-command-vmlab-" + tag], timeout=30)
        g.check("a missing command raises", False)
    except Exception as exc:
        g.check("a missing command raises, naming it", "no-such-command-vmlab-" + tag in str(exc), detail=str(exc))
    try:  # sh's 127; PowerShell's CommandNotFoundException, whatever the Guest's language
        g.wait_for(exec=cmd(g, "no-such-command-vmlab-" + tag, "no-such-command-vmlab-" + tag), timeout=30)
        g.check("a missing command in a script raises", False)
    except Exception as exc:
        named = "no such command" in str(exc) and (g.os != "windows" or "CommandNotFoundException" in str(exc))
        g.check("a missing command in a script raises", named, detail=str(exc))
    # A process with a name of its own that quits after a few seconds: on macOS a link to sleep
    # (macOS kills a copy of a system binary), on Linux a script (its sleep is a multicall
    # binary that goes by the name it is called with), on Windows a copy of ping. Longer than
    # the 15 bytes of a name Linux keeps.
    name = "vmlab-long-name-" + tag
    darwin = g.os != "windows" and g.exec(["uname"]).stdout.strip() == "Darwin"  # a Fake Lab runs on the Host
    posix = 'ln -sf /bin/sleep "$0" && "$0" 8' if darwin else 'printf "#!/bin/sh\\nsleep 8\\n" > "$0" && chmod +x "$0" && "$0"'
    g.exec(background(g, posix.replace("$0", "${TMPDIR:-/tmp}/" + name),
        "$p = Join-Path $env:TEMP '%s.exe'; Copy-Item C:\\Windows\\System32\\PING.EXE $p; & $p -n 9 127.0.0.1" % name))
    running = g.wait_for(process=name, timeout=30)
    g.check("the process runs", running["met"], detail=running)
    pattern = g.wait_for(process=name[:-3] + "*", timeout=3)
    g.check("a process name is text, not a pattern", not pattern["met"] and not pattern.get("error"), detail=pattern)
    gone = g.wait_for(process=name, gone=True, timeout=60)
    g.check("process gone after it quits", gone["met"] and gone["condition"] == {"process": name, "gone": True}, detail=gone)
    g.exec(cmd(g, 'rm -f ~/%s "${TMPDIR:-/tmp}/%s"' % (flag, name), "Remove-Item -Force (Join-Path $HOME %s), (Join-Path $env:TEMP %s.exe)" % (flag, name)))
"""))

    def test_06c_put_and_get_round_trip_files(self):
        self.assertPassed(*self.target.scenario("files.py", COMMANDS + """
import os
import uuid

def scenario(g):
    tag = uuid.uuid4().hex[:6]
    text = "caf\\u00e9 \\u0417\\u0430\\u043a\\u043e\\u043d \\u2713 " + tag + "\\n"
    path = g.put("~/contract-files-%s/sub/text.txt" % tag, text)
    g.check("put returns the absolute Guest path", path.endswith("text.txt") and "~" not in path, detail=path)
    g.check("text round trip", g.get("~/contract-files-%s/sub/text.txt" % tag) == text)
    shown = g.exec(cmd(g, 'cat "$HOME/contract-files-%s/sub/text.txt"' % tag, "Get-Content -Raw -Encoding UTF8 (Join-Path $HOME contract-files-%s/sub/text.txt)" % tag))
    g.check("written as UTF-8 where the Guest's tools read it", shown.stdout.strip() == text.strip(), detail=repr(shown.stdout + shown.stderr))
    big = os.urandom(1 << 20)
    g.put("~/contract-files-%s/big.bin" % tag, big)
    g.check("a 1 MB binary file round trips", g.get("~/contract-files-%s/big.bin" % tag, binary=True) == big)
    if g.os == "windows":
        temp = g.put("%%TEMP%%\\\\contract-files-%s.txt" % tag, text)
        g.check("%%TEMP%% expands", "%%" not in temp and temp.lower().endswith("contract-files-%s.txt" % tag), detail=temp)
        g.check("%%TEMP%% round trip", g.get("%%TEMP%%\\\\contract-files-%s.txt" % tag) == text)
    try:
        g.get("~/contract-files-%s/missing.txt" % tag)
        g.check("a missing file raises", False)
    except Exception as exc:
        g.check("a missing file raises, naming it", "missing.txt" in str(exc), detail=str(exc))
    g.exec(cmd(g, 'rm -rf "$HOME/contract-files-%s"' % tag,
               "Remove-Item -Recurse -Force (Join-Path $HOME contract-files-%s), (Join-Path $env:TEMP contract-files-%s.txt)" % (tag, tag)))
"""))

    def test_06c2_put_and_get_names_with_spaces_non_ascii_and_tildes_and_over_a_symlink(self):
        self.assertPassed(*self.target.scenario("files_names.py", COMMANDS + """
import uuid

def scenario(g):
    tag = uuid.uuid4().hex[:6]
    sep = "\\\\" if g.os == "windows" else "/"
    folder = "contract names \\u00e9 %s" % tag
    text = "\\u0417\\u0430\\u043a\\u043e\\u043d \\u2713\\n"
    path = g.put("~/%s/\\u0444\\u0430\\u0439\\u043b \\u2713 caf\\u00e9.txt" % folder, text)
    g.check("a path with spaces and non-ASCII names", path.endswith(folder + sep + "\\u0444\\u0430\\u0439\\u043b \\u2713 caf\\u00e9.txt"), detail=path)
    g.check("its round trip", g.get("~/%s/\\u0444\\u0430\\u0439\\u043b \\u2713 caf\\u00e9.txt" % folder) == text)
    # ~ is the home only alone or before a slash: ~name is a relative path, in the home here too
    # (calls start there).
    tilde = g.put("~%s/x.txt" % tag, "tilde")
    g.check("~name is not the home", tilde.endswith(sep + "~%s" % tag + sep + "x.txt"), detail=tilde)
    g.check("~name round trip", g.get("~%s/x.txt" % tag) == "tilde")
    if g.os != "windows":
        # put replaces the file at the path: a symlink there is replaced, its target left alone.
        g.exec(["sh", "-c", 'cd "$HOME/$1" && printf target > target.txt && ln -s target.txt link.txt', "sh", folder])
        g.put("~/%s/link.txt" % folder, "new")
        kind = g.exec(["sh", "-c", 'cd "$HOME/$1" && if [ -L link.txt ]; then echo link; else echo file; fi', "sh", folder])
        g.check("put over a symlink replaces the link", kind.stdout.strip() == "file", detail=kind.stdout + kind.stderr)
        g.check("the link's target is untouched", g.get("~/%s/target.txt" % folder) == "target")
        g.check("the new file has the content", g.get("~/%s/link.txt" % folder) == "new")
    g.exec(cmd(g, 'rm -rf "$HOME/%s" "$HOME/~%s"' % (folder, tag),
               "Remove-Item -Recurse -Force -LiteralPath (Join-Path $HOME '%s'), (Join-Path $HOME '~%s')" % (folder, tag)))
"""))

    def test_06c3_brackets_in_names_are_taken_as_they_are(self):
        # PowerShell takes [ ] in a -Path or -Name as a wildcard: every Guest path and name must reach it literally.
        self.assertPassed(*self.target.scenario("files_brackets.py", COMMANDS + """
import uuid

def scenario(g):
    tag = uuid.uuid4().hex[:6]
    sep = "\\\\" if g.os == "windows" else "/"
    folder = "contract [1] %s" % tag
    text = "bracket %s\\n" % tag
    path = g.put("~/%s/[a].txt" % folder, text)
    g.check("put into a folder with brackets", path.endswith(folder + sep + "[a].txt"), detail=path)
    g.check("its round trip", g.get("~/%s/[a].txt" % folder) == text)
    seen = g.wait_for(file="~/%s/[a].txt" % folder, timeout=30)
    g.check("wait_for finds the file", seen["met"], detail=seen)
    logged = g.wait_for(log="~/%s/[a].txt" % folder, pattern="bracket " + tag, timeout=30)
    g.check("wait_for reads the file", logged["met"], detail=logged)
    # A program in that folder, with brackets in its own name.
    program = path[: -len("[a].txt")] + ("ps [1].exe" if g.os == "windows" else "run [1].sh")
    made = g.exec(cmd(g, 'printf "#!/bin/sh\\necho started\\n" > "$0" && chmod +x "$0"'.replace("$0", program),
                      "[IO.File]::Copy((Get-Command powershell.exe).Source, '%s')" % program))
    g.check("the program is in place", made.ok, detail=made.stderr)
    spawned = g.spawn([program, "-NoProfile", "-Command", "'started'; Start-Sleep 60"] if g.os == "windows" else [program])
    said = g.wait_for(log=spawned.log, pattern="started", timeout=30)
    g.check("spawn starts it", said["met"], detail=[said, spawned.output()])
    if g.os == "windows":
        running = g.wait_for(process="ps [1]", timeout=30)
        g.check("wait_for finds its process by its name", running["met"], detail=running)
        spawned.stop()
        gone = g.wait_for(process="ps [1]", gone=True, timeout=30)
        g.check("and sees it stop", gone["met"], detail=gone)
    try:  # PowerShell's Get-Command -Name would run whatever matches first
        g.spawn(["powershel*" if g.os == "windows" else "s*"])
        g.check("a program name is not a pattern", False)
    except Exception as exc:
        g.check("a program name is not a pattern", "no such command" in str(exc), detail=str(exc))
    g.exec(cmd(g, 'rm -rf "$HOME/%s"' % folder, "Remove-Item -Recurse -Force -LiteralPath (Join-Path $HOME '%s')" % folder))
"""))

    def test_06c4_vmrun_carries_a_file_before_it_ran_any_call(self):
        # A Windows Guest's files go to vmlab's call folder, which the vmrun Channel made at its first
        # call; when ssh fails before that, its file copy must make the folder too.
        if (self.target.provider, self.target.os) != ("fusion", "windows"):
            self.skipTest("the vmrun Channel into Windows Guests is Fusion's")
        # Set aside, not deleted: files scp sent over ssh's elevated session cannot be deleted from
        # the desktop session. The ssh call that does it is served by the call server, which uses no files.
        vmlab_dir, aside = "C:\\ProgramData\\vmlab", "calls-aside-%d" % os.getpid()
        calls = vmlab_dir + "\\calls"
        powershell = ["exec", "--lab", self.target.lab, "--", "powershell", "-NoProfile", "-Command"]
        moved = self.target.vmlab(*powershell, "Rename-Item -LiteralPath '%s' -NewName %s; Test-Path -LiteralPath '%s'" % (calls, aside, calls))
        self.assertEqual((moved.returncode, moved.stdout.strip()), (0, "False"), moved.stderr)
        restore = "Remove-Item -Recurse -Force -LiteralPath '%s'; Rename-Item -LiteralPath '%s\\%s' -NewName calls" % (calls, vmlab_dir, aside)
        self.addCleanup(self.target.vmlab, *powershell, restore)
        stand_in = Path(tempfile.mkdtemp(prefix="vmlab-no-ssh-"))
        self.addCleanup(shutil.rmtree, str(stand_in), ignore_errors=True)
        for name in ("ssh", "scp"):  # ssh cannot reach the Guest: every call falls back to vmrun
            (stand_in / name).write_text("#!/bin/sh\necho 'ssh: connect to host port 22: Connection refused' >&2\nexit 255\n")
            (stand_in / name).chmod(0o755)
        env = dict(self.target.env, PATH="%s:%s" % (stand_in, self.target.env["PATH"]))
        data = b"over vmrun %d" % os.getpid()
        put = subprocess.run(
            [sys.executable, str(zipapp_path()), "put", "~/contract-vmrun-%d.txt" % os.getpid(), "--lab", self.target.lab],
            cwd=str(self.target.root), env=env, input=data, capture_output=True, timeout=300,
        )  # fmt: skip
        self.assertEqual(put.returncode, 0, put.stderr)
        got = subprocess.run(
            [sys.executable, str(zipapp_path()), "get", "~/contract-vmrun-%d.txt" % os.getpid(), "--lab", self.target.lab],
            cwd=str(self.target.root), env=self.target.env, capture_output=True, timeout=300,
        )  # fmt: skip
        self.assertEqual((got.returncode, got.stdout), (0, data), got.stderr)
        self.target.vmlab("exec", "--lab", self.target.lab, "--", *self.remove_argv(put.stdout.decode().strip()))

    def test_06d_put_and_get_on_the_cli(self):
        data = bytes(range(256)) * 16 + "é✓".encode("utf-8")
        path = "~/contract-cli-%d.bin" % os.getpid()
        put = subprocess.run(
            [sys.executable, str(zipapp_path()), "put", path, "--lab", self.target.lab],
            cwd=str(self.target.root), env=self.target.env, input=data, capture_output=True, timeout=300,
        )  # fmt: skip
        self.assertEqual(put.returncode, 0, put.stderr)
        got = subprocess.run(
            [sys.executable, str(zipapp_path()), "get", path, "--lab", self.target.lab],
            cwd=str(self.target.root), env=self.target.env, capture_output=True, timeout=300,
        )  # fmt: skip
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, data)
        self.target.vmlab("exec", "--lab", self.target.lab, "--", *self.remove_argv(put.stdout.decode().strip()))

    def test_06e_spawn_a_server_stop_it_and_the_run_stops_the_other(self):
        tag = uuid.uuid4().hex[:6]
        port = 20000 + int(tag, 16) % 20000
        proc, report = self.target.scenario("spawn.py", COMMANDS + SPAWN + """
TAG, PORT = %r, %d

def scenario(g):
    web, ask = server(g, "vs" + TAG, PORT)
    answered = g.wait_for(exec=ask, pattern="ok " + TAG, timeout=60)
    g.check("the spawned server answers", answered["met"], detail=[answered, web.output()])
    g.check("it is running", web.running())
    logged = g.wait_for(log=web.log, pattern="got: GET", timeout=20)
    g.check("its output is in its log", logged["met"], detail=repr(web.output()))
    g.check("output() reads it", "listening on %%d" %% PORT in web.output(), detail=repr(web.output()))
    web.stop()
    gone = g.wait_for(process="vs" + TAG, gone=True, timeout=30)
    g.check("stop ends it", gone["met"], detail=gone)
    g.check("it is not running", not web.running())
    orphaned, ask = orphaned_server(g, "vs" + TAG + "c", PORT + 2)
    answered = g.wait_for(exec=ask, pattern="ok " + TAG, timeout=60)
    g.check("a server whose starter exited answers", answered["met"], detail=[answered, orphaned.output()])
    for _ in range(40):
        if not orphaned.running():
            break
        time.sleep(0.5)
    g.check("its starter exited", not orphaned.running())
    orphaned.stop()
    gone = g.wait_for(process="vs" + TAG + "c", gone=True, timeout=30)
    g.check("stop ends what an exited process left running", gone["met"], detail=gone)
    left, ask = server(g, "vs" + TAG + "b", PORT + 1)
    answered = g.wait_for(exec=ask, pattern="ok " + TAG, timeout=60)
    g.check("the second server answers", answered["met"], detail=[answered, left.output()])
    remove_programs(g, "vs" + TAG, "vs" + TAG + "c")
""" % (tag, port))
        self.assertPassed(proc, report)
        [web, orphaned, left] = report["scenarios"][0]["spawned"]
        self.assertEqual((web["ended"], orphaned["ended"], left["ended"]), ("scenario", "scenario", "run"), report["scenarios"][0]["spawned"])
        self.assertPassed(*self.target.scenario("spawn_after.py", COMMANDS + SPAWN + """
TAG = %r

def scenario(g):
    gone = g.wait_for(process="vs" + TAG + "b", gone=True, timeout=10)
    g.check("what the Scenario left running ended with its Run", gone["met"], detail=gone)
    remove_programs(g, "vs" + TAG + "b")
""" % tag))

    def remove_argv(self, guest_path):
        if self.target.os == "windows":
            return ["powershell", "-NoProfile", "-Command", "Remove-Item -Force -LiteralPath '%s'" % guest_path]
        return ["rm", "-f", guest_path]

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
    def test_07a_ui_an_app_is_found_by_its_other_names_too(self):
        # The helper chooses an app by more than its name: on macOS by its bundle id, on Linux by
        # its process name (gnome-text-editor's is cut to 15 bytes). Element queries take either.
        if self.target.provider == "fake":
            self.skipTest("the Fake's apps are scripted: tests/test_ui.py covers a bundle id")
        if self.target.os == "windows":
            self.skipTest("Windows chooses apps by their process name alone, which is the app node's name")
        self.assertPassed(*self.target.scenario("ui_app_alias.py", UI + """
def scenario(g):
    text = "vmlab alias " + uuid.uuid4().hex[:8]
    staged = g.stage_text(text)
    app = staged["app"]
    [node] = [a for a in g.tree(app=app)["children"] if a["name"] == app]
    if g.os == "macos":
        alias = g.exec(["osascript", "-e", 'id of app "%s"' % app]).stdout.strip()  # the bundle id
    else:
        alias = g.exec(["cat", "/proc/%d/comm" % node["pid"]]).stdout.strip()  # the process name
    g.check("the app has another name", alias and alias.lower() != app.lower(), detail=[app, alias])
    by_name = g.find(role="textarea", text=text, app=app)["matches"]
    by_alias = g.find(role="textarea", text=text, app=alias)["matches"]
    g.check("find by it matches the same, naming the app by its name", by_name and by_alias == by_name, detail=[by_name, by_alias])
    waited = g.wait_for(role="textarea", text=text, app=alias, timeout=10)
    g.check("wait-for by it is met", waited["met"], detail=waited)
    clicked = g.click(role="textarea", text=text, app=alias)
    g.check("click by it lands", clicked["element"]["app"] == app, detail=clicked)
"""))

    @ui
    def test_07b_ui_close_staged_closes_just_its_staged_document(self):
        proc, report = self.target.scenario("ui_close_staged.py", UI + """
import re

FAKE = %r  # the Fake editor opens Staged documents only
EDITOR = {"macos": ["open", "-a", "TextEdit"], "windows": ["notepad.exe"], "linux": ["gnome-text-editor"]}
COPY = {"macos": "cmd+c"}

def stems(g, app):
    # The documents named like staged ones the editor shows: in window titles, tabs and proxy icons alike.
    return {m for n in walk(g.tree(app=app)) for m in re.findall(r"vmlab-stage-[0-9A-Fa-f]{8}", n["name"] or "")}

def stem(path):
    return re.search(r"vmlab-stage-[0-9A-Fa-f]{8}", path).group(0)

def scenario(g):
    tag = uuid.uuid4().hex[:8]
    first = g.stage_text("first " + tag)
    app = first["app"]
    g.type("changed " + tag)  # a document changed since it was staged closes too
    own = "vmlab-stage-" + uuid.uuid4().hex[:8]  # the editor's own document, named like a staged one
    if FAKE:
        outside = "/home/contract/%%s.txt" %% own
    else:
        outside = g.put("~/%%s.txt" %% own, "own " + tag)
        g.spawn(EDITOR[g.os] + [outside])
        opened = g.wait_for(text=own, app=app, timeout=30)
        g.check("the editor opened a document of its own", opened["met"], detail=opened)
    second = g.stage_text("second " + tag)
    g.check("the second's text is selected and frontmost", (second["selected"], second["frontmost"]) == ("second " + tag, app), detail=second)
    shown = stems(g, app)
    g.check("a stage closes nothing", {stem(first["file"]), stem(second["file"])} <= shown and (FAKE or own in shown), detail=sorted(shown))

    closed = g.close_staged(first)
    g.check("close_staged closes the first", closed == {"file": first["file"], "closed": True}, detail=closed)
    shown = stems(g, app)
    g.check("and no other", stem(first["file"]) not in shown and stem(second["file"]) in shown and (FAKE or own in shown), detail=sorted(shown))
    g.press(COPY.get(g.os, "ctrl+c"))
    g.check("the second is still in front with its text selected", g.clipboard()["text"] == "second " + tag, detail=g.clipboard())
    again = g.close_staged(first["file"])
    g.check("closing it again finds it gone", again == {"file": first["file"], "closed": False}, detail=again)
    try:
        g.close_staged(outside)
        refused = None
    except Exception as exc:
        refused = str(exc)
    g.check("a document outside the staging folder is refused", refused and "not a Staged document" in refused, detail=refused)
    if not FAKE:
        g.check("and stays open", own in stems(g, app), detail=sorted(stems(g, app)))
        g.exec(cmd(g, "rm -f '%%s'" %% outside, "Remove-Item -Force -LiteralPath '%%s'" %% outside))
    # The second stays open: its Run closes it.
""" % (self.target.provider == "fake"))
        self.assertPassed(proc, report)
        [first, second] = report["scenarios"][0]["staged"]
        self.assertEqual((first["ended"], second["ended"]), ("scenario", "run"), report["scenarios"][0]["staged"])
        self.assertPassed(*self.target.scenario("ui_close_staged_after.py", UI + """
import re

def scenario(g):
    shown = [n["name"] for n in walk(g.tree()) if %r in (n["name"] or "")]
    g.check("the Staged document the Scenario left open was closed with its Run", not shown, detail=sorted(shown))
""" % os.path.splitext(os.path.basename(second["file"].replace("\\", "/")))[0]))

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
    emptied = g.set_clipboard("")
    g.check("the clipboard can be emptied", (emptied["text"], g.clipboard()["text"]) == ("", ""), detail=[emptied, g.clipboard()])
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
    def test_08b_ui_a_click_with_a_timeout_waits_for_a_window_opened_just_before_it(self):
        self.assertPassed(*self.target.scenario("ui_click_timeout.py", UI + """
FAKE = %r  # the Fake editor opens staged documents only
EDITOR = {"macos": ["open", "-a", "TextEdit"], "windows": ["notepad.exe"], "linux": ["gnome-text-editor"]}

def scenario(g):
    tag = uuid.uuid4().hex[:8]
    text = "opened " + tag
    if FAKE:
        app = g.stage_text(text)["app"]
    else:
        app = g.stage_text("staged " + tag)["app"]
        g.spawn(EDITOR[g.os] + [g.put("~/vmlab-opened-%%s.txt" %% tag, text)])  # its window is not there yet
    clicked = g.click(role="textarea", text=text, app=app, timeout=30)
    g.check("the click waited for the new document's text area", clicked["element"]["value"] == text, detail=clicked)
""" % (self.target.provider == "fake")))

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
    def test_09b_ui_a_taskbar_button_takes_a_click(self):
        # The Windows 11 taskbar draws its buttons in XAML that FromPoint does not
        # reach: a click's hit test must still find the button, not refuse it as covered.
        if self.target.os != "windows":
            self.skipTest("the taskbar is Windows'")
        self.assertPassed(*self.target.scenario("ui_taskbar.py", UI + """
def scenario(g):
    buttons = g.find(role="button", app="explorer")["matches"]
    g.check("the taskbar's buttons are in the tree", len(buttons) > 0, detail=buttons)
    clicked = g.click(role="button", app="explorer", index=0)  # Start, whatever the display language
    g.check("a click lands on the taskbar button", clicked["element"]["name"] == buttons[0]["name"], detail=clicked)
    g.press("escape")
"""))

    @ui
    def test_09c_ui_tray_reads_and_chooses_from_a_tray_menu(self):
        if self.target.provider == "fake":
            self.skipTest("the Fake's Tray menus are scripted: tests/test_ui.py covers them")
        source = (Path(__file__).resolve().parent / TRAY_FIXTURES[self.target.os]).read_text(encoding="utf-8")
        self.assertPassed(*self.target.scenario("ui_tray.py", UI + "SOURCE = %r\n" % source + TRAY))

    @ui
    def test_09d_ui_notifications_are_read_and_waited_for(self):
        if self.target.provider == "fake":
            self.skipTest("the Fake's Notifications are scripted: tests/test_ui.py covers them")
        self.assertPassed(*self.target.scenario("ui_notifications.py", UI + "from datetime import datetime\n" + NOTIFY))

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
