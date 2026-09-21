import json
import time
import xml.etree.ElementTree as ET

from harness import FAKE_LAB, VmlabTestCase

ECHO = """
def scenario(g):
    r = g.exec(["echo", "hello"])
    g.check("echo prints hello", r.stdout.strip() == "hello")
    g.check("served by %s", r.channel == "%s")
"""


class ChannelFallbackTest(VmlabTestCase):
    def test_channel_failure_falls_back_to_the_next_channel_and_the_report_says_so(self):
        self.project.config(FAKE_LAB + """
            [labs.mac.fake]
            channels = ["ssh", "exec"]
            broken_channels = ["ssh"]
        """)
        self.project.scenario("echo.py", ECHO % ("exec", "exec"))

        r = self.project.vmlab("run")

        self.assertExit(r, 0)
        run_dir = self.project.only_run_dir()
        [scenario] = self.project.report(run_dir)["scenarios"]
        self.assertEqual(scenario["channels"], {"exec": 1})
        [fallback] = scenario["fallbacks"]
        self.assertEqual((fallback["from"], fallback["to"]), ("ssh", "exec"))
        self.assertIn("broken", fallback["reason"])
        self.assertIn("ssh", (run_dir / "summary.md").read_text())

    def test_the_preferred_channel_serves_when_healthy(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\n')
        self.project.scenario("echo.py", ECHO % ("ssh", "ssh"))

        self.assertExit(self.project.vmlab("run"), 0)
        [scenario] = self.project.report()["scenarios"]
        self.assertEqual(scenario["channels"], {"ssh": 1})
        self.assertEqual(scenario["fallbacks"], [])

    def test_a_command_exiting_nonzero_does_not_fall_back(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\n')
        self.project.scenario("exit3.py", """
            def scenario(g):
                r = g.exec(["sh", "-c", "echo oops >&2; exit 3"])
                g.check("exit code is passed through", r.code == 3 and not r.ok)
                g.check("stderr is passed through", r.stderr.strip() == "oops")
                g.check("served by ssh", r.channel == "ssh")
        """)

        self.assertExit(self.project.vmlab("run"), 0)
        [scenario] = self.project.report()["scenarios"]
        self.assertEqual(scenario["channels"], {"ssh": 1})
        self.assertEqual(scenario["fallbacks"], [])

    def test_all_channels_failing_is_a_clear_run_error(self):
        self.project.config(FAKE_LAB + """
            [labs.mac.fake]
            channels = ["ssh", "exec"]
            broken_channels = ["ssh", "exec"]
        """)
        self.project.scenario("echo.py", ECHO % ("ssh", "ssh"))

        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        [scenario] = self.project.report()["scenarios"]
        self.assertEqual(scenario["status"], "error")
        for fragment in ("echo.py:3", "no Channel", "ssh", "exec", "vmlab doctor"):
            self.assertIn(fragment, scenario["error"])

    def test_unknown_fault_channel_is_a_config_error(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh"]\nbroken_channels = ["exec"]\n')
        self.project.scenario("echo.py", ECHO % ("ssh", "ssh"))
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn("labs.mac.fake.broken_channels", r.err)


class TimeoutTest(VmlabTestCase):
    def test_a_hung_command_is_killed_and_fails_the_run_with_a_timeout_error(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("a_hang.py", """
            def scenario(g):
                g.exec(["sh", "-c", "sleep 2; touch finished-after-timeout"], timeout=1)
                g.check("unreachable", True)
        """)
        self.project.scenario("b_after.py", """
            def scenario(g):
                r = g.exec(["sh", "-c", "sleep 2; ls"])
                g.check("the timed-out command was killed", "finished-after-timeout" not in r.stdout, detail=r.stdout)
        """)

        started = time.time()
        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        self.assertLess(time.time() - started, 15)
        hang, after = self.project.report()["scenarios"]
        self.assertEqual(hang["status"], "error")
        self.assertIn("a_hang.py:3", hang["error"])
        self.assertIn("timed out after 1s", hang["error"])
        self.assertEqual(after["status"], "passed", after)
        self.assertIn("timed out after 1s", r.out)

    def test_a_hung_channel_times_out_instead_of_falling_back(self):
        self.project.config(FAKE_LAB + """
            [labs.mac.fake]
            channels = ["ssh", "exec"]
            hung_channels = ["ssh"]
        """)
        self.project.scenario("echo.py", """
            def scenario(g):
                g.exec(["echo", "hi"], timeout=1)
                g.check("unreachable", True)
        """)

        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        [scenario] = self.project.report()["scenarios"]
        self.assertIn("timed out after 1s on Channel ssh", scenario["error"])
        suite = ET.parse(str(self.project.only_run_dir() / "junit.xml")).getroot()
        self.assertEqual(suite.get("errors"), "1")


class BootTest(VmlabTestCase):
    def status(self):
        return {g["lab"]: g["running"] for g in json.loads(self.project.vmlab("status", "--json").out)}

    def test_up_waits_for_a_slow_boot(self):
        self.project.config(FAKE_LAB + "[labs.mac.fake]\nboot_seconds = 1\n")
        self.project.scenario("echo.py", ECHO % ("ssh", "ssh"))

        started = time.time()
        r = self.project.vmlab("run")

        self.assertExit(r, 0)
        self.assertGreaterEqual(time.time() - started, 1)

    def test_a_guest_that_does_not_come_up_in_time_is_a_lab_error(self):
        self.project.config(FAKE_LAB.replace('arch = "arm64"', 'arch = "arm64"\nboot_timeout = 1') + """
            [labs.mac.fake]
            boot_seconds = 30
        """)
        self.project.scenario("echo.py", ECHO % ("ssh", "ssh"))

        started = time.time()
        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        self.assertLess(time.time() - started, 15)
        self.assertIn("not reachable within 1s", r.out)
        report = self.project.report()
        self.assertEqual(report["status"], "error")
        self.assertIn("not reachable within 1s", report["error"])
        suite = ET.parse(str(self.project.only_run_dir() / "junit.xml")).getroot()
        self.assertEqual(suite.get("errors"), "1")
        self.assertIn("not reachable", (self.project.only_run_dir() / "summary.md").read_text())
        self.assertEqual(self.status(), {"mac": False})

    def test_vmlab_up_reports_a_guest_that_does_not_come_up(self):
        self.project.config(FAKE_LAB.replace('arch = "arm64"', 'arch = "arm64"\nboot_timeout = 1') + """
            [labs.mac.fake]
            boot_seconds = 30
        """)
        r = self.project.vmlab("up")
        self.assertExit(r, 1)
        self.assertIn("not reachable within 1s", r.err)
