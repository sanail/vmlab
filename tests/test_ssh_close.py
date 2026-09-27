"""Closing the ssh Channel's master connection, loaded directly against a stand-in for ssh."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab.providers import ssh  # noqa: E402


class CloseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patched = mock.patch.dict(os.environ, {"VMLAB_HOME": tmp.name})
        patched.start()
        self.addCleanup(patched.stop)
        self.channel = ssh.SshChannel("admin", lambda: "192.0.2.1", "mac", "vmlab-app-mac")
        self.socket = self.channel._control
        self.socket.write_text("")

    def test_a_master_that_removes_its_own_socket_as_it_exits_closes_cleanly(self):
        # `ssh -O exit` asks the master to go, and the master removes its socket itself,
        # sometimes only just after close() has seen it still there.
        def exit_master(argv, timeout):
            if "-O" in argv:
                self.socket.unlink()
            return 0, "", ""

        seen = {self.socket: True}
        real_exists = Path.exists
        with mock.patch.object(ssh.hostproc, "run", exit_master), \
                mock.patch.object(Path, "exists", lambda path: seen.get(path, False) or real_exists(path)):
            self.channel.close()

        self.assertFalse(self.socket.exists())

    def test_a_socket_the_master_left_behind_is_removed(self):
        with mock.patch.object(ssh.hostproc, "run", lambda argv, timeout: (255 if "-O" in argv else 0, "", "")):
            self.channel.close()

        self.assertFalse(self.socket.exists())


if __name__ == "__main__":
    unittest.main()
