"""Deploy: build hook when stale, then deliver, install and launch per the Lab's recipe."""

import json
import os
import time

from harness import FAKE_LAB, VmlabTestCase

PASS = 'def scenario(g):\n    g.check("ok", True)\n'

READ_LOG = """
def scenario(g):
    log = g.exec(["sh", "-c", "cat ~/deploy.log"]).stdout.split()
    g.check("recipe order", log == %r, detail=" ".join(log))
"""

APP = """
[labs.mac.app]
artifact = "dist/MyApp.zip"
build = "mkdir -p dist && echo build-$VMLAB_LAB >> build-count && echo app-bytes > dist/MyApp.zip"
inputs = ["src"]
"""


class BuildHookTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.app = self.project.root / "app"
        (self.app / "src").mkdir()
        (self.app / "src" / "main.c").write_text("int main;")
        self.project.scenario("ok.py", PASS)

    def builds(self):
        path = self.app / "build-count"
        return path.read_text().split() if path.exists() else []

    def make_artifact(self, age):
        (self.app / "dist").mkdir(exist_ok=True)
        artifact = self.app / "dist" / "MyApp.zip"
        artifact.write_text("old-bytes")
        then = time.time() + age
        os.utime(str(artifact), (then, then))
        return artifact

    def test_a_missing_artifact_is_built(self):
        self.project.config(FAKE_LAB + APP)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)
        self.assertEqual(self.builds(), ["build-mac"])
        self.assertTrue(self.project.report()["deploy"]["built"])

    def test_the_hook_is_skipped_when_the_artifact_is_newer_than_its_inputs(self):
        self.project.config(FAKE_LAB + APP)
        self.make_artifact(age=+60)
        self.assertExit(self.project.vmlab("run"), 0)
        self.assertEqual(self.builds(), [])
        self.assertFalse(self.project.report()["deploy"]["built"])

    def test_the_hook_runs_when_an_input_is_newer_than_the_artifact(self):
        self.project.config(FAKE_LAB + APP)
        self.make_artifact(age=-60)
        self.assertExit(self.project.vmlab("run"), 0)
        self.assertEqual(self.builds(), ["build-mac"])

    def test_labs_sharing_an_artifact_build_it_once(self):
        self.project.config(FAKE_LAB + APP + """
            [labs.mac2]
            provider = "fake"
            os = "macos"
            [labs.mac2.app]
            artifact = "dist/MyApp.zip"
            build = "mkdir -p dist && echo build-$VMLAB_LAB >> build-count && echo app-bytes > dist/MyApp.zip"
            inputs = ["src"]
        """)
        self.assertExit(self.project.vmlab("run"), 0)
        self.assertEqual(self.builds(), ["build-mac"])

    def test_a_missing_artifact_after_the_build_is_a_clear_error(self):
        self.project.config(FAKE_LAB + '[labs.mac.app]\nartifact = "dist/MyApp.zip"\nbuild = "true"\n')
        r = self.project.vmlab("run")
        self.assertExit(r, 1)
        report = self.project.report()
        self.assertEqual(report["status"], "error")
        for fragment in ("dist/MyApp.zip", "not found after", "labs.mac.app.build"):
            self.assertIn(fragment, report["error"])
        self.assertIn("not found after", r.out)

    def test_a_missing_artifact_without_a_hook_says_how_to_get_one(self):
        self.project.config(FAKE_LAB + '[labs.mac.app]\nartifact = "dist/MyApp.zip"\n')
        r = self.project.vmlab("run")
        self.assertExit(r, 1)
        self.assertIn("labs.mac.app.build", self.project.report()["error"])

    def test_a_failing_build_shows_its_output(self):
        self.project.config(FAKE_LAB + '[labs.mac.app]\nartifact = "dist/MyApp.zip"\nbuild = "echo linker exploded >&2; exit 4"\n')
        r = self.project.vmlab("run")
        self.assertExit(r, 1)
        error = self.project.report()["error"]
        self.assertIn("exited 4", error)
        self.assertIn("linker exploded", error)
        self.assertIn("linker exploded", (self.project.only_run_dir() / "build.log").read_text())

    def test_the_newest_glob_match_is_the_artifact(self):
        (self.app / "dist").mkdir()
        for name, age in (("MyApp-1.0.zip", -60), ("MyApp-1.1.zip", 0)):
            path = self.app / "dist" / name
            path.write_text(name)
            os.utime(str(path), (time.time() + age,) * 2)
        self.project.config(FAKE_LAB + '[labs.mac.app]\nartifact = "dist/MyApp-*.zip"\ninstall = "cp \\"$VMLAB_ARTIFACT\\" ~/installed"\n')
        self.project.scenario("ok.py", """
            def scenario(g):
                g.check("newest delivered", g.exec(["sh", "-c", "cat ~/installed"]).stdout == "MyApp-1.1.zip")
        """)
        self.assertExit(self.project.vmlab("run"), 0)


