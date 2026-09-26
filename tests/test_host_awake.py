"""vmlab keeps the Host awake while it works: a Host that sleeps pauses its Guests mid-boot and mid-Run."""

import json
import stat
import textwrap
import time

from harness import FAKE_LAB, VmlabTestCase

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
