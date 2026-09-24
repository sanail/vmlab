"""The Guest OS: how vmlab runs commands, names paths and drives the UI in each OS's Guests.

A Fake Lab of any os runs its commands on the Host, in its POSIX shell: the first tests drive
the zipapp against a Fake Lab with os = "windows". The rest load vmlab.guestos directly and
check what each Guest OS object builds, running its POSIX commands in the Host's sh; the contract suite
(tests/contract/test_contract.py) runs those commands in real Guests.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import VmlabTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab import guestos, ui  # noqa: E402
from vmlab.config import OSES, UsageError  # noqa: E402
from vmlab.providers import spawning  # noqa: E402
from vmlab.providers.base import NO_FILE  # noqa: E402

WINDOWS_FAKE_LAB = """
[labs.win]
provider = "fake"
os = "windows"
arch = "arm64"
[labs.win.app]
state = ["~/.myapp"]
"""


class FakeWindowsLabTest(VmlabTestCase):
    """A Fake Lab's commands run on the Host whatever its os, so its Guest OS is the Host's POSIX one."""

    def setUp(self):
        super().setUp()
        self.project.config(WINDOWS_FAKE_LAB)
        self.addCleanup(self.kill_spawned)

    def kill_spawned(self):
        for d in self.project.run_dirs():
            if (d / "report.json").exists():
                for s in self.project.report(d)["scenarios"]:
                    for p in s.get("spawned", []):
                        try:
                            os.killpg(p["pid"], signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass

    def test_get_state_reset_and_spawn_run_in_the_hosts_shell(self):
        self.project.scenario("a_dirty.py", """
            def scenario(g):
                g.put("~/.myapp/settings", "dirty")
                g.check("get reads what put wrote", g.get("~/.myapp/settings") == "dirty")
                p = g.spawn(["sh", "-c", "echo spawned; sleep 300"])
                g.check("spawned process logs", g.wait_for(log=p.log, pattern="spawned", timeout=10)["met"])
                g.check("its log is read with get", p.output() == "spawned\\n", detail=repr(p.output()))
                g.check("spawned process runs", p.running())
                p.stop()
                g.check("spawned process stops", not p.running())
                g.check("process condition", g.wait_for(process="no-such-process-vmlab", gone=True, timeout=5)["met"])
                g.check("file condition", g.wait_for(file="~/.myapp/settings", timeout=5)["met"])
        """)
        self.project.scenario("b_clean.py", """
            def scenario(g):
                g.check("app state reset", not g.exec(["sh", "-c", "test -e ~/.myapp"]).ok)
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)


class SplitPathTest(unittest.TestCase):
    def test_posix_paths(self):
        for os_name in ("macos", "linux"):
            split = guestos.for_os(os_name).split_path
            self.assertEqual(split("/etc/hosts"), ("/etc", "hosts"))
            self.assertEqual(split("/hosts"), ("/", "hosts"))
            self.assertEqual(split("~/a/notes.txt"), ("~/a", "notes.txt"))
            self.assertEqual(split("notes.txt"), ("~", "notes.txt"))
            self.assertEqual(split("a\\b.txt"), ("~", "a\\b.txt"))  # a backslash is part of a POSIX name

    def test_windows_paths(self):
        split = guestos.for_os("windows").split_path
        self.assertEqual(split("C:\\Users\\me\\notes.txt"), ("C:\\Users\\me", "notes.txt"))
        self.assertEqual(split("C:\\notes.txt"), ("C:\\", "notes.txt"))
        self.assertEqual(split("~/a\\notes.txt"), ("~/a", "notes.txt"))
        self.assertEqual(split("~\\notes.txt"), ("~", "notes.txt"))
        self.assertEqual(split("notes.txt"), ("~", "notes.txt"))

    def test_a_path_without_a_file_name_is_a_usage_error(self):
        for os_name in OSES:
            for path in ("~", "~/", "/tmp/", "/tmp/..", "."):
                with self.assertRaises(UsageError, msg=(os_name, path)):
                    guestos.for_os(os_name).split_path(path)


