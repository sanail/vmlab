"""vmlab run --repeat N [--until-fail]: the selection N times on each Lab, and how often each Check passed."""

import json
import os
import re
import signal
import time

from harness import FAKE_LAB, VmlabTestCase

BUILT = FAKE_LAB + """
[labs.mac.app]
artifact = "dist/app.txt"
build = "mkdir -p dist && echo build >> build-count && echo app > dist/app.txt"
"""

TWO_LABS = FAKE_LAB + """
[labs.ubuntu]
provider = "fake"
os = "linux"
"""

# Counts its repetitions in a Host file per Lab; its Check fails in the ones listed for the Lab.
FLAKY = """
from pathlib import Path

def scenario(g):
    count = Path(%r) / g.lab
    n = int(count.read_text()) + 1 if count.exists() else 1
    count.write_text(str(n))
    g.check("the palette opens", True)
    g.check("the palette closes", n not in %r.get(g.lab, ()), detail="repetition %%d" %% n)
"""


class RepeatTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.counts = self.project.root / "counts"
        self.counts.mkdir()

    def flaky(self, fails_in):
        """The Scenario flaky.py, whose second Check fails in these repetitions ({lab: [n, ...]})."""
        self.project.scenario("flaky.py", FLAKY % (str(self.counts), fails_in))

    def events(self, lab="mac"):
        [log] = [p for p in self.project.home.glob("fake/*/commands.jsonl") if p.parent.name.endswith("-" + lab)]
        return [json.loads(line)["event"] for line in log.read_text().splitlines()]

    def status(self):
        r = self.project.vmlab("status", "--json")
        self.assertExit(r, 0)
        return {g["lab"]: g["running"] for g in json.loads(r.out)}

    def test_repeat_runs_the_selection_n_times_with_one_build_and_one_guest_start(self):
        self.project.config(BUILT)
        self.flaky({})

        r = self.project.vmlab("run", "--repeat", "3")

        self.assertExit(r, 0)
        run_dirs = self.project.run_dirs()
        self.assertEqual(len(run_dirs), 3)
        self.assertEqual([self.project.report(d)["status"] for d in run_dirs], ["passed"] * 3)
        self.assertEqual((self.project.root / "app" / "build-count").read_text(), "build\n")
        self.assertEqual([e for e in self.events() if e in ("up", "down", "restore")], ["up", "restore", "restore", "restore", "down"])
        for i, run_dir in enumerate(run_dirs, 1):
            self.assertIn("PASSED mac (repetition %d of 3): 1 Scenario(s)" % i, r.out)
            self.assertRegex(r.out, r"report: \S*/%s\n" % re.escape(run_dir.name))
        self.assertIn("mac/flaky: the palette opens: passed 3 of 3", r.out)
        self.assertIn("mac/flaky: the palette closes: passed 3 of 3", r.out)
        self.assertEqual(self.status(), {"mac": False})

    def test_a_check_failing_in_one_repetition_passed_the_others(self):
        self.project.config(FAKE_LAB)
        self.flaky({"mac": [2]})

        r = self.project.vmlab("run", "--repeat", "3")

        self.assertExit(r, 1)
        self.assertEqual([self.project.report(d)["status"] for d in self.project.run_dirs()], ["passed", "failed", "passed"])
        self.assertIn("FAILED mac (repetition 2 of 3)", r.out)
        self.assertIn("FAIL mac/flaky: the palette closes: repetition 2", r.out)
        self.assertIn("mac/flaky: the palette opens: passed 3 of 3", r.out)
        self.assertIn("mac/flaky: the palette closes: passed 2 of 3", r.out)

    def test_skipped_checks_are_counted_apart_and_errored_scenarios_per_scenario(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("sometimes_breaks.py", """
            from pathlib import Path

            def scenario(g):
                count = Path(%r) / "n"
                n = int(count.read_text()) + 1 if count.exists() else 1
                count.write_text(str(n))
                g.check("before the helper", True)
                if n == 2:
                    raise RuntimeError("helper crashed")
                if n == 3:
                    g.skip("after the helper", "the helper is not there")
                else:
                    g.check("after the helper", True)
        """ % str(self.counts))

        r = self.project.vmlab("run", "--repeat", "3")

        self.assertExit(r, 1)
        self.assertIn("mac/sometimes_breaks: errored 1 of 3", r.out)
        self.assertIn("mac/sometimes_breaks: before the helper: passed 3 of 3", r.out)
        self.assertIn("mac/sometimes_breaks: after the helper: passed 1 of 1, skipped 1", r.out)

    def test_until_fail_stops_at_the_first_failure_and_keeps_the_guest(self):
        self.project.config(FAKE_LAB)
        self.flaky({"mac": [2]})

        r = self.project.vmlab("run", "--repeat", "5", "--until-fail")

        self.assertExit(r, 1)
        self.assertEqual(len(self.project.run_dirs()), 2)
        self.assertIn("mac/flaky: the palette closes: passed 1 of 2", r.out)
        self.assertIn("Kept running: mac.", r.out)
        self.assertEqual(self.status(), {"mac": True})

    def test_until_fail_without_a_failure_is_repeat(self):
        self.project.config(FAKE_LAB)
        self.flaky({})

        r = self.project.vmlab("run", "--repeat", "3", "--until-fail")

        self.assertExit(r, 0)
        self.assertEqual(len(self.project.run_dirs()), 3)
        self.assertNotIn("Kept running", r.out)
        self.assertEqual(self.status(), {"mac": False})

    def test_until_fail_stops_every_lab_in_parallel(self):
        self.project.config(TWO_LABS)
        self.flaky({"ubuntu": [1]})

        r = self.project.vmlab("run", "--repeat", "5", "--until-fail", "--parallel", env={"VMLAB_FREE_MEMORY_GB": "64"})

        self.assertExit(r, 1)
        reports = [self.project.report(d) for d in self.project.run_dirs()]
        self.assertEqual([rep["status"] for rep in reports if rep["lab"] == "ubuntu"], ["failed"])
        self.assertLess(len([rep for rep in reports if rep["lab"] == "mac"]), 5)
        self.assertIn("Kept running: ", r.out)
        self.assertEqual(self.status(), {"mac": True, "ubuntu": True})

    def test_until_fail_in_sequence_runs_no_later_lab(self):
        self.project.config(TWO_LABS)
        self.flaky({"mac": [1]})

        r = self.project.vmlab("run", "--repeat", "3", "--until-fail")

        self.assertExit(r, 1)
        self.assertEqual([self.project.report(d)["lab"] for d in self.project.run_dirs()], ["mac"])
        self.assertIn("ubuntu: not run: --until-fail stopped at a failure", r.out)
        self.assertEqual(self.status(), {"mac": True, "ubuntu": False})

    def test_an_ad_hoc_run_repeated_does_not_restore(self):
        self.project.config(FAKE_LAB)
        adhoc = self.project.root / "adhoc"
        adhoc.mkdir()
        (adhoc / "grows.py").write_text(
            'def scenario(g):\n'
            '    g.exec(["sh", "-c", "echo x >> marker"])\n'
            '    g.check("marker grows", True, detail=g.exec(["cat", "marker"]).stdout)\n'
        )

        r = self.project.vmlab("run", adhoc / "grows.py", "--repeat", "3")

        self.assertExit(r, 0)
        self.assertNotIn("restore", self.events())
        details = [self.project.report(d)["scenarios"][0]["checks"][0]["detail"] for d in self.project.run_dirs()]
        self.assertEqual(details, ["x\n", "x\nx\n", "x\nx\nx\n"])

    def test_a_check_recorded_twice_counts_once_per_repetition(self):
        self.project.config(FAKE_LAB)
        self.flaky({"mac": [2]})
        self.project.scenario("twice.py", """
            def scenario(g):
                g.check("the palette opens", True)
                g.check("the palette opens", g.exec(["test", "-e", "marker"]).ok is False)
                g.exec(["touch", "marker"])
        """)

        r = self.project.vmlab("run", "twice", "--repeat", "2", "--fresh")

        self.assertExit(r, 0)
        self.assertIn("mac/twice: the palette opens: passed 2 of 2\n", r.out)

    def test_repeat_one_prints_and_reports_as_a_single_run(self):
        self.project.config(BUILT)
        self.flaky({})

        def run(*args):
            before = set(self.project.run_dirs())
            r = self.project.vmlab("run", *args)
            self.assertExit(r, 0)
            (run_dir,) = set(self.project.run_dirs()) - before
            self.counts.joinpath("mac").unlink()
            report = self.project.report(run_dir)
            for key in ("started_at", "duration_s", "run_dir"):
                del report[key]
            return re.sub(r"\S*/runs/\S+", "RUN_DIR", r.out), report

        run()  # builds the Build artifact, so neither Run below does
        without_flag = run()
        self.assertEqual(run("--repeat", "1"), without_flag)
        self.assertNotIn("repetition", without_flag[0])

    def test_ctrl_c_tallies_the_repetitions_that_ended(self):
        self.project.config(FAKE_LAB)
        ready = self.counts / "ready"
        self.project.scenario("hangs_second.py", """
            import time
            from pathlib import Path

            def scenario(g):
                ready = Path(%r)
                if ready.parent.joinpath("first").exists():
                    ready.write_text("")
                    time.sleep(30)
                ready.parent.joinpath("first").write_text("")
                g.check("ran", True)
        """ % str(ready))
        proc = self.project.vmlab_background("run", "--repeat", "3")
        self.addCleanup(proc.kill)
        deadline = time.time() + 30
        while not ready.exists():
            self.assertLess(time.time(), deadline, "the second repetition never got going")
            time.sleep(0.05)
        os.kill(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=60)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("KeyboardInterrupt", err)
        self.assertIn("mac: 1 of 1 repetition(s) passed\n", out)
        self.assertIn("mac/hangs_second: ran: passed 1 of 1\n", out)
        self.assertIn("Guests stopped: mac\n", out)

    def test_usage_errors(self):
        self.project.config(FAKE_LAB)
        self.flaky({})
        for args, message in (
            (["--repeat", "0"], "--repeat must be at least 1"),
            (["--repeat", "-1"], "--repeat must be at least 1"),
            (["--until-fail"], "--until-fail goes with --repeat N"),
        ):
            r = self.project.vmlab("run", *args)
            self.assertExit(r, 2)
            self.assertIn(message, r.err)
        self.assertEqual(self.project.run_dirs(), [])
