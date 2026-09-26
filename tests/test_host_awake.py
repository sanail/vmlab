"""vmlab keeps the Host awake while it works: a Host that sleeps pauses its Guests mid-boot and mid-Run.

A macOS Host is driven through the zipapp; Linux and Windows Hosts load vmlab.hostpower directly,
with platform.system() answering for them.
"""

import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import FAKE_LAB, VmlabTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab import hostpower  # noqa: E402

# A stand-in for caffeinate: records its arguments, then exits.
FAKE_CAFFEINATE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    with open(os.environ["FAKE_CAFFEINATE_LOG"], "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    """
)


class HostAwakeTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.caffeinate = self.project.root / "fake-caffeinate"
        self.caffeinate.write_text(FAKE_CAFFEINATE)
        self.caffeinate.chmod(self.caffeinate.stat().st_mode | stat.S_IXUSR)
        self.log = self.project.root / "caffeinate.jsonl"
        self.project.config(FAKE_LAB)

    def vmlab(self, *args):
        """(pid, exit code) of vmlab with the stand-in caffeinate."""
        proc = self.project.vmlab_background(*args, env={"VMLAB_CAFFEINATE": str(self.caffeinate), "FAKE_CAFFEINATE_LOG": str(self.log)})
        proc.communicate(timeout=60)
        return proc.pid, proc.returncode

    def calls(self, within=0):
        """What caffeinate was started with; it may still be starting when vmlab has exited."""
        deadline = time.time() + within
        while not self.log.exists() and time.time() < deadline:
            time.sleep(0.05)
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_a_command_that_drives_guests_keeps_the_host_awake_until_vmlab_exits(self):
        pid, code = self.vmlab("up")

        self.assertEqual(code, 0)
        self.assertEqual(self.calls(within=10), [["-i", "-s", "-w", str(pid)]])

    def test_version_does_not_keep_the_host_awake(self):
        self.assertEqual(self.vmlab("version")[1], 0)
        self.assertEqual(self.calls(within=1), [])

    def test_a_host_without_caffeinate_runs_anyway(self):
        r = self.project.vmlab("up", env={"VMLAB_CAFFEINATE": "/nonexistent/caffeinate"})
        self.assertExit(r, 0)


class OtherHostsTest(unittest.TestCase):
    def test_a_linux_host_is_held_by_systemd_inhibit_until_vmlab_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub, log = Path(tmp) / "systemd-inhibit", Path(tmp) / "log.jsonl"
            stub.write_text(FAKE_CAFFEINATE)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            env = {"VMLAB_SYSTEMD_INHIBIT": str(stub), "FAKE_CAFFEINATE_LOG": str(log)}
            with mock.patch("platform.system", return_value="Linux"), mock.patch.dict(os.environ, env):
                hostpower.keep_awake()
            deadline = time.time() + 10
            while not log.exists() and time.time() < deadline:
                time.sleep(0.05)
            [argv] = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(argv[:4], ["--what=idle:sleep", "--who=vmlab", "--why=Guests are running", "--mode=block"])
        self.assertEqual(argv[4:], ["tail", "--pid=%d" % os.getpid(), "-f", "/dev/null"])

    def test_a_linux_host_without_systemd_runs_anyway(self):
        with mock.patch("platform.system", return_value="Linux"), mock.patch.dict(os.environ, {"VMLAB_SYSTEMD_INHIBIT": "/nonexistent/systemd-inhibit"}):
            hostpower.keep_awake()

    def test_a_windows_host_is_held_by_the_execution_state_of_vmlabs_thread(self):
        kernel32 = mock.Mock()
        with mock.patch("platform.system", return_value="Windows"), mock.patch.object(hostpower, "_kernel32", return_value=kernel32):
            hostpower.keep_awake()
        kernel32.SetThreadExecutionState.assert_called_once_with(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED

    def test_a_windows_hosts_awake_time_leaves_out_its_sleep(self):
        def unbiased(ref):  # QueryUnbiasedInterruptTime: 100 ns units, not counting sleep
            ref._obj.value = 12_345 * 10_000_000
            return 1

        kernel32 = mock.Mock(QueryUnbiasedInterruptTime=unbiased)
        with mock.patch("platform.system", return_value="Windows"), mock.patch.object(hostpower, "_kernel32", return_value=kernel32):
            self.assertEqual(hostpower.awake_time(), 12_345)
