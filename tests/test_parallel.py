"""Labs run sequentially by default; --parallel runs them concurrently as free memory allows."""

import json

from harness import VmlabTestCase

LABS = """
[labs.mac]
provider = "fake"
os = "macos"
memory_gb = 8

[labs.ubuntu]
provider = "fake"
os = "linux"
memory_gb = 8
"""

# Records when the Scenario ran on each Lab, so tests can tell whether Labs overlapped.
TIMED = """
import json, time

def scenario(g):
    start = time.time()
    g.exec(["sleep", "1"])
    with open(%r + "/" + g.lab + ".json", "w") as f:
        json.dump([start, time.time()], f)
    g.check("ran", True)
"""


class ParallelTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.times = self.project.root / "times"
        self.times.mkdir()
        self.project.config(LABS)
        self.project.scenario("timed.py", TIMED % str(self.times))

    def overlapped(self):
        (a0, a1), (b0, b1) = (json.loads((self.times / n).read_text()) for n in ("mac.json", "ubuntu.json"))
        return a0 < b1 and b0 < a1

    def run_vmlab(self, *args, free_gb):
        r = self.project.vmlab("run", *args, env={"VMLAB_FREE_MEMORY_GB": str(free_gb)})
        self.assertExit(r, 0)
        return r

    def test_labs_run_sequentially_by_default(self):
        self.run_vmlab(free_gb=64)
        self.assertFalse(self.overlapped())

    def test_parallel_runs_labs_concurrently_when_memory_allows(self):
        self.run_vmlab("--parallel", free_gb=64)
        self.assertTrue(self.overlapped())

    def test_labs_that_do_not_fit_are_queued_not_failed(self):
        r = self.run_vmlab("--parallel", free_gb=10)
        self.assertFalse(self.overlapped())
        self.assertIn("queued ubuntu", r.out)
        self.assertIn("8 GB", r.out)

    def test_a_lab_bigger_than_free_memory_still_runs_alone(self):
        r = self.run_vmlab("--parallel", free_gb=4)
        self.assertFalse(self.overlapped())
        self.assertIn("more memory than is free", r.out)

    def test_an_already_running_guest_needs_no_memory(self):
        self.assertExit(self.project.vmlab("up", "mac"), 0)
        self.run_vmlab("--parallel", free_gb=10)
        self.assertTrue(self.overlapped())

    def test_per_lab_reports_stay_separate_and_correct(self):
        self.project.scenario("fails_on_linux.py", """
            def scenario(g):
                g.check("not linux", g.os != "linux")
        """)
        r = self.project.vmlab("run", "--parallel", env={"VMLAB_FREE_MEMORY_GB": "64"})

        self.assertExit(r, 1)
        reports = {self.project.report(d)["lab"]: self.project.report(d) for d in self.project.run_dirs()}
        self.assertEqual(sorted(reports), ["mac", "ubuntu"])
        self.assertEqual(reports["mac"]["status"], "passed")
        self.assertEqual(reports["ubuntu"]["status"], "failed")
        for lab, rep in reports.items():
            self.assertEqual([s["name"] for s in rep["scenarios"]], ["fails_on_linux", "timed"])
            self.assertTrue(rep["run_dir"].endswith("-" + lab))

    def test_memory_gb_must_be_positive(self):
        self.project.config(LABS.replace("memory_gb = 8", "memory_gb = 0", 1))
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn("labs.mac.memory_gb", r.err)
