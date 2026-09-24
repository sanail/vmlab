"""g.spawn: background processes in the Guest, stopped with the Run.

The Fake Provider runs Guest commands on the Host, so a spawned process is a
Host process here: the tests look it up by pid, and kill whatever is left.
"""

import os
import re
import signal
import time

from harness import FAKE_LAB, VmlabTestCase

# A process with a child: it writes the child's pid to ~/child.pid, then prints and waits.
PARENT_AND_CHILD = '["sh", "-c", "sleep 300 & echo $! > \\"$HOME/child.pid\\"; echo started; echo oops >&2; sleep 300"]'
# A process that starts a child and exits, leaving the child in its process group.
ORPHAN = '["sh", "-c", "sleep 300 & echo $! > \\"$HOME/child.pid\\""]'


def gone(pid, within=5):
    """Has process pid ended (and been reaped) within the given seconds?"""
    deadline = time.time() + within
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True  # the pid was reused by someone else's process
        time.sleep(0.05)
    return False


class SpawnCase(VmlabTestCase):
    """A Fake Lab, and no spawned process left behind on the Host."""

    def setUp(self):
        super().setUp()
        self.project.config(FAKE_LAB)
        self.addCleanup(self.kill_leftovers)

    def kill_leftovers(self):
        pids = [p["pid"] for d in self.project.run_dirs() if (d / "report.json").exists() for s in self.project.report(d)["scenarios"] for p in s.get("spawned", [])]
        pids += [int(p.read_text()) for p in self.project.home.glob("fake/*/fs/home/child.pid") if p.read_text().strip()]
        for pid in pids:
            for kill in (os.killpg, os.kill):
                try:
                    kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def child_pid(self):
        [path] = self.project.home.glob("fake/*/fs/home/child.pid")
        return int(path.read_text())

    def run_scenario(self, body):
        self.project.scenario("spawn.py", body)
        r = self.project.vmlab("run")
        return r, self.project.report()["scenarios"][0]

    def assertPasses(self, body):
        r, scenario = self.run_scenario(body)
        self.assertEqual(scenario["status"], "passed", scenario)
        self.assertExit(r, 0)
        return scenario


