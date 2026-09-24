"""g.put / g.get and `vmlab put` / `vmlab get`: files into and out of a Guest."""

import os
import subprocess
import sys
import time

from harness import FAKE_LAB, VmlabTestCase, zipapp_path

BINARY = bytes(range(256)) * 64 + b"\r\n\x00end"
TEXT = "Ohm's law: U = I·R, Закон Ома ✓\n"


class ScenarioFilesTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.project.config(FAKE_LAB)

    def run_scenario(self, body):
        self.project.scenario("files.py", body)
        r = self.project.vmlab("run")
        return r, self.project.report()["scenarios"][0]

    def assertPasses(self, body):
        r, scenario = self.run_scenario(body)
        self.assertExit(r, 0)
        self.assertEqual(scenario["status"], "passed", scenario)

    def test_str_and_bytes_round_trip(self):
        self.assertPasses("""
def scenario(g):
    text = %r
    data = %r
    g.put("~/text.txt", text)
    g.put("~/data.bin", data)
    g.check("str back as str", g.get("~/text.txt") == text, detail=repr(g.get("~/text.txt")))
    g.check("bytes back as bytes", g.get("~/data.bin", binary=True) == data)
    g.check("str is written as UTF-8", g.get("~/text.txt", binary=True) == text.encode("utf-8"))
    g.put("~/text.txt", "shorter")
    g.check("put replaces a file", g.get("~/text.txt") == "shorter")
""" % (TEXT, BINARY))

    def test_parent_folders_are_created_and_tilde_is_the_guest_home(self):
        self.assertPasses("""
def scenario(g):
    path = g.put("~/deep/er/notes.txt", "hi")
    g.check("absolute Guest path returned", path.startswith("/") and path.endswith("/deep/er/notes.txt"), detail=path)
    r = g.exec(["cat", "home/deep/er/notes.txt"])  # the Fake Guest's home, from its root
    g.check("written in the Guest's home", r.stdout == "hi", detail=r.stdout + r.stderr)
""")

    def test_a_missing_file_raises_with_the_path(self):
        r, scenario = self.run_scenario("""
def scenario(g):
    g.get("~/no/such-file.txt")
    g.check("unreachable", True)
""")
        self.assertExit(r, 1)
        self.assertEqual(scenario["status"], "error")
        self.assertIn("files.py:3", scenario["error"])
        self.assertIn("~/no/such-file.txt", scenario["error"])

    def test_content_must_be_str_or_bytes(self):
        r, scenario = self.run_scenario("""
def scenario(g):
    g.put("~/x.txt", 42)
    g.check("unreachable", True)
""")
        self.assertEqual(scenario["status"], "error")
        self.assertIn("str or bytes", scenario["error"])

    def test_a_non_utf8_file_read_as_text_says_binary(self):
        r, scenario = self.run_scenario("""
def scenario(g):
    g.put("~/x.bin", b"\\xff\\xfe\\x00")
    g.get("~/x.bin")
    g.check("unreachable", True)
""")
        self.assertEqual(scenario["status"], "error")
        self.assertIn("binary=True", scenario["error"])

    def test_put_and_get_are_on_the_scenario_clock(self):
        r, scenario = self.run_scenario("""
import time
TIMEOUT = 1
def scenario(g):
    time.sleep(1.2)
    g.put("~/late.txt", "late")
    g.check("unreachable", True)
""")
        self.assertEqual(scenario["status"], "error")
        self.assertIn("1s timeout (before put", scenario["error"])

    def test_a_hung_get_ends_with_the_scenario(self):
        self.project.config(FAKE_LAB + 'step_timeout = 60\n[labs.mac.fake]\nhung_channels = ["ssh", "exec"]\n')
        started = time.time()
        r, scenario = self.run_scenario("""
TIMEOUT = 1
def scenario(g):
    g.put("~/a.txt", "a")
    g.get("~/a.txt")
    g.check("unreachable", True)
""")
        self.assertLess(time.time() - started, 30)
        self.assertEqual(scenario["status"], "error")
        self.assertIn("1s timeout (during get", scenario["error"])

    def test_get_takes_the_labs_step_timeout(self):
        self.project.config(FAKE_LAB + 'step_timeout = 1\n[labs.mac.fake]\nhung_channels = ["ssh", "exec"]\n')
        r, scenario = self.run_scenario("""
def scenario(g):
    g.get("~/a.txt")
    g.check("unreachable", True)
""")
        self.assertEqual(scenario["status"], "error")
        self.assertIn("timed out after 1", scenario["error"])


class CliFilesTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.project.config(FAKE_LAB)

    def vmlab_bytes(self, *args, stdin=b""):
        """vmlab with bytes in and out, as a shell pipe would give it."""
        return subprocess.run(
            [sys.executable, str(zipapp_path())] + list(args),
            cwd=str(self.project.root / "app"),
            env=self.project.environ(),
            input=stdin,
            capture_output=True,
            timeout=60,
        )

    def test_put_from_stdin_and_get_to_stdout_round_trip_text_and_binary(self):
        self.assertExit(self.project.vmlab("up"), 0)
        for path, data in (("~/in/text.txt", TEXT.encode("utf-8")), ("~/in/data.bin", BINARY)):
            put = self.vmlab_bytes("put", path, stdin=data)
            self.assertEqual(put.returncode, 0, put.stderr)
            self.assertTrue(put.stdout.decode().strip().endswith(path[1:]), put.stdout)
            got = self.vmlab_bytes("get", path)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(got.stdout, data)

    def test_put_from_a_host_file(self):
        self.assertExit(self.project.vmlab("up"), 0)
        source = self.project.root / "source.bin"
        source.write_bytes(BINARY)
        put = self.vmlab_bytes("put", "~/copy.bin", "--from", str(source), "--lab", "mac")
        self.assertEqual(put.returncode, 0, put.stderr)
        self.assertEqual(self.vmlab_bytes("get", "~/copy.bin", "--lab", "mac").stdout, BINARY)

    def test_a_missing_host_file_is_a_usage_error(self):
        self.assertExit(self.project.vmlab("up"), 0)
        r = self.project.vmlab("put", "~/x", "--from", "no-such-file")
        self.assertExit(r, 2)
        self.assertIn("no-such-file", r.err)

    def test_get_of_a_missing_file_fails_naming_it(self):
        self.assertExit(self.project.vmlab("up"), 0)
        r = self.project.vmlab("get", "~/nothing-here.txt")
        self.assertExit(r, 1)
        self.assertIn("~/nothing-here.txt", r.err)

    def test_put_with_a_terminal_for_stdin_says_how_to_give_content_instead_of_waiting(self):
        self.assertExit(self.project.vmlab("up"), 0)
        leader, terminal = os.openpty()
        self.addCleanup(os.close, leader)
        try:
            put = subprocess.run(
                [sys.executable, str(zipapp_path()), "put", "~/x.txt"],
                cwd=str(self.project.root / "app"), env=self.project.environ(), stdin=terminal, capture_output=True, timeout=30,
            )  # fmt: skip
        finally:
            os.close(terminal)
        self.assertEqual(put.returncode, 2, put.stderr)
        self.assertIn(b"--from", put.stderr)

    def test_put_to_a_stopped_guest_fails_without_waiting_for_stdin(self):
        # A pipe nobody writes to or closes: reading it first would wait for good.
        with subprocess.Popen(
            [sys.executable, str(zipapp_path()), "put", "~/x.txt"],
            cwd=str(self.project.root / "app"), env=self.project.environ(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as put:  # fmt: skip
            try:
                code = put.wait(timeout=30)
            finally:
                put.stdin.close()
            err = put.stderr.read()
        self.assertEqual(code, 1, err)
        self.assertIn(b"vmlab up mac", err)

    def test_put_of_an_empty_pipe_writes_an_empty_file(self):
        self.assertExit(self.project.vmlab("up"), 0)
        put = self.vmlab_bytes("put", "~/empty.txt", stdin=b"")
        self.assertEqual(put.returncode, 0, put.stderr)
        got = self.vmlab_bytes("get", "~/empty.txt")
        self.assertEqual((got.returncode, got.stdout), (0, b""), got.stderr)

    def test_a_stopped_guest_is_an_error(self):
        r = self.project.vmlab("get", "~/x")
        self.assertExit(r, 1)
        self.assertIn("vmlab up mac", r.err)
