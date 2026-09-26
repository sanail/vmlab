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


# The app writes ~/ready a second after its launch recipe returns, as a tray app registers its icon.
SLOW_APP = """
[labs.mac.app]
launch = "(sleep 1.5; touch ~/ready) >/dev/null 2>&1 &"
quit = "rm -f ~/ready"
"""
READY = 'ready = { file = "~/ready" }\n'
SEES_READY = """
def scenario(g):
    g.check("the app was ready", g.exec(["test", "-e", "%s"]).ok)
"""


class ReadyTest(VmlabTestCase):
    """[labs.LAB.app] ready: the app counts as launched only once its condition holds."""

    def config(self, extra=READY, app=SLOW_APP):
        self.project.config(FAKE_LAB + app + extra)

    def run_error(self):
        r = self.project.vmlab("run")
        self.assertExit(r, 1)
        [scenario] = self.project.report()["scenarios"]
        self.assertEqual(scenario["status"], "error")
        self.assertEqual(scenario["checks"], [])
        return scenario["error"]

    def test_the_runner_waits_for_ready_off_the_scenario_clock(self):
        self.config()
        self.project.scenario("a.py", "TIMEOUT = 1\n" + SEES_READY % "home/ready")
        self.assertExit(self.project.vmlab("run"), 0)

    def test_ready_timeout_defaults_to_the_step_timeout(self):
        self.project.config(FAKE_LAB.replace('arch = "arm64"', 'arch = "arm64"\nstep_timeout = 0.5') + SLOW_APP + READY)
        self.project.scenario("a.py", PASS)
        error = self.run_error()
        self.assertIn("0.5s", error)

    def test_unmet_ready_is_a_run_error_naming_the_condition_and_the_last_answer(self):
        self.config('ready = { exec = ["sh", "-c", "echo tray-missing; exit 3"] }\nready_timeout = 1\n')
        self.project.scenario("a.py", PASS)
        error = self.run_error()
        for fragment in ("labs.mac.app.ready", '"exec"', "tray-missing", '"code": 3', "1s"):
            self.assertIn(fragment, error)

    def test_every_condition_and_gone_work(self):
        self.config('ready = { process = "no-such-process-vmlab", gone = true }\n')
        self.project.scenario("a.py", PASS)
        self.assertExit(self.project.vmlab("run"), 0)

    def tray_app(self, launch):
        """A Lab whose scripted tree gains MyApp's Tray icon in its launch recipe, launch % the tree with it."""
        tray = {"role": "desktop", "children": [{"role": "application", "name": "MyApp", "children": [
            {"role": "trayicon", "children": [{"role": "menuitem", "name": "Quit"}]},
        ]}]}  # fmt: skip
        (self.project.dir / "tree.json").write_text(json.dumps({"role": "desktop", "children": []}))
        (self.project.dir / "tray.json").write_text(json.dumps(tray))
        app = '[labs.mac.app]\nlaunch = "%s"\n' % (launch % (self.project.dir / "tray.json", self.project.dir / "tree.json"))
        return '[labs.mac.fake]\nui_tree = "tree.json"\n' + app

    def test_a_tray_icon_is_waited_for_after_the_launch(self):
        self.config('ready = { tray = "MyApp" }\n', app=self.tray_app("(sleep 1.5; cp '%s' '%s') >/dev/null 2>&1 &"))
        self.project.scenario("a.py", """
def scenario(g):
    g.check("the Tray menu reads without a timeout", g.tray("MyApp")["items"][0]["name"] == "Quit")
""")
        self.assertExit(self.project.vmlab("run"), 0)

    def test_a_tray_icon_that_never_appears_is_a_run_error(self):
        self.config('ready = { tray = "MyApp" }\nready_timeout = 1\n', app=self.tray_app("true # %s %s"))
        self.project.scenario("a.py", PASS)
        error = self.run_error()
        self.assertIn("labs.mac.app.ready", error)
        self.assertIn('"tray": "MyApp"', error)

    def test_g_launch_waits_for_ready(self):
        self.config()
        self.project.scenario("a.py", "LAUNCH = False\n" + """
def scenario(g):
    g.check("not launched yet", not g.exec(["test", "-e", "home/ready"]).ok)
    g.launch()
    g.check("ready after g.launch", g.exec(["test", "-e", "home/ready"]).ok)
""")
        self.assertExit(self.project.vmlab("run"), 0)

    def test_g_launch_unmet_is_a_run_error(self):
        self.config('ready = { file = "~/never" }\nready_timeout = 1\n')
        self.project.scenario("a.py", "LAUNCH = False\ndef scenario(g):\n    g.launch()\n    g.check('ok', True)\n")
        error = self.run_error()
        self.assertIn("a.py:3", error)
        self.assertIn('"file": "~/never"', error)

    def test_g_launch_waits_on_the_scenario_clock(self):
        self.config('ready = { exec = ["sh", "-c", "echo tray-missing; exit 3"] }\nready_timeout = 60\n')
        self.project.scenario("a.py", "LAUNCH = False\nTIMEOUT = 1\ndef scenario(g):\n    g.launch()\n    g.check('ok', True)\n")
        started = time.time()
        error = self.run_error()
        self.assertLess(time.time() - started, 20)
        # The Scenario's clock ran out before ready_timeout: the error still says what it waited for.
        for fragment in ("1s timeout", "labs.mac.app.ready", '"exec"', "tray-missing", '"code": 3'):
            self.assertIn(fragment, error)

    def test_deploy_waits_for_ready(self):
        self.config()
        self.assertExit(self.project.vmlab("deploy", "mac"), 0)
        self.assertExit(self.project.vmlab("exec", "--", "test", "-e", "home/ready"), 0)

    def test_deploy_exits_non_zero_when_not_ready(self):
        self.config('ready = { log = "~/app.log", pattern = "tray: up" }\nready_timeout = 1\n')
        r = self.project.vmlab("deploy", "mac")
        self.assertExit(r, 1)
        self.assertIn("labs.mac.app.ready", r.err)
        self.assertIn('"log": "~/app.log"', r.err)
        self.assertIn("ready_timeout", r.err)

    def test_deploy_stops_at_the_first_lab_that_is_not_ready(self):
        self.project.config(
            FAKE_LAB + SLOW_APP + 'ready = { file = "~/never" }\nready_timeout = 1\n'
            + FAKE_LAB.replace("labs.mac", "labs.later") + '[labs.later.app]\nlaunch = "true"\n'
        )  # fmt: skip
        r = self.project.vmlab("deploy")
        self.assertExit(r, 1)
        self.assertIn("labs.mac.app.ready", r.err)
        self.assertNotIn("later: deployed", r.out)
        status = {g["lab"]: g["running"] for g in json.loads(self.project.vmlab("status", "--json").out)}
        self.assertEqual(status, {"mac": True, "later": False})
        # The Lab that was not ready still has its Guest up: the user is told, and how to stop it.
        self.assertRegex(r.out, r"(?m)^Kept running: mac\. Stop with: .*vmlab.* down mac$")

    def test_a_failed_deploy_names_the_guests_it_left_running(self):
        self.project.config(
            FAKE_LAB.replace("labs.mac", "labs.first") + '[labs.first.app]\nlaunch = "true"\n'
            + FAKE_LAB.replace("labs.mac", "labs.second") + '[labs.second.app]\nlaunch = "true"\n'
            + FAKE_LAB + '[labs.mac.app]\nartifact = "dist/MyApp.zip"\nbuild = "exit 7"\n'
        )  # fmt: skip
        r = self.project.vmlab("deploy")
        self.assertExit(r, 1)
        self.assertIn("second: deployed", r.out)
        # mac's build failed before its Guest was started: it is not named.
        self.assertRegex(r.out, r"(?m)^Kept running: first, second\. Stop with: .*vmlab.* down first second$")
        status = {g["lab"]: g["running"] for g in json.loads(self.project.vmlab("status", "--json").out)}
        self.assertEqual(status, {"first": True, "second": True, "mac": False})


