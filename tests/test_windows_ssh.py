"""The Host half of the Windows ssh Channel: calls to vmlab's call server in the Guest's desktop
session (vmlab.providers.windows), against a scripted stand-in for ssh and scp.

The stand-in plays the Guest's sshd and the server: `ssh -W` answers as the server would, or
with nothing while no server listens (as ssh does when Windows refuses the connection), and
the `-Start` command starts it. What needs a real Windows Guest (the server itself, the
Scheduled Task, the desktop session) is covered by Seam 2: tests/contract/test_contract.py
with a Windows Lab.
"""

import base64
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab.providers import windows  # noqa: E402
from vmlab.providers.base import ChannelError, ExecResult, GuestError, GuestTimeout, ps_path  # noqa: E402

FAKE_SSH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import base64, json, os, sys, time
    state = os.environ["FAKE_GUEST"]
    args = sys.argv[1:]
    with open(os.path.join(state, "log"), "a") as log:
        log.write(json.dumps([os.path.basename(sys.argv[0])] + args) + "\\n")
    if os.path.basename(sys.argv[0]) == "scp":
        time.sleep(float(os.environ.get("FAKE_SCP_SECONDS", "0")))
        sys.exit(0)
    if "-O" in args or "-N" in args:
        sys.exit(0)
    server = os.path.join(state, "server")
    if "-W" in args:
        request = sys.stdin.buffer.read()
        if not os.path.exists(server):
            sys.exit(0)  # nothing listens on the port: ssh -W prints nothing
        mode = open(server).read()
        sys.stdout.write("vmlab-call-server %s\\n" % os.environ["FAKE_SERVER_VERSION"])
        sys.stdout.flush()
        if mode == "mute":
            sys.exit(0)
        if mode == "slow":
            time.sleep(10)
        head, _, data = request.partition(b"\\n\\n")
        fields = dict(line.split(" ", 1) for line in head.decode("ascii").splitlines())
        decode = lambda b64: base64.b64decode(b64).decode("utf-8")
        said = ("%s %s|" % (decode(fields["file"]), decode(fields["args"]))).encode("utf-8") + data
        assert len(data) == int(fields["stdin"]), "stdin length"
        print(json.dumps({"code": 0, "stdout": base64.b64encode(said).decode(), "stderr": ""}))
        sys.exit(0)
    if "-Start" in args[-1]:
        if os.environ.get("FAKE_SERVER_FAILS"):
            sys.stderr.write("vmlab: the call server's Scheduled Task did not start it (task state 3, last result 0); is tester logged in to the desktop?\\n")
            sys.exit(3)
        with open(server, "w") as f:
            f.write("ok")
    sys.exit(0)
    """
)


class WindowsSshChannelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vmlab-winssh-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.guest = self.tmp / "guest"
        self.guest.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name in ("ssh", "scp"):
            (bin_dir / name).write_text(FAKE_SSH)
            (bin_dir / name).chmod(0o755 | stat.S_IXUSR)
        home = self.tmp / "home"
        (home / "ssh").mkdir(parents=True)
        (home / "ssh" / "id_ed25519").write_text("key")  # never read: ssh is the stand-in
        env = {
            "PATH": "%s:%s" % (bin_dir, os.environ["PATH"]),
            "VMLAB_HOME": str(home),
            "HOME": str(self.tmp),
            "FAKE_GUEST": str(self.guest),
            "FAKE_SERVER_VERSION": windows.server_version(),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.channel = windows.WindowsSshChannel("tester", lambda: "10.0.0.9", "vmlab-base-windows-11", "guest-1")

    def server(self, mode="ok"):
        (self.guest / "server").write_text(mode)

    def calls(self):
        """What reached the Guest: 'forward' (a call to the server's port), 'start', 'mkdir', 'scp'."""
        log = self.guest / "log"
        found = []
        for args in (json.loads(line) for line in log.read_text().splitlines()) if log.exists() else []:
            if args[0] == "scp":
                found.append("scp")
            elif "-W" in args:
                found.append("forward")
            elif "-Start" in args[-1]:
                found.append("start")
            elif "New-Item" in args[-1]:
                found.append("mkdir")
        return found

    def test_the_first_call_starts_the_server_and_later_calls_go_straight_to_it(self):
        first = self.channel.exec(["cmd", "/c", "echo one"], 5, {})
        second = self.channel.exec(["cmd", "/c", "echo two"], 5, {})

        self.assertEqual(first.stdout, 'cmd /c "echo one"|')
        self.assertEqual(second.stdout, 'cmd /c "echo two"|')
        self.assertEqual(self.calls(), ["forward", "mkdir", "scp", "start", "forward", "forward"])

    def test_a_running_server_is_not_started_again(self):
        self.server()
        self.channel.exec(["cmd", "/c", "exit 0"], 5, {})
        self.assertEqual(self.calls(), ["forward"])

    def test_after_the_guest_stopped_the_server_is_started_without_trying_it_first(self):
        self.channel.close()  # the Provider closes its Channels when it stops or starts the Guest
        self.channel.exec(["cmd", "/c", "exit 0"], 5, {})
        self.channel.exec(["cmd", "/c", "exit 0"], 5, {})
        self.assertEqual(self.calls(), ["mkdir", "scp", "start", "forward", "forward"])

    def test_stdin_reaches_the_server_whole_after_the_request(self):
        self.server()
        text = "\ufeffстрока\n\n" * 2000  # beyond sshd's ~4 KB, a BOM, and what looks like the request's end
        result = self.channel.exec(["tar.exe", "-xf", "-"], 5, {}, stdin=io.BytesIO(text.encode("utf-8")))
        self.assertEqual(result.stdout, "tar.exe -xf -|" + text)  # the stand-in echoes stdin back

    def test_the_request_carries_the_environment(self):
        request = windows.call_request(["app.exe", "a b"], {"LANG": "ru", "EMPTY": ""}, 7.5, 3).decode("ascii")
        b64 = lambda text: base64.b64encode(text.encode("utf-8")).decode("ascii")  # noqa: E731
        self.assertEqual(
            request.split("\n"),
            ["file " + b64("app.exe"), "args " + b64('"a b"'), "timeout 7", "env %s " % b64("EMPTY"), "env %s %s" % (b64("LANG"), b64("ru")), "stdin 3", "", ""],
        )

    def test_a_call_the_server_took_but_did_not_answer_is_not_repeated(self):
        self.server("mute")
        with self.assertRaises(GuestError) as caught:
            self.channel.exec(["cmd", "/c", "exit 0"], 5, {})
        self.assertIn("gave no result", caught.exception.message)
        self.assertNotIsInstance(caught.exception, ChannelError, "no other Channel may run it again")
        self.assertNotIsInstance(caught.exception, ChannelError, "another Channel must not run it again")
        self.assertEqual(self.calls(), ["forward"])

    def test_a_server_that_does_not_start_says_why(self):
        with mock.patch.dict(os.environ, {"FAKE_SERVER_FAILS": "1"}):
            with self.assertRaises(ChannelError) as caught:
                self.channel.exec(["cmd", "/c", "exit 0"], 5, {})
        self.assertIn("is tester logged in to the desktop?", caught.exception.message)
        self.assertIn("autologin", caught.exception.fix)

    def test_a_call_that_outlives_its_timeout_is_killed(self):
        self.server("slow")
        with self.assertRaises(GuestTimeout) as caught:
            self.channel.exec(["powershell", "-Command", "Start-Sleep 30"], 1, {})
        self.assertIn("timed out after 1s on Channel ssh", caught.exception.message)

    def test_scp_is_killed_at_its_timeout(self):
        local = self.tmp / "big.tar"
        local.write_bytes(b"x")
        started = time.time()
        with mock.patch.dict(os.environ, {"FAKE_SCP_SECONDS": "30"}):
            with self.assertRaises(GuestTimeout) as caught:
                self.channel.send_file(local, r"C:\ProgramData\vmlab\calls\big.tar", 1)
        self.assertLess(time.time() - started, 10)
        self.assertIn("within 1s", caught.exception.message)

    def test_each_version_of_the_server_has_its_own_port(self):
        ports = {windows.server_port("%012x" % n) for n in range(0, 2**48, 2**40)}
        self.assertEqual(len(ports), 256)
        self.assertTrue(all(windows.SERVER_PORTS[0] <= p < windows.SERVER_PORTS[1] for p in ports))


class StandInProvider:
    """What windows.copy_in uses of a Provider: its transfer takes send_seconds; exec answers."""

    def __init__(self, send_seconds):
        self.lab = mock.Mock(name="lab")
        self.lab.name, self.lab.app.install_timeout = "win", 600
        self.send_seconds = send_seconds
        self.timeouts = []  # (what, timeout) per call
        self.scripts = []  # the PowerShell of each exec

    def send_file(self, local, guest_path, timeout):
        self.timeouts.append(("send_file", timeout))
        if self.send_seconds > timeout:
            time.sleep(timeout)
            raise GuestTimeout("copy timed out")
        time.sleep(self.send_seconds)

    def shell_argv(self, command):
        return ["powershell", "-Command", command]

    def exec(self, argv, timeout, env=None, stdin=None):
        self.timeouts.append(("exec", timeout))
        self.scripts.append(argv[-1])
        return ExecResult(argv, 0, "C:\\Users\\tester\\notes\r\n", "")


class WindowsCopyInTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="vmlab-wincopy-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        self.src = tmp / "a.txt"
        self.src.write_text("a")

    def test_the_transfer_and_the_unpacking_share_the_timeout(self):
        provider = StandInProvider(send_seconds=0.5)
        path = windows.copy_in(provider, self.src, "~/notes", 5)
        self.assertEqual(path, "C:\\Users\\tester\\notes\\a.txt")
        [(first, sent), (second, unpacked)] = provider.timeouts
        self.assertEqual((first, second), ("send_file", "exec"))
        self.assertLessEqual(sent, 5)
        self.assertLessEqual(unpacked, 5 - 0.5)

    def test_a_transfer_that_outlives_the_timeout_ends_the_copy(self):
        provider = StandInProvider(send_seconds=30)
        started = time.time()
        with self.assertRaises(GuestTimeout):
            windows.copy_in(provider, self.src, "~/notes", 1)
        self.assertLess(time.time() - started, 5)
        self.assertEqual([what for what, _ in provider.timeouts], ["send_file"])

    def test_the_folder_is_named_literally_brackets_and_all(self):
        provider = StandInProvider(send_seconds=0)
        windows.copy_in(provider, self.src, "~/notes [1]", 5)
        [script] = provider.scripts
        self.assertIn(ps_path("~/notes [1]"), script)
        self.assertNotRegex(script, r"\s-Path\b")  # PowerShell's -Path takes [ ] as a wildcard


class PowerShellPathTest(unittest.TestCase):
    def test_tilde_is_the_profile_only_alone_or_before_a_slash(self):
        for path in ("~", "~\\notes.txt", "~/notes.txt"):
            self.assertTrue(ps_path(path).startswith("($env:USERPROFILE + "), path)
        for path in ("~notes.txt", "C:\\~\\x", "%TEMP%\\~x"):
            self.assertNotIn("USERPROFILE", ps_path(path), path)

    def test_the_path_is_taken_literally_not_as_a_pattern(self):
        expr = ps_path("~\\it's $HOME\\%TEMP%")
        self.assertNotIn("-replace", expr)
        self.assertIn("'\\it''s $HOME\\%TEMP%'", expr)  # single-quoted: no $ expansion


class StandInGuest:
    """What vmrun's guest operations reach in a Windows Guest: folders vmrun makes, and files it
    copies in, which need their folder (vmrun makes no parents)."""

    def __init__(self):
        self.dirs = {"C:\\", "C:\\ProgramData"}
        self.files = []

    def vmrun(self, args, timeout, auth=()):
        command, path = args[0], str(args[-1])
        parent = path.rsplit("\\", 1)[0]
        parent = parent + "\\" if parent.endswith(":") else parent
        if command == "createDirectoryInGuest":
            if path in self.dirs:
                return 255, "Error: The file already exists\n"
            if parent not in self.dirs:
                return 255, "Error: A file was not found\n"
            self.dirs.add(path)
            return 0, ""
        if command == "copyFileFromHostToGuest":
            if parent not in self.dirs:
                return 255, "Error: A file was not found\n"
            self.files.append(path)
            return 0, ""
        return 255, "Error: stand-in: unsupported %r\n" % (args,)


class WindowsVmrunChannelTest(unittest.TestCase):
    def setUp(self):
        from vmlab.providers import fusion

        self.guest = StandInGuest()
        for name, value in (("vmrun", self.guest.vmrun), ("credentials", lambda base_vm: {"user": "tester", "password": "pw"})):
            patcher = mock.patch.object(fusion, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        vm = mock.Mock(vmx="/vms/guest-1.vmx", auth=[])
        vm.name = "guest-1"
        self.channel = fusion.WindowsVmrunChannel(vm, "vmlab-base-windows-11")

    def test_a_file_is_sent_to_the_call_folder_before_any_call_made_it(self):
        with tempfile.NamedTemporaryFile() as local:
            self.channel.send_file(local.name, windows.CALL_DIR + "\\vmlab-copy.tar", 5)
        self.assertEqual(self.guest.files, [windows.CALL_DIR + "\\vmlab-copy.tar"])


if __name__ == "__main__":
    unittest.main()
