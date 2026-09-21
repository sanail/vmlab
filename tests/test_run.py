import re
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