class ReadyConfigTest(VmlabTestCase):
    def assertConfigError(self, app, *fragments):
        self.project.config(FAKE_LAB + '[labs.mac.app]\nlaunch = "true"\n' + app)
        self.project.scenario("a.py", PASS)
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn("fix:", r.err)
        for fragment in fragments:
            self.assertIn(fragment, r.err)
        self.assertEqual(self.project.run_dirs(), [])
        self.last_error = r.err

    def test_no_condition(self):
        self.assertConfigError("ready = {}\n", "labs.mac.app.ready", "no condition")
        self.assertConfigError('ready = { app = "MyApp" }\n', "labs.mac.app.ready", "no condition")

    def test_several_conditions(self):
        self.assertConfigError('ready = { process = "MyApp", file = "~/x" }\n', "labs.mac.app.ready", "several conditions", "process", "file")

    def test_must_be_a_table(self):
        self.assertConfigError('ready = "MyApp"\n', "labs.mac.app.ready", "must be a table")

    def test_unknown_key(self):
        self.assertConfigError('ready = { procss = "MyApp" }\n', "labs.mac.app.ready.procss", "unknown key", "process")

    def test_wrong_types(self):
        self.assertConfigError('ready = { process = 1 }\n', "labs.mac.app.ready.process", "string")
        self.assertConfigError('ready = { process = "x", gone = "yes" }\n', "labs.mac.app.ready.gone", "true or false")
        self.assertConfigError('ready = { exec = "curl localhost" }\n', "labs.mac.app.ready.exec", "list")

    def test_the_same_errors_as_wait_for_in_toml_keys(self):
        self.assertConfigError('ready = { log = "~/app.log" }\n', "labs.mac.app.ready.log: needs pattern")
        self.assertConfigError('ready = { log = "~/app.log", pattern = "(" }\n', "labs.mac.app.ready.pattern:", "not a valid regular expression")
        self.assertConfigError('ready = { file = "~/x", pattern = "y" }\n', "labs.mac.app.ready.pattern: goes with log or exec")
        self.assertConfigError('ready = { process = "MyApp", app = "MyApp" }\n', "labs.mac.app.ready.app: goes with text or role")
        self.assertNotIn("--", self.last_error)

    def test_tray(self):
        self.assertConfigError('ready = { tray = "" }\n', "labs.mac.app.ready.tray", "non-empty string")
        self.assertConfigError('ready = { tray = "A", process = "B" }\n', "labs.mac.app.ready", "several conditions", "process", "tray")
        self.assertConfigError('ready = { tray = "A", app = "A" }\n', "labs.mac.app.ready.app: goes with text or role")
        self.assertConfigError('ready = { tray = "A", pattern = "x" }\n', "labs.mac.app.ready.pattern: goes with log or exec")
        self.assertConfigError("ready = {}\n", "tray")  # the conditions it may hold

    def test_ready_timeout(self):
        self.assertConfigError('ready = { file = "~/x" }\nready_timeout = 0\n', "labs.mac.app.ready_timeout", "> 0")
        self.assertConfigError("ready_timeout = 5\n", "labs.mac.app.ready", "missing")

    def test_ready_needs_a_launch_recipe(self):
        self.project.config(FAKE_LAB + '[labs.mac.app]\nready = { file = "~/x" }\n')
        self.project.scenario("a.py", PASS)
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn("labs.mac.app.launch", r.err)
