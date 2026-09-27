import json
import time

from harness import FAKE_LAB, VmlabTestCase

TWO_LABS = FAKE_LAB + """
[labs.ubuntu]
provider = "fake"
os = "linux"
"""


class UpDownTest(VmlabTestCase):
    def status(self):
        r = self.project.vmlab("status", "--json")
        self.assertExit(r, 0)
        return {g["lab"]: g["running"] for g in json.loads(r.out)}

    def test_guests_start_stopped(self):
        self.project.config(TWO_LABS)
        self.assertEqual(self.status(), {"mac": False, "ubuntu": False})

    def test_up_and_down_one_lab(self):
        self.project.config(TWO_LABS)

        self.assertExit(self.project.vmlab("up", "mac"), 0)
        self.assertEqual(self.status(), {"mac": True, "ubuntu": False})

        self.assertExit(self.project.vmlab("down", "mac"), 0)
        self.assertEqual(self.status(), {"mac": False, "ubuntu": False})

    def test_up_and_down_without_a_lab_act_on_all_labs(self):
        self.project.config(TWO_LABS)

        self.assertExit(self.project.vmlab("up"), 0)
        self.assertEqual(self.status(), {"mac": True, "ubuntu": True})

        self.assertExit(self.project.vmlab("down"), 0)
        self.assertEqual(self.status(), {"mac": False, "ubuntu": False})

    def test_up_prints_each_step_as_it_starts_and_ends(self):
        self.project.config(FAKE_LAB)

        r = self.project.vmlab("up", "mac")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"mac: booting\n")
        self.assertRegex(r.out, r"mac: booting done in \d+s\n")
        self.assertTrue(r.out.endswith("mac running\n"), r.out)

    def test_step_lines_reach_a_pipe_as_they_happen(self):
        # Not a terminal, as in CI or `vmlab up > log`: a step still running must show there.
        self.project.config(FAKE_LAB + """
[labs.mac.fake]
boot_seconds = 3
""")
        started = time.monotonic()
        proc = self.project.vmlab_background("up", "mac")
        self.addCleanup(proc.communicate)

        first = proc.stdout.readline()

        self.assertEqual(first.strip(), "mac: booting")
        self.assertLess(time.monotonic() - started, 2, "the line came only as vmlab exited, after the 3 s boot")

    def test_up_and_down_are_idempotent(self):
        self.project.config(FAKE_LAB)
        self.assertExit(self.project.vmlab("up", "mac"), 0)
        self.assertExit(self.project.vmlab("up", "mac"), 0)
        self.assertEqual(self.status(), {"mac": True})
        self.assertExit(self.project.vmlab("down", "mac"), 0)
        self.assertExit(self.project.vmlab("down", "mac"), 0)
        self.assertEqual(self.status(), {"mac": False})

    def test_unknown_lab(self):
        self.project.config(FAKE_LAB)
        r = self.project.vmlab("up", "win")
        self.assertExit(r, 2)
        self.assertIn("labs.win", r.err)

    def test_run_stops_a_guest_it_started(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("smoke.py", 'def scenario(g):\n    g.check("ok", True)\n')

        self.assertExit(self.project.vmlab("run"), 0)
        self.assertEqual(self.status(), {"mac": False})

    def test_run_leaves_a_guest_the_user_started_running(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("smoke.py", 'def scenario(g):\n    g.check("ok", True)\n')
        self.assertExit(self.project.vmlab("up", "mac"), 0)

        self.assertExit(self.project.vmlab("run"), 0)
        self.assertEqual(self.status(), {"mac": True})
