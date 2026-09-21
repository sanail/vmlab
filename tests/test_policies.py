"""Runner lifecycle policies: restore, app state reset, who stops Guests, timeouts, visual Checks."""

import json
import xml.etree.ElementTree as ET

from harness import FAKE_LAB, VmlabTestCase

WRITE_MARKER = """
def scenario(g):
    g.exec(["sh", "-c", "touch marker"])
    g.check("marker written", g.exec(["test", "-e", "marker"]).ok)
"""

SEES_MARKER = """
def scenario(g):
    g.check("marker %s", g.exec(["test", "-e", "marker"]).ok == %s)
"""

PASS = 'def scenario(g):\n    g.check("ok", True)\n'


class RestoreTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.project.config(FAKE_LAB)

    def test_clean_state_is_restored_once_at_the_start_of_the_suite(self):
        self.project.scenario("a_write.py", WRITE_MARKER)
        self.project.scenario("b_sees.py", SEES_MARKER % ("survives within the suite", True))
        self.assertExit(self.project.vmlab("run"), 0)

        self.project.scenario("c_next_suite.py", SEES_MARKER % ("gone at the next suite", False))
        self.assertExit(self.project.vmlab("run", "c_next_suite"), 0)

    def test_a_fresh_scenario_gets_its_own_restore(self):
        self.project.scenario("a_write.py", WRITE_MARKER)
        self.project.scenario("b_fresh.py", "FRESH = True\n" + SEES_MARKER % ("gone for a fresh Scenario", False))
        self.assertExit(self.project.vmlab("run"), 0)

    def test_fresh_flag_restores_before_every_scenario(self):
        self.project.scenario("a_write.py", WRITE_MARKER)
        self.project.scenario("b_sees.py", SEES_MARKER % ("gone with --fresh", False))
        self.assertExit(self.project.vmlab("run", "--fresh"), 0)

    def test_an_ad_hoc_run_keeps_guest_state(self):
        adhoc = self.project.root / "adhoc"
        adhoc.mkdir()
        (adhoc / "write.py").write_text(WRITE_MARKER)
        (adhoc / "sees.py").write_text(SEES_MARKER % ("kept between Ad-hoc runs", True))

        self.assertExit(self.project.vmlab("run", adhoc / "write.py"), 0)
        self.assertExit(self.project.vmlab("run", adhoc / "sees.py"), 0)
        self.assertExit(self.project.vmlab("run", adhoc / "sees.py", "--fresh"), 1)


