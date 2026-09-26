"""`deploy` and `run` print each step as it starts, prefixed with its Lab, and its time when it ends.

The duration format is tested by loading vmlab.progress directly; the rest drives the zipapp.
"""

import re
import sys
from pathlib import Path

from harness import FAKE_LAB, VmlabTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab import progress  # noqa: E402

APP = """
[labs.mac.fake]
boot_seconds = 0.3
[labs.mac.app]
artifact = "dist/MyApp.zip"
build = "mkdir -p dist && echo app-bytes > dist/MyApp.zip"
inputs = ["src"]
install = "cp \\"$VMLAB_ARTIFACT\\" ~/installed"
launch = "touch ~/ready"
ready = { file = "~/ready" }
"""

PASS = 'def scenario(g):\n    g.check("ok", True)\n'
STEP = re.compile(r"^(\w+): (building|cloning|deleting the old clone|booting|quitting|waiting for |restoring Clean state|delivering|installing|launching|scenario )")


def steps(out):
    """The step lines of out, their times replaced by N so they can be compared."""
    return [re.sub(r" done in \S+$", " done in N", line) for line in out.splitlines() if STEP.match(line)]


class DurationTest(VmlabTestCase):
    def test_seconds_are_rounded_and_minutes_shown_above_one(self):
        self.assertEqual([progress.duration(s) for s in (0.2, 41.4, 59.6, 125, 3600)], ["0s", "41s", "1m00s", "2m05s", "60m00s"])


class DeployProgressTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        (self.project.root / "app" / "src").mkdir(parents=True)
        self.project.config(FAKE_LAB + APP)
        self.project.scenario("ok.py", PASS)

    def test_deploy_prints_every_step_as_it_starts_and_ends(self):
        r = self.project.vmlab("deploy")

        self.assertExit(r, 0)
        self.assertEqual(steps(r.out), [
            "mac: building",
            "mac: building done in N",
            "mac: booting",
            "mac: booting done in N",
            "mac: waiting for Channels",
            "mac: waiting for Channels done in N",
            "mac: delivering",
            "mac: delivering done in N",
            "mac: installing",
            "mac: installing done in N",
            "mac: launching",
            "mac: launching done in N",
            'mac: waiting for ready {"file": "~/ready"}',
            "mac: waiting for ready done in N",
        ])  # fmt: skip
        self.assertRegex(r.out, r"mac: building done in \d+s\n")
        self.assertRegex(r.out, r"\nmac: deployed ")

    def test_steps_that_do_not_happen_print_nothing(self):
        self.assertExit(self.project.vmlab("deploy"), 0)

        r = self.project.vmlab("deploy")  # the Guest runs and the artifact is fresh

        self.assertExit(r, 0)
        self.assertEqual(steps(r.out)[:2], ["mac: delivering", "mac: delivering done in N"])

    def test_a_failing_step_prints_no_done_line(self):
        self.project.config(FAKE_LAB + APP.replace("touch ~/ready", "echo no display >&2; exit 1"))

        r = self.project.vmlab("deploy")

        self.assertExit(r, 1)
        self.assertEqual(steps(r.out)[-2:], ["mac: installing done in N", "mac: launching"])
        self.assertIn("launch recipe", r.err)

    def test_quiet_prints_what_deploy_printed_before(self):
        r = self.project.vmlab("deploy", "--quiet")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"^mac: deployed ")
        self.assertEqual(steps(r.out), [])

    def test_quiet_leaves_out_only_the_step_lines(self):
        self.project.vmlab("deploy")  # builds: the next two do not
        self.project.vmlab("down")
        loud = self.project.vmlab("deploy")
        self.project.vmlab("down")
        quiet = self.project.vmlab("deploy", "--quiet")

        self.assertExit(quiet, 0)
        same = lambda out: [re.sub(r"\d+", "N", line) for line in out.splitlines() if not STEP.match(line)]  # noqa: E731
        self.assertEqual(same(quiet.out), same(loud.out))

    def test_the_quit_before_launching_is_a_step(self):
        self.project.config(FAKE_LAB + APP + 'quit = "rm -f ~/ready"\n')

        r = self.project.vmlab("deploy")

        self.assertExit(r, 0)
        self.assertEqual(steps(r.out)[-6:-4], ["mac: quitting", "mac: quitting done in N"])


class RunProgressTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        (self.project.root / "app" / "src").mkdir(parents=True)
        self.project.config(FAKE_LAB + APP)
        self.project.scenario("ok.py", PASS)
        self.project.scenario("again.py", PASS)

    def test_run_prints_its_steps_and_one_line_per_scenario(self):
        r = self.project.vmlab("run")

        self.assertExit(r, 0)
        self.assertEqual(steps(r.out), [
            "mac: building (log: %s/build.log)" % self.project.report()["run_dir"],
            "mac: building done in N",
            "mac: booting",
            "mac: booting done in N",
            "mac: waiting for Channels",
            "mac: waiting for Channels done in N",
            "mac: restoring Clean state",
            "mac: restoring Clean state done in N",
            "mac: delivering",
            "mac: delivering done in N",
            "mac: installing",
            "mac: installing done in N",
            "mac: scenario again",
            "mac: scenario ok",
        ])  # fmt: skip

    def test_quiet_prints_what_run_printed_before(self):
        r = self.project.vmlab("run", "--quiet")

        self.assertExit(r, 0)
        self.assertEqual(steps(r.out), [])
        self.assertRegex(r.out, r"^PASSED mac: 2 Scenario\(s\)")

    def test_parallel_lines_carry_their_lab_and_never_interleave(self):
        self.project.config(FAKE_LAB + APP + APP.replace("labs.mac", "labs.mac2").replace("[labs.mac2.fake]", '[labs.mac2]\nprovider = "fake"\nos = "macos"\n[labs.mac2.fake]'))

        r = self.project.vmlab("run", "--parallel")

        self.assertExit(r, 0)
        for lab in ("mac", "mac2"):
            self.assertIn("%s: scenario ok" % lab, r.out)
            self.assertIn("%s: waiting for Channels" % lab, r.out)
        for line in r.out.splitlines():
            self.assertRegex(line, r"^(mac2?: |PASSED mac2?: |queued |warning: )", "a line of its own, with its Lab")
