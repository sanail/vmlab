import re
import textwrap
import xml.etree.ElementTree as ET

from harness import FAKE_LAB, VmlabTestCase

PASSING = """
def scenario(g):
    r = g.exec(["echo", "hello"])
    g.check("echo prints hello", r.stdout.strip() == "hello")
"""

FAILING = """
def scenario(g):
    g.check("always true", True)
    g.check("app window appears", False, detail="no window titled Palette")
"""


class RunTest(VmlabTestCase):
    def test_passing_scenario_exits_zero_and_writes_all_reports(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("smoke.py", PASSING)

        r = self.project.vmlab("run")

        self.assertExit(r, 0)
        run_dir = self.project.only_run_dir()
        self.assertRegex(run_dir.name, r"^\d{8}T\d{6}Z-mac$")

        report = self.project.report(run_dir)
        self.assertEqual(report["lab"], "mac")
        self.assertEqual(report["status"], "passed")
        [scenario] = report["scenarios"]
        self.assertEqual(scenario["name"], "smoke")
        self.assertEqual(scenario["status"], "passed")
        self.assertEqual(
            [(c["name"], c["passed"]) for c in scenario["checks"]], [("echo prints hello", True)]
        )

        suite = ET.parse(str(run_dir / "junit.xml")).getroot()
        self.assertEqual(suite.get("failures"), "0")
        self.assertIn("passed", (run_dir / "summary.md").read_text().lower())

    def test_failing_check_exits_nonzero_and_is_named_in_every_report(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("palette.py", FAILING)

        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        self.assertIn("mac/palette", r.out)
        self.assertIn("app window appears", r.out)

        run_dir = self.project.only_run_dir()
        report = self.project.report(run_dir)
        self.assertEqual(report["status"], "failed")
        [scenario] = report["scenarios"]
        self.assertEqual(scenario["status"], "failed")
        failed = [c for c in scenario["checks"] if not c["passed"]]
        self.assertEqual([c["name"] for c in failed], ["app window appears"])
        self.assertEqual(failed[0]["detail"], "no window titled Palette")

        suite = ET.parse(str(run_dir / "junit.xml")).getroot()
        self.assertEqual(suite.get("tests"), "2")
        self.assertEqual(suite.get("failures"), "1")
        [failure] = suite.iter("failure")
        self.assertEqual(failure.get("message"), "no window titled Palette")

        summary = (run_dir / "summary.md").read_text()
        self.assertIn("FAILED", summary)
        self.assertIn("app window appears", summary)

    def test_scenario_that_raises_is_an_error_not_a_pass(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("broken.py", """
            def scenario(g):
                g.check("reached", True)
                raise RuntimeError("helper crashed")
        """)

        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        self.assertIn("helper crashed", r.out)
        report = self.project.report()
        self.assertEqual(report["status"], "error")
        self.assertIn("helper crashed", report["scenarios"][0]["error"])
        suite = ET.parse(str(self.project.only_run_dir() / "junit.xml")).getroot()
        self.assertEqual(suite.get("errors"), "1")

    def test_scenario_without_checks_is_an_error(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("empty.py", """
            def scenario(g):
                g.exec(["true"])
        """)

        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        self.assertEqual(self.project.report()["status"], "error")
        self.assertIn("no Checks", r.out)

    def test_each_run_gets_its_own_folder_per_lab(self):
        self.project.config(FAKE_LAB + """
            [labs.ubuntu]
            provider = "fake"
            os = "linux"
        """)
        self.project.scenario("smoke.py", PASSING)

        self.assertExit(self.project.vmlab("run"), 0)
        self.assertExit(self.project.vmlab("run", "--lab", "ubuntu"), 0)

        labs = [self.project.report(d)["lab"] for d in self.project.run_dirs()]
        self.assertEqual(sorted(labs), ["mac", "ubuntu", "ubuntu"])
        self.assertEqual(len(set(self.project.run_dirs())), 3)

    def test_one_scenario_can_be_selected_by_name(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("smoke.py", PASSING)
        self.project.scenario("palette.py", FAILING)

        r = self.project.vmlab("run", "smoke")

        self.assertExit(r, 0)
        self.assertEqual([s["name"] for s in self.project.report()["scenarios"]], ["smoke"])

    def test_screenshots_are_saved_in_the_run_folder_as_evidence(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("shot.py", """
            def scenario(g):
                path = g.screenshot("after launch")
                g.check("screenshot taken", path.endswith(".png"))
        """)

        self.assertExit(self.project.vmlab("run"), 0)

        run_dir = self.project.only_run_dir()
        [shot] = self.project.report(run_dir)["scenarios"][0]["screenshots"]
        self.assertTrue((run_dir / shot).read_bytes().startswith(b"\x89PNG"))
        self.assertIn(shot, (run_dir / "summary.md").read_text())

    def test_each_scenario_keeps_its_screenshots_in_its_own_folder(self):
        self.project.config(FAKE_LAB)
        for name in ("palette.py", "tray.py"):
            self.project.scenario(name, """
                def scenario(g):
                    g.check("returns its path", g.screenshot("welcome") == "screenshots/%s/01-welcome.png")
                    g.check("window appears", False)
            """ % name[:-3])

        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        run_dir = self.project.only_run_dir()
        shots = {s["name"]: s["screenshots"] for s in self.project.report(run_dir)["scenarios"]}
        self.assertEqual(
            shots, {"palette": ["screenshots/palette/01-welcome.png"], "tray": ["screenshots/tray/01-welcome.png"]}
        )
        summary = (run_dir / "summary.md").read_text()
        junit = (run_dir / "junit.xml").read_text()
        for name, [shot] in shots.items():
            self.assertTrue((run_dir / shot).is_file())
            self.assertIn("- screenshot: [%s](%s)" % (shot, shot), summary)
            self.assertIn(shot, junit)
            self.assertIn("FAIL mac/%s: window appears (screenshots: %s)" % (name, shot), r.out)
        self.assertNotIn("returns its path", r.out)

    def test_scenarios_with_one_name_in_a_suite_run_keep_their_screenshots_apart(self):
        self.project.config(FAKE_LAB)
        files = [self.project.dir / "ad-hoc" / f for f in ("a/x.py", "b/x.py", "c/x-2.py")]
        for path in files:
            path.parent.mkdir(parents=True)
            path.write_text("def scenario(g):\n    g.check('taken', g.screenshot('welcome'))\n")

        r = self.project.vmlab("run", *files)

        self.assertExit(r, 0)
        run_dir = self.project.only_run_dir()
        shots = [s["screenshots"] for s in self.project.report(run_dir)["scenarios"]]
        self.assertEqual(
            shots,
            [["screenshots/x/01-welcome.png"], ["screenshots/x-3/01-welcome.png"], ["screenshots/x-2/01-welcome.png"]],
        )
        for [shot] in shots:
            self.assertTrue((run_dir / shot).is_file())

    def test_scenario_sees_the_guest_os_and_the_scripted_ui_tree(self):
        self.project.config(FAKE_LAB + """
            [labs.mac.fake]
            ui_tree = "tree.json"
        """)
        (self.project.dir / "tree.json").write_text(
            '{"role": "desktop", "children": [{"role": "window", "name": "Palette"}]}'
        )
        self.project.scenario("tree.py", """
            def scenario(g):
                g.check("os is macos", g.os == "macos")
                names = [c["name"] for c in g.tree()["children"]]
                g.check("palette window listed", names == ["Palette"])
        """)

        self.assertExit(self.project.vmlab("run"), 0)

    def test_scenarios_import_helpers_from_their_own_folder_without_shared_state(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("_helpers.py", """
            seen = []

            def greet(g):
                seen.append(g.os)
                return g.exec(["echo", "hello"]).stdout.strip()
        """)
        body = """
            import sys
            from pathlib import Path

            import _helpers

            def scenario(g):
                g.check("helper greets", _helpers.greet(g) == "hello")
                g.check("helper state not carried over", _helpers.seen == ["macos"])
                g.check("folder on sys.path once", sys.path.count(str(Path(__file__).parent)) == 1)
        """
        self.project.scenario("first.py", body)
        self.project.scenario("second.py", body)

        r = self.project.vmlab("run")

        self.assertExit(r, 0)
        self.assertEqual([s["status"] for s in self.project.report()["scenarios"]], ["passed", "passed"])

    def test_ad_hoc_scenario_imports_its_own_helpers_and_leaves_sys_path_as_it_was(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("_helpers.py", "WHERE = 'saved'\n")
        self.project.scenario("saved.py", """
            import sys

            import _helpers

            def scenario(g):
                g.check("saved helper imported", _helpers.WHERE == "saved")
                g.check("ad-hoc folder gone from sys.path", not any(p.endswith("ad-hoc") for p in sys.path))
        """)
        ad_hoc = self.project.dir / "runs" / "ad-hoc"
        ad_hoc.mkdir(parents=True)
        (ad_hoc / "_helpers.py").write_text("WHERE = 'ad-hoc'\n")
        (ad_hoc / "try.py").write_text(textwrap.dedent("""
            import _helpers

            def scenario(g):
                g.check("ad-hoc helper imported", _helpers.WHERE == "ad-hoc")
        """))

        r = self.project.vmlab("run", "saved", str(ad_hoc / "try.py"), "saved")

        self.assertExit(r, 0)