class AppStateResetTest(VmlabTestCase):
    def test_app_state_paths_are_removed_before_each_run_and_nothing_else(self):
        self.project.config(FAKE_LAB + """
            [labs.mac.app]
            state = ["~/.config/myapp", "~/Library/Caches/myapp.db"]
        """)
        self.project.scenario("a_dirty.py", """
            def scenario(g):
                g.exec(["sh", "-c", "mkdir -p ~/.config/myapp ~/Library/Caches && touch ~/.config/myapp/settings ~/Library/Caches/myapp.db ~/unrelated"])
                g.check("state written", g.exec(["sh", "-c", "test -e ~/.config/myapp/settings"]).ok)
        """)
        self.project.scenario("b_clean.py", """
            def scenario(g):
                g.check("settings dir removed", not g.exec(["sh", "-c", "test -e ~/.config/myapp"]).ok)
                g.check("cache file removed", not g.exec(["sh", "-c", "test -e ~/Library/Caches/myapp.db"]).ok)
                g.check("other files kept", g.exec(["sh", "-c", "test -e ~/unrelated"]).ok)
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)

    def test_state_must_be_a_list_of_paths(self):
        self.project.config(FAKE_LAB + '[labs.mac.app]\nstate = "~/.config/myapp"\n')
        self.project.scenario("ok.py", PASS)
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn("labs.mac.app.state", r.err)


class WhoStopsGuestsTest(VmlabTestCase):
    def status(self):
        return {g["lab"]: g["running"] for g in json.loads(self.project.vmlab("status", "--json").out)}

    def setUp(self):
        super().setUp()
        self.project.config(FAKE_LAB)
        self.project.scenario("ok.py", PASS)

    def test_keep_leaves_a_guest_vmlab_started_running_and_says_how_to_stop_it(self):
        r = self.project.vmlab("run", "--keep")
        self.assertExit(r, 0)
        self.assertEqual(self.status(), {"mac": True})
        self.assertIn("down mac", r.out)

    def test_an_ad_hoc_run_keeps_its_guest_and_the_next_suite_stops_it(self):
        adhoc = self.project.root / "check.py"
        adhoc.write_text(PASS)

        r = self.project.vmlab("run", adhoc)
        self.assertExit(r, 0)
        self.assertEqual(self.status(), {"mac": True})
        self.assertIn("down mac", r.out)

        self.assertExit(self.project.vmlab("run"), 0)
        self.assertEqual(self.status(), {"mac": False})

    def test_a_guest_the_user_started_survives_keep_and_ad_hoc_bookkeeping(self):
        self.assertExit(self.project.vmlab("run", "--keep"), 0)  # vmlab started it...
        self.assertExit(self.project.vmlab("up", "mac"), 0)  # ...then the user claims it

        self.assertExit(self.project.vmlab("run"), 0)
        self.assertEqual(self.status(), {"mac": True})


class ScenarioTimeoutTest(VmlabTestCase):
    def test_lab_step_timeout_applies_to_every_call(self):
        self.project.config(FAKE_LAB.replace('arch = "arm64"', 'arch = "arm64"\nstep_timeout = 1'))
        self.project.scenario("slow.py", """
            def scenario(g):
                g.exec(["sleep", "5"])
                g.check("unreachable", True)
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 1)
        self.assertIn("timed out after 1s", self.project.report()["scenarios"][0]["error"])

    def test_scenario_timeout_bounds_the_whole_scenario(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("a_slow.py", """
            TIMEOUT = 2

            def scenario(g):
                for _ in range(5):
                    g.exec(["sleep", "1"])
                g.check("unreachable", True)
        """)
        self.project.scenario("b_next.py", PASS)

        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        slow, after = self.project.report()["scenarios"]
        self.assertIn("a_slow.py:6", slow["error"])
        self.assertIn("exceeded its 2s timeout", slow["error"])
        self.assertLess(slow["duration_s"], 4)
        self.assertEqual(after["status"], "passed")

    def test_lab_scenario_timeout_is_the_default(self):
        self.project.config(FAKE_LAB.replace('arch = "arm64"', 'arch = "arm64"\nscenario_timeout = 1'))
        self.project.scenario("slow.py", """
            def scenario(g):
                g.exec(["sleep", "3"])
                g.check("unreachable", True)
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertIn("exceeded its 1s timeout", self.project.report()["scenarios"][0]["error"])

    def test_bad_timeout_declaration_is_a_scenario_error(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("bad.py", 'TIMEOUT = "soon"\n' + PASS)
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertIn("TIMEOUT", self.project.report()["scenarios"][0]["error"])


class VisualCheckTest(VmlabTestCase):
    def test_visual_checks_are_marked_unverified_in_every_report(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("look.py", """
            def scenario(g):
                g.check("window listed", True)
                g.check("icon looks crisp", True, visual=True)
        """)

        r = self.project.vmlab("run")

        self.assertExit(r, 0)
        run_dir = self.project.only_run_dir()
        [scenario] = self.project.report(run_dir)["scenarios"]
        kinds = {c["name"]: c["kind"] for c in scenario["checks"]}
        self.assertEqual(kinds, {"window listed": "deterministic", "icon looks crisp": "visual, unverified"})

        cases = {c.get("name"): c for c in ET.parse(str(run_dir / "junit.xml")).getroot().iter("testcase")}
        props = {p.get("name"): p.get("value") for p in cases["icon looks crisp"].iter("property")}
        self.assertEqual(props, {"kind": "visual, unverified"})
        self.assertEqual(list(cases["window listed"].iter("property")), [])

        summary = (run_dir / "summary.md").read_text()
        self.assertIn("icon looks crisp (visual, unverified)", summary)
        self.assertNotIn("window listed (visual", summary)
        self.assertIn("1 visual", r.out)
