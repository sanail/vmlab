import json

from harness import FAKE_LAB, VmlabTestCase

SMOKE = """
def scenario(g):
    g.check("ok", True)
"""


class ConfigErrorTest(VmlabTestCase):
    def assertConfigError(self, key, *fragments):
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn(str(self.project.config_path), r.err)
        if key:
            self.assertIn(key, r.err)
        self.assertIn("fix:", r.err)
        for fragment in fragments:
            self.assertIn(fragment, r.err)
        self.assertEqual(self.project.run_dirs(), [])
        return r

    def test_missing_config_points_at_where_it_is_expected(self):
        self.assertConfigError(None, "no vmlab config found")

    def test_invalid_toml_reports_the_line(self):
        self.project.config("[labs.mac\nprovider = 'fake'\n")
        self.assertConfigError(None, "invalid TOML", "line 1")

    def test_no_labs(self):
        self.project.config("# nothing yet\n")
        self.assertConfigError("labs", "no Labs declared", "[labs.mac]")

    def test_missing_provider(self):
        self.project.config('[labs.mac]\nos = "macos"\n')
        self.assertConfigError("labs.mac.provider", "missing", "fake")

    def test_unknown_provider_lists_the_known_ones(self):
        self.project.config('[labs.mac]\nprovider = "tartt"\nos = "macos"\n')
        self.assertConfigError("labs.mac.provider", "'tartt'", "use one of: fake")

    def test_unknown_os(self):
        self.project.config('[labs.mac]\nprovider = "fake"\nos = "osx"\n')
        self.assertConfigError("labs.mac.os", "'osx'", "macos, windows, linux")

    def test_unknown_arch(self):
        self.project.config('[labs.mac]\nprovider = "fake"\nos = "macos"\narch = "amd64"\n')
        self.assertConfigError("labs.mac.arch", "'amd64'", "arm64, x86_64")

    def test_misspelled_key_is_rejected_not_ignored(self):
        self.project.config('[labs.mac]\nprovider = "fake"\nos = "macos"\nproviderr = "x"\n')
        self.assertConfigError("labs.mac.providerr", "unknown key")

    def test_unknown_lab_on_the_command_line(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("smoke.py", SMOKE)
        r = self.project.vmlab("run", "--lab", "win")
        self.assertExit(r, 2)
        self.assertIn("labs.win", r.err)
        self.assertIn("use one of: mac", r.err)

    def test_no_scenarios(self):
        self.project.config(FAKE_LAB)
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn(str(self.project.dir / "scenarios"), r.err)
        self.assertIn("scenario(g)", r.err)

    def test_config_is_found_from_a_subdirectory(self):
        self.project.config(FAKE_LAB)
        self.project.scenario("smoke.py", SMOKE)
        sub = self.project.root / "app" / "src" / "deep"
        sub.mkdir(parents=True)
        self.assertExit(self.project.vmlab("run", cwd=sub), 0)

    def test_unreadable_scripted_ui_tree_is_a_config_error(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nui_tree = "missing.json"\n')
        self.project.scenario("tree.py", 'def scenario(g):\n    g.check("tree", g.tree() is not None)\n')
        self.assertConfigError("labs.mac.fake.ui_tree", "missing.json")

    def test_notification_id_is_a_non_empty_string(self):
        self.project.config(FAKE_LAB + "[labs.mac.app]\nnotification_id = 3\n")
        self.assertConfigError("labs.mac.app.notification_id", "non-empty string")

    def test_an_unknown_app_key_lists_notification_id_among_the_allowed(self):
        self.project.config(FAKE_LAB + '[labs.mac.app]\nnotification = "com.example.myapp"\n')
        self.assertConfigError("labs.mac.app.notification", "unknown key", "notification_id")

    def test_unreadable_scripted_notifications_are_a_config_error(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nnotifications = "missing.json"\n')
        self.project.scenario("smoke.py", SMOKE)
        self.assertConfigError("labs.mac.fake.notifications", "missing.json")

    def test_scripted_notifications_are_a_list_of_them(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nnotifications = "notifications.json"\n')
        (self.project.dir / "notifications.json").write_text('{"app": "x"}')
        self.project.scenario("smoke.py", SMOKE)
        self.assertConfigError("labs.mac.fake.notifications", "list")


LANGUAGE_SCENARIO = """
def scenario(g):
    g.check("language " + g.language, True)
"""


class LabLanguageTest(VmlabTestCase):
    """[labs.NAME] language: the Lab language, ll-RR, en-US unless the Lab says otherwise."""

    def run_language(self, toml):
        self.project.config(toml)
        self.project.scenario("language.py", LANGUAGE_SCENARIO)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)
        [check] = self.project.report()["scenarios"][0]["checks"]
        return check["name"]

    def test_a_lab_is_in_english_unless_it_says_otherwise(self):
        self.assertEqual(self.run_language(FAKE_LAB), "language en-US")

    def test_scenarios_read_the_labs_language(self):
        self.assertEqual(self.run_language(FAKE_LAB + 'language = "ru-RU"\n'), "language ru-RU")

    def test_status_shows_the_language(self):
        self.project.config(FAKE_LAB + 'language = "de-DE"\n')
        [row] = json.loads(self.project.vmlab("status", "--json").out)
        self.assertEqual(row["language"], "de-DE")

    def test_a_language_without_a_region_is_rejected_with_an_example(self):
        self.project.config(FAKE_LAB + 'language = "ru"\n')
        r = self.project.vmlab("status")
        self.assertExit(r, 2)
        self.assertIn("labs.mac.language", r.err)
        self.assertIn('language = "ru-RU"', r.err)

    def test_a_language_must_be_a_string(self):
        self.project.config(FAKE_LAB + "language = 7\n")
        r = self.project.vmlab("status")
        self.assertExit(r, 2)
        self.assertIn("labs.mac.language", r.err)