class SpawnTest(SpawnCase):
    def test_spawn_gives_a_handle_with_output_log_running_and_stop(self):
        scenario = self.assertPasses("""
def scenario(g):
    argv = ["sh", "-c", 'echo out; echo err >&2; echo "$GREETING"; exec sleep 300']
    p = g.spawn(argv, env={"GREETING": "hi there"})
    g.check("argv", p.argv == argv, detail=p.argv)
    g.check("pid", isinstance(p.pid, int) and p.pid > 0, detail=p.pid)
    logged = g.wait_for(log=p.log, pattern="hi there", timeout=10)
    g.check("its log is a Guest path wait_for reads", logged["met"], detail=logged)
    g.check("output is stdout and stderr together", p.output() == "out\\nerr\\nhi there\\n", detail=repr(p.output()))
    g.check("the log is a Guest file", g.get(p.log) == p.output())
    g.check("running", p.running())
    p.stop()
    g.check("not running after stop", not p.running())
    p.stop()
    g.check("stop again is harmless", not p.running())
    g.check("output stays readable", p.output().startswith("out"))
""")
        [spawned] = scenario["spawned"]
        self.assertEqual(spawned["argv"][:2], ["sh", "-c"])
        self.assertEqual(spawned["ended"], "scenario")
        self.assertNotIn("output_tail", spawned)  # a passing Run carries no output
        self.assertTrue(gone(spawned["pid"]))

    def test_stop_ends_the_processes_children(self):
        self.assertPasses("""
def scenario(g):
    p = g.spawn(%s)
    g.check("child started", g.wait_for(file="~/child.pid", timeout=10)["met"])
    child = g.get("~/child.pid").strip()
    g.check("child runs", g.exec(["kill", "-0", child]).ok)
    p.stop()
    ended = g.wait_for(exec=["kill", "-0", child], gone=True, timeout=5)
    g.check("child ended with it", ended["met"], detail=ended)
""" % PARENT_AND_CHILD)

    def test_stop_ends_the_children_of_a_process_that_exited(self):
        scenario = self.assertPasses("""
def scenario(g):
    p = g.spawn(%s)
    g.check("child started", g.wait_for(file="~/child.pid", timeout=10)["met"])
    child = g.get("~/child.pid").strip()
    exited = g.wait_for(exec=["kill", "-0", str(p.pid)], gone=True, timeout=10)
    g.check("the process exited", exited["met"] and not p.running(), detail=exited)
    g.check("its child runs", g.exec(["kill", "-0", child]).ok)
    p.stop()
    ended = g.wait_for(exec=["kill", "-0", child], gone=True, timeout=5)
    g.check("its child ended", ended["met"], detail=ended)
""" % ORPHAN)
        [spawned] = scenario["spawned"]
        self.assertEqual(spawned["ended"], "scenario", "something was left to stop, so it was not 'exited'")

    def test_a_pid_now_another_process_s_is_not_signalled(self):
        # A pid is reused only once its process and group are gone; the handle stands in for one
        # whose pid a later process took by claiming a different start time.
        scenario = self.assertPasses("""
def scenario(g):
    p = g.spawn(["sleep", "300"])
    p._process = p._process._replace(started="Mon Jan  1 00:00:00 2001")
    g.check("not running: the pid is another process's", not p.running())
    p.stop()
    g.check("stop leaves that process alone", g.exec(["kill", "-0", str(p.pid)]).ok)
""")
        [spawned] = scenario["spawned"]
        self.assertEqual(spawned["ended"], "exited", spawned)
        self.assertFalse(gone(spawned["pid"], within=0.5), "vmlab signalled a process that was not its")

    def test_a_missing_command_raises_naming_it(self):
        r, scenario = self.run_scenario("""
def scenario(g):
    g.spawn(["no-such-command-vmlab-spawn", "x"])
    g.check("unreachable", True)
""")
        self.assertExit(r, 1)
        self.assertEqual(scenario["status"], "error")
        self.assertIn("spawn.py:3", scenario["error"])
        self.assertIn("no-such-command-vmlab-spawn", scenario["error"])
        self.assertEqual(scenario["spawned"], [])

    def test_argv_must_be_a_list_of_strings(self):
        r, scenario = self.run_scenario("""
def scenario(g):
    g.spawn("sleep 30")
    g.check("unreachable", True)
""")
        self.assertEqual(scenario["status"], "error")
        self.assertIn("argv", scenario["error"])

    def test_spawn_is_on_the_scenario_clock(self):
        r, scenario = self.run_scenario("""
import time
TIMEOUT = 1
def scenario(g):
    time.sleep(1.2)
    g.spawn(["sleep", "30"])
    g.check("unreachable", True)
""")
        self.assertEqual(scenario["status"], "error")
        self.assertIn("1s timeout (before spawn", scenario["error"])


