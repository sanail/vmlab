"""A Scenario declaring OS and LANGUAGES is left out on the Labs that do not match them."""

import json
import xml.etree.ElementTree as ET

from harness import VmlabTestCase


def lab(name, os, extra=""):
    return '[labs.%s]\nprovider = "fake"\nos = "%s"\n%s\n' % (name, os, extra)


EN = lab("en", "macos")
RU = lab("ru", "macos", 'language = "ru-RU"\n[labs.ru.app]\nartifact = "app.bin"\nbuild = "touch built && touch app.bin"')
WIN = lab("win", "windows")
PLAIN = 'def scenario(g):\n    g.check("ran on " + g.lab, True)\n'


def limited(declarations):
    return declarations + "\n" + PLAIN


class ScenarioLimitsTest(VmlabTestCase):
    def test_a_scenario_is_left_out_on_a_lab_whose_language_it_does_not_declare(self):
        self.project.config(EN + lab("ru", "macos", 'language = "ru-RU"'))
        self.project.scenario("languages.py", limited('LANGUAGES = ["en-US"]'))
        self.project.scenario("smoke.py", PLAIN)

        r = self.project.vmlab("run")

        self.assertExit(r, 0)
        self.assertIn('ru: languages left out (LANGUAGES = ["en-US"])', r.out)
        self.assertNotIn("en: languages left out", r.out)
        reports = {self.project.report(d)["lab"]: (d, self.project.report(d)) for d in self.project.run_dirs()}
        self.assertEqual([s["name"] for s in reports["en"][1]["scenarios"]], ["languages", "smoke"])
        self.assertEqual(reports["en"][1]["excluded"], [])
        ru_dir, ru = reports["ru"]
        self.assertEqual(ru["status"], "passed")
        self.assertEqual([s["name"] for s in ru["scenarios"]], ["smoke"])
        self.assertEqual(ru["excluded"], [{"name": "languages", "reason": 'LANGUAGES = ["en-US"]'}])
        self.assertIn('languages: left out (LANGUAGES = ["en-US"])', (ru_dir / "summary.md").read_text())

    def test_os_leaves_a_scenario_out_and_os_with_languages_needs_both(self):
        self.project.config(EN + lab("ru", "macos", 'language = "ru-RU"') + WIN)
        self.project.scenario("unix.py", limited('OS = ["macos", "linux"]'))
        self.project.scenario("en_mac.py", limited('OS = ("macos",)\nLANGUAGES = ["en-US"]'))
        self.project.scenario("smoke.py", PLAIN)

        r = self.project.vmlab("run")

        self.assertExit(r, 0)
        ran = {self.project.report(d)["lab"]: [s["name"] for s in self.project.report(d)["scenarios"]] for d in self.project.run_dirs()}
        self.assertEqual(ran, {"en": ["en_mac", "smoke", "unix"], "ru": ["smoke", "unix"], "win": ["smoke"]})
        self.assertIn('win: unix left out (OS = ["macos", "linux"])', r.out)
        self.assertIn('win: en_mac left out (OS = ["macos"])', r.out)
        self.assertIn('ru: en_mac left out (LANGUAGES = ["en-US"])', r.out)

    def test_a_lab_with_every_scenario_left_out_is_skipped_unbuilt_and_unbooted(self):
        self.project.config(EN + RU)
        self.project.scenario("languages.py", limited('LANGUAGES = ["en-US"]'))

        r = self.project.vmlab("run", "--repeat", "2")

        self.assertExit(r, 0)
        self.assertIn('ru: languages left out (LANGUAGES = ["en-US"])', r.out)
        self.assertEqual(r.out.count("ru: languages left out"), 1, r.out)
        self.assertIn("ru: skipped: every Scenario chosen is left out on this Lab", r.out)
        self.assertIn("SKIPPED ru", r.out)
        self.assertIn("en: 2 of 2 repetition(s) passed", r.out)
        self.assertNotIn("ru: 0 of", r.out)
        self.assertFalse((self.project.root / "app" / "built").exists(), "nothing built for a Lab with nothing to run")
        status = {s["lab"]: s["running"] for s in json.loads(self.project.vmlab("status", "--json").out)}
        self.assertFalse(status["ru"])
        ru_dirs = [d for d in self.project.run_dirs() if d.name.endswith("-ru")]
        self.assertTrue(ru_dirs)
        report = self.project.report(ru_dirs[0])
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["scenarios"], [])
        self.assertIn("every Scenario", report["skipped"])
        suite = ET.parse(str(ru_dirs[0] / "junit.xml")).getroot()
        self.assertEqual(suite.get("skipped"), "1")
        self.assertIn("every Scenario", suite.find("testcase/skipped").get("message"))
        self.assertIn("Skipped: every Scenario", (ru_dirs[0] / "summary.md").read_text())

    def test_a_scenario_chosen_twice_is_left_out_once(self):
        self.project.config(EN + lab("ru", "macos", 'language = "ru-RU"'))
        self.project.scenario("languages.py", limited('LANGUAGES: list = ["en-US"]'))

        r = self.project.vmlab("run", "languages", "languages")

        self.assertExit(r, 0)
        self.assertEqual(r.out.count("ru: languages left out"), 1, r.out)
        [ru] = [self.project.report(d) for d in self.project.run_dirs() if d.name.endswith("-ru")]
        self.assertEqual(len(ru["excluded"]), 1)

    def test_an_explicit_run_on_an_excluded_lab_is_a_usage_error(self):
        self.project.config(EN + RU)
        scenario = self.project.root / "languages.py"
        scenario.write_text(limited('LANGUAGES = ["en-US"]'))

        r = self.project.vmlab("run", str(scenario), "--lab", "ru")

        self.assertExit(r, 2)
        self.assertIn('LANGUAGES = ["en-US"]', r.err)
        self.assertIn("ru", r.err)
        self.assertEqual(self.project.run_dirs(), [])

        r = self.project.vmlab("run", str(scenario), "--lab", "ru", "--lab", "en")

        self.assertExit(r, 0)
        self.assertIn('ru: languages left out (LANGUAGES = ["en-US"])', r.out)
        self.assertIn("PASSED en", r.out)

    def test_a_malformed_declaration_is_a_config_error_before_any_lab_boots(self):
        self.project.config(EN)
        for declaration, problem in [
            ('OS = "macos"', "OS"),
            ('OS = ["mac"]', "mac"),
            ("OS = []", "OS"),
            ('LANGUAGES = ["ru"]', "ru"),
            ("OS = some_variable", "OS"),
        ]:
            with self.subTest(declaration=declaration):
                self.project.scenario("bad.py", limited(declaration))

                r = self.project.vmlab("run")

                self.assertExit(r, 2)
                self.assertIn("bad.py", r.err)
                self.assertIn(problem, r.err)
                self.assertEqual(self.project.run_dirs(), [])
                status = {s["lab"]: s["running"] for s in json.loads(self.project.vmlab("status", "--json").out)}
                self.assertFalse(status["en"])

    def test_declarations_are_read_without_executing_the_scenario(self):
        self.project.config(RU)
        marker = self.project.root / "executed"
        self.project.scenario("languages.py", "open(%r, 'w').close()\n" % str(marker) + limited('LANGUAGES = ["en-US"]'))

        r = self.project.vmlab("run")

        self.assertExit(r, 2)
        self.assertFalse(marker.exists())

    def test_a_scenario_that_does_not_parse_runs_and_errors_as_before(self):
        self.project.config(EN)
        self.project.scenario("broken.py", 'OS = ["macos"]\ndef scenario(g:\n')

        r = self.project.vmlab("run")

        self.assertExit(r, 1)
        self.assertEqual(self.project.report()["scenarios"][0]["status"], "error")