class TildeTest(unittest.TestCase):
    """~ alone or before a slash is the Guest user's home; ~other stays as it is."""

    def read(self, os_name, path, home):
        argv = guestos.for_os(os_name).read_file_argv(path)
        return subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ, HOME=home))

    def test_posix_read_file_expands_tilde_to_the_home(self):
        home = tempfile.mkdtemp(prefix="vmlab-tilde-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        Path(home, "a b").mkdir()
        Path(home, "a b", "$x").write_text("hi")
        for os_name in ("macos", "linux"):
            self.assertEqual(self.read(os_name, "~/a b/$x", home).stdout.strip(), "aGk=", os_name)
            self.assertEqual(self.read(os_name, "~other/x", home).returncode, NO_FILE, os_name)

    def test_posix_remove_paths_expands_tilde_to_the_home(self):
        home = tempfile.mkdtemp(prefix="vmlab-tilde-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        Path(home, "gone").mkdir()
        argv = guestos.for_os("linux").remove_paths_argv(["~/gone"])
        subprocess.run(argv, check=True, env=dict(os.environ, HOME=home))
        self.assertEqual(os.listdir(home), [])

    def test_windows_paths_are_single_quoted_and_tilde_is_the_profile(self):
        windows = guestos.for_os("windows")
        script = windows.read_file_argv("~\\it's $HOME")[-1]
        self.assertIn("($env:USERPROFILE + [Environment]::ExpandEnvironmentVariables('\\it''s $HOME'))", script)
        script = windows.remove_paths_argv(["~notes", "C:\\x"])[-1]
        self.assertNotIn("USERPROFILE", script)
        self.assertIn("'~notes'", script)


class ProcessProbeTest(unittest.TestCase):
    LONG = "a-very-long-process-name"  # more than Linux's 15 bytes

    def test_linux_matches_the_first_15_bytes_then_the_full_name(self):
        argv = guestos.for_os("linux").probes().process_argv(None, self.LONG)
        self.assertEqual(argv[-2:], ["^a-very-long-pro", self.LONG])

    def test_macos_matches_the_whole_name(self):
        argv = guestos.for_os("macos").probes().process_argv(None, self.LONG)
        self.assertEqual(argv[-2:], ["^a-very-long-process-name$", ""])

    def test_a_short_name_is_matched_whole_on_linux_too(self):
        argv = guestos.for_os("linux").probes().process_argv(None, "myapp.bin")
        self.assertEqual(argv[-2:], ["^myapp\\.bin$", ""])


class ChoicesTest(unittest.TestCase):
    def test_each_guest_os_runs_its_shell(self):
        self.assertEqual(guestos.for_os("linux").shell_argv("true"), ["sh", "-c", "true"])
        self.assertEqual(guestos.for_os("windows").shell_argv("exit 0")[0], "powershell")
        self.assertEqual(guestos.for_os("windows").probe_argv(), ["cmd", "/c", "exit 0"])
        self.assertEqual(guestos.for_os("macos").probe_argv(), ["true"])

    def test_spawners(self):
        self.assertIsInstance(guestos.for_os("macos").spawner(None), spawning.PosixSpawner)
        self.assertIsInstance(guestos.for_os("windows").spawner(None), spawning.WindowsSpawner)

    def test_a_fake_lab_runs_posix_commands_with_its_guest_oss_ui(self):
        fake = guestos.FakeGuestOS(guestos.for_os("windows"))
        self.assertEqual(fake.shell_argv("true"), ["sh", "-c", "true"])
        self.assertIsInstance(fake.spawner(None), spawning.PosixSpawner)
        self.assertIsInstance(fake.probes(), ui.PosixProbes)
        self.assertEqual(fake.stage_app, "Notepad")
        self.assertIs(fake.native_roles, guestos.for_os("windows").native_roles)

    def test_a_fake_linux_lab_keeps_linuxs_process_name_probe(self):
        self.assertIsInstance(guestos.FakeGuestOS(guestos.for_os("linux")).probes(), ui.LinuxProbes)


if __name__ == "__main__":
    unittest.main()