class SpawnCleanupTest(SpawnCase):
    """What a Scenario spawns ends with its Run, however the Run ends."""

    def run_leaving_it_running(self, ending, timeout=30):
        return self.run_scenario("""
TIMEOUT = %s
def scenario(g):
    p = g.spawn(%s)
    g.check("child started", g.wait_for(file="~/child.pid", timeout=10)["met"])
    g.wait_for(log=p.log, pattern="oops", timeout=10)
    %s
""" % (timeout, PARENT_AND_CHILD, ending))

    def assertCleanedUp(self, scenario, tail):
        [spawned] = scenario["spawned"]
        self.assertEqual(spawned["ended"], "run", spawned)
        self.assertTrue(gone(spawned["pid"]), "the spawned process still runs")
        self.assertTrue(gone(self.child_pid()), "its child still runs")
        if tail:
            self.assertIn("sleep 300", " ".join(spawned["argv"]))
            self.assertEqual(spawned["output_tail"], "started\noops\n")
            summary = (self.project.only_run_dir() / "summary.md").read_text()
            self.assertIn("sleep 300 &", summary)
            self.assertIn("  started\n  oops\n", summary)
        else:
            self.assertNotIn("output_tail", spawned)

    def test_a_passing_scenario(self):
        r, scenario = self.run_leaving_it_running("pass")
        self.assertEqual(scenario["status"], "passed", scenario)
        self.assertCleanedUp(scenario, tail=False)

    def test_a_failing_scenario(self):
        r, scenario = self.run_leaving_it_running('g.check("fails", False)')
        self.assertEqual(scenario["status"], "failed", scenario)
        self.assertCleanedUp(scenario, tail=True)

    def test_an_erroring_scenario(self):
        r, scenario = self.run_leaving_it_running('raise RuntimeError("boom")')
        self.assertEqual(scenario["status"], "error", scenario)
        self.assertIn("boom", scenario["error"])
        self.assertCleanedUp(scenario, tail=True)

    def test_a_timed_out_scenario(self):
        r, scenario = self.run_leaving_it_running('g.exec(["sleep", "30"])', timeout=3)
        self.assertEqual(scenario["status"], "error", scenario)
        self.assertIn("3s timeout", scenario["error"])
        self.assertCleanedUp(scenario, tail=True)

    def test_a_process_that_exited_by_itself(self):
        r, scenario = self.run_scenario("""
def scenario(g):
    p = g.spawn(["sh", "-c", "echo bye"])
    g.wait_for(exec=["kill", "-0", str(p.pid)], gone=True, timeout=10)
    g.check("fails", False)
""")
        [spawned] = scenario["spawned"]
        self.assertEqual(spawned["ended"], "exited", spawned)
        self.assertEqual(spawned["output_tail"], "bye\n")

    def test_ctrl_c(self):
        # The Run ends as Ctrl-C always ended it, with no report; what it spawned ends first.
        self.project.scenario("spawn.py", """
import time
def scenario(g):
    g.spawn(%s)
    g.wait_for(file="~/child.pid", timeout=10)
    g.put("~/ready", "")
    time.sleep(30)
    g.check("unreachable", True)
""" % PARENT_AND_CHILD)
        proc = self.project.vmlab_background("run")
        self.addCleanup(proc.kill)
        deadline = time.time() + 30
        while not list(self.project.home.glob("fake/*/fs/home/ready")):
            self.assertLess(time.time(), deadline, "the Scenario never got going")
            time.sleep(0.05)
        os.kill(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("KeyboardInterrupt", err)
        self.assertTrue(gone(self.child_pid()), "what the Scenario spawned still runs")

    def test_sys_exit(self):
        self.project.scenario("spawn.py", """
def scenario(g):
    g.spawn(%s)
    g.wait_for(file="~/child.pid", timeout=10)
    raise SystemExit(3)
""" % PARENT_AND_CHILD)
        self.assertExit(self.project.vmlab("run"), 3)
        self.assertTrue(gone(self.child_pid()), "what the Scenario spawned still runs")

    def test_a_process_vmlab_cannot_stop_is_warned_about_when_the_run_ends_with_no_report(self):
        # A path outside the Guest is a ConfigError under the Fake Provider: the Run ends with no report.
        self.project.scenario("spawn.py", """
def scenario(g):
    g.spawn(["sleep", "300"])
    g.exec(["sh", "-c", 'rm "$VMLAB_HOME"/fake/*/running'])
    g.put("/../../outside", "x")
""")
        r = self.project.vmlab("run")
        pids = [int(pid) for pid in re.findall(r"\(pid (\d+)\)", r.out)]
        self.addCleanup(lambda: [os.kill(pid, signal.SIGKILL) for pid in pids if not gone(pid, within=0)])
        self.assertExit(r, 2)
        self.assertIn("leaves the Guest", r.err)
        self.assertIn("spawned `sleep 300`", r.out)
        self.assertIn("still running", r.out)

    def test_a_process_vmlab_cannot_stop_is_noted_and_the_result_stands(self):
        # The Scenario takes the Fake Guest down under vmlab (as a Guest that stopped answering), so
        # neither stopping the process nor reading its output can reach it.
        r, scenario = self.run_scenario("""
def scenario(g):
    g.spawn(["sleep", "300"])
    g.spawn(["sleep", "301"])
    g.check("fails", False)
    g.exec(["sh", "-c", 'rm "$VMLAB_HOME"/fake/*/running'])
""")
        self.assertEqual(scenario["status"], "failed", scenario)
        self.assertIsNone(scenario["error"])
        [first, second] = scenario["spawned"]
        self.assertEqual(first["ended"], "failed", first)
        self.assertIn("not running", first["stop_error"])
        self.assertIsNone(first["output_tail"])
        self.assertIn("not running", first["output_error"])
        # Once the Guest does not answer, vmlab does not wait for it again for every other process.
        self.assertEqual(second["ended"], "failed", second)
        self.assertTrue(second["stop_error"].startswith("not tried: "), second)
        self.assertIn("not running", second["stop_error"])
        self.assertIn("still running", r.out)
        self.assertIn("still running", (self.project.only_run_dir() / "summary.md").read_text())
