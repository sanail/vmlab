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