RECIPE = """
[labs.mac.app]
artifact = "MyApp.zip"
install = "echo install-$CHANNEL_ENV >> ~/deploy.log && cp \\"$VMLAB_ARTIFACT\\" ~/installed"
quit = "echo quit >> ~/deploy.log"
launch = "echo launch-$CHANNEL_ENV >> ~/deploy.log"
state = ["~/.myapp"]
env = { CHANNEL_ENV = "beta" }
"""


class RecipeTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        (self.project.root / "app" / "MyApp.zip").write_text("app-bytes")

    def test_install_once_then_quit_and_launch_before_every_run_with_the_env(self):
        self.project.config(FAKE_LAB + RECIPE)
        self.project.scenario("a_first.py", READ_LOG % ["install-beta", "quit", "launch-beta"])
        self.project.scenario("b_second.py", READ_LOG % ["install-beta", "quit", "launch-beta", "quit", "launch-beta"])
        self.project.scenario("c_installed.py", """
            def scenario(g):
                g.check("artifact delivered and installed", g.exec(["sh", "-c", "cat ~/installed"]).stdout == "app-bytes")
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)

    def test_state_is_reset_after_quitting_and_before_launching(self):
        self.project.config(FAKE_LAB + RECIPE.replace(
            'quit = "echo quit >> ~/deploy.log"', 'quit = "echo quit >> ~/deploy.log; touch ~/.myapp"'
        ).replace(
            'launch = "echo launch-$CHANNEL_ENV >> ~/deploy.log"',
            'launch = "test -e ~/.myapp && echo launch-dirty >> ~/deploy.log || echo launch-clean >> ~/deploy.log"',
        ))
        self.project.scenario("a.py", READ_LOG % ["install-beta", "quit", "launch-clean"])
        self.assertExit(self.project.vmlab("run"), 0)

    def test_a_fresh_scenario_gets_the_app_installed_again(self):
        self.project.config(FAKE_LAB + RECIPE)
        self.project.scenario("a.py", PASS)
        self.project.scenario("b_fresh.py", "FRESH = True\n" + READ_LOG % ["install-beta", "quit", "launch-beta"])
        self.assertExit(self.project.vmlab("run"), 0)

    def test_a_scenario_can_launch_the_app_itself_with_more_env(self):
        self.project.config(FAKE_LAB + RECIPE.replace("launch-$CHANNEL_ENV", "launch-$CHANNEL_ENV-$EXTRA"))
        self.project.scenario("a.py", "LAUNCH = False\n" + """
def scenario(g):
    before = g.exec(["sh", "-c", "cat ~/deploy.log"]).stdout.split()
    g.check("not launched automatically", before == ["install-beta", "quit"], detail=" ".join(before))
    g.launch(env={"EXTRA": "x"})
    after = g.exec(["sh", "-c", "cat ~/deploy.log"]).stdout.split()
    g.check("launched with merged env", after[-1] == "launch-beta-x", detail=" ".join(after))
""")
        self.assertExit(self.project.vmlab("run"), 0)

    def test_a_failing_launch_is_a_run_error(self):
        self.project.config(FAKE_LAB + RECIPE.replace('launch = "echo launch-$CHANNEL_ENV >> ~/deploy.log"', 'launch = "echo no display >&2; exit 1"'))
        self.project.scenario("a.py", PASS)
        r = self.project.vmlab("run")
        self.assertExit(r, 1)
        error = self.project.report()["scenarios"][0]["error"]
        self.assertIn("launch", error)
        self.assertIn("no display", error)

    def test_install_needs_an_artifact(self):
        self.project.config(FAKE_LAB + '[labs.mac.app]\ninstall = "true"\n')
        self.project.scenario("a.py", PASS)
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn("labs.mac.app.artifact", r.err)

    def test_deploy_command_builds_installs_and_launches_and_keeps_the_guest(self):
        self.project.config(FAKE_LAB + RECIPE)
        r = self.project.vmlab("deploy", "mac")
        self.assertExit(r, 0)
        status = {g["lab"]: g["running"] for g in json.loads(self.project.vmlab("status", "--json").out)}
        self.assertEqual(status, {"mac": True})
        self.assertIn("down mac", r.out)

        # An Ad-hoc run keeps Guest state: it sees deploy's recipe, then its own.
        check = self.project.root / "check.py"
        check.write_text(READ_LOG % (["install-beta", "quit", "launch-beta"] * 2))
        self.assertExit(self.project.vmlab("run", check), 0)
