"""The cross-OS UI contract through the CLI (`vmlab ui ...`) and the Scenario API.

The Fake Provider serves a scripted tree and emulates a text editor for
stage-text, so the same shapes can be checked here as on real Guests (Seam 2).
"""

import json
import threading
import time
from pathlib import Path

from harness import FAKE_LAB, VmlabTestCase

TREE = {
    "role": "desktop",
    "children": [
        {
            "role": "application",
            "name": "MyApp",
            "children": [
                {
                    "role": "window",
                    "name": "Palette",
                    "bounds": {"x": 100, "y": 100, "w": 400, "h": 300},
                    "children": [
                        {"role": "button", "name": "Run again", "bounds": {"x": 110, "y": 350, "w": 100, "h": 20}},
                        {"role": "button", "name": "Run", "bounds": {"x": 220, "y": 350, "w": 60, "h": 20}},
                        {"role": "text", "name": "", "value": "From the clipboard", "bounds": {"x": 110, "y": 120, "w": 200, "h": 16}},
                    ],
                }
            ],
        },
        {"role": "application", "name": "Other", "children": [{"role": "button", "name": "Run"}]},
    ],
}
NODE_KEYS = {"role", "name", "value", "description", "bounds", "focused", "enabled", "native_role", "children"}


def walk(node):
    yield node
    for child in node["children"]:
        yield from walk(child)


class UiTestCase(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nui_tree = "tree.json"\n')
        (self.project.dir / "tree.json").write_text(json.dumps(TREE))
        self.assertExit(self.project.vmlab("up"), 0)

    def ui(self, *args, code=0):
        r = self.project.vmlab("ui", *args)
        self.assertExit(r, code)
        return json.loads(r.out) if code == 0 else r


class UiCliTest(UiTestCase):
    def test_tree_has_the_shared_shape_on_every_node(self):
        tree = self.ui("tree")
        self.assertEqual(tree["role"], "desktop")
        for node in walk(tree):
            self.assertLessEqual(NODE_KEYS, set(node), node)
        self.assertEqual([a["name"] for a in tree["children"]], ["MyApp", "Other"])

    def test_tree_of_one_app(self):
        tree = self.ui("tree", "--app", "myapp")
        self.assertEqual([a["name"] for a in tree["children"]], ["MyApp"])

    def test_find_prefers_exact_text_over_substring(self):
        found = self.ui("find", "--text", "Run", "--app", "MyApp")
        self.assertEqual([(m["name"], m["app"]) for m in found["matches"]], [("Run", "MyApp")])
        self.assertNotIn("children", found["matches"][0])

    def test_find_falls_back_to_substring_and_matches_values(self):
        found = self.ui("find", "--text", "clipboard")
        self.assertEqual([m["value"] for m in found["matches"]], ["From the clipboard"])

    def test_find_by_role_across_apps(self):
        found = self.ui("find", "--role", "button")
        self.assertEqual([(m["app"], m["name"]) for m in found["matches"]], [("MyApp", "Run again"), ("MyApp", "Run"), ("Other", "Run")])

    def test_find_with_no_match_is_an_empty_list(self):
        self.assertEqual(self.ui("find", "--text", "Nope")["matches"], [])

    def test_find_needs_a_criterion(self):
        r = self.ui("find", code=2)
        self.assertIn("--text", r.err)

    def test_click_hits_the_middle_of_the_element(self):
        clicked = self.ui("click", "--text", "Run", "--app", "MyApp")
        self.assertEqual((clicked["x"], clicked["y"]), (250, 360))
        self.assertEqual(clicked["element"]["name"], "Run")

    def test_click_at_coordinates(self):
        clicked = self.ui("click", "--at", "5", "7")
        self.assertEqual((clicked["x"], clicked["y"]), (5, 7))
        self.assertIsNone(clicked["element"])

    def test_click_on_nothing_fails_naming_what_was_sought(self):
        r = self.ui("click", "--text", "Nope", code=1)
        self.assertIn("Nope", r.err)

    def test_click_on_an_element_without_bounds_fails(self):
        r = self.ui("click", "--text", "Run", "--app", "Other", code=1)
        self.assertIn("no bounds", r.err)

    def test_press_normalises_the_chord(self):
        self.assertEqual(self.ui("press", "Shift+Command+Space")["chord"], "shift+cmd+space")

    def test_press_rejects_an_unknown_key(self):
        r = self.ui("press", "cmd+banana", code=2)
        self.assertIn("banana", r.err)

    def test_clipboard_round_trip(self):
        self.assertEqual(self.ui("clipboard", "--set", "héllo wörld")["text"], "héllo wörld")
        self.assertEqual(self.ui("clipboard")["text"], "héllo wörld")

    def test_stage_text_then_copy_puts_the_text_on_the_clipboard(self):
        staged = self.ui("stage-text", "Ohm law", "--then", "cmd+c")
        self.assertEqual((staged["frontmost"], staged["selected"], staged["pressed"]), (staged["app"], "Ohm law", "cmd+c"))
        self.assertEqual(self.ui("clipboard")["text"], "Ohm law")
        [area] = self.ui("find", "--role", "textarea", "--app", staged["app"])["matches"]
        self.assertEqual(area["value"], "Ohm law")

    def test_typing_replaces_the_selection(self):
        staged = self.ui("stage-text", "old text")
        self.assertEqual(self.ui("type", "new ü")["typed"], 5)
        [area] = self.ui("find", "--role", "textarea", "--app", staged["app"])["matches"]
        self.assertEqual(area["value"], "new ü")

    def test_focus_brings_an_app_and_its_window_to_the_front(self):
        focused = self.ui("focus", "--app", "myapp", "--window", "Pal")
        self.assertEqual(focused, {"app": "MyApp", "window": "Palette", "frontmost": "MyApp"})
        tree = self.ui("tree")
        self.assertEqual([a["name"] for a in tree["children"] if a["focused"]], ["MyApp"])

    def test_focus_on_an_app_that_is_not_running_fails(self):
        r = self.ui("focus", "--app", "Nope", code=1)
        self.assertIn("Nope", r.err)

    def test_focus_on_a_missing_window_fails_naming_the_windows(self):
        r = self.ui("focus", "--app", "MyApp", "--window", "Nope", code=1)
        self.assertIn("Palette", r.err)

    def test_wait_for_an_element_that_is_there(self):
        waited = self.ui("wait-for", "--text", "Palette", "--timeout", "5")
        self.assertTrue(waited["met"])
        self.assertEqual(waited["matches"][0]["name"], "Palette")

    def test_wait_for_times_out_without_sleeping_past_the_timeout(self):
        started = time.time()
        r = self.ui("wait-for", "--text", "Nope", "--timeout", "1", code=1)
        self.assertLess(time.time() - started, 8)
        waited = json.loads(r.out)
        self.assertFalse(waited["met"])
        self.assertGreaterEqual(waited["waited_s"], 1)

    def test_wait_for_gone(self):
        self.assertTrue(self.ui("wait-for", "--text", "Nope", "--gone", "--timeout", "1")["met"])

    def test_wait_for_file_process_and_log_line(self):
        home = self.project.home / "fake"
        [guest] = list(home.iterdir())
        (guest / "fs" / "home" / "app.log").write_text("boot\nready: 42\n")
        self.assertTrue(self.ui("wait-for", "--file", "~/app.log", "--timeout", "2")["met"])
        self.assertTrue(self.ui("wait-for", "--log", "~/app.log", "--pattern", r"ready: \d+", "--timeout", "2")["met"])
        self.ui("wait-for", "--log", "~/app.log", "--pattern", "never", "--timeout", "1", code=1)
        self.ui("wait-for", "--process", "no-such-process-vmlab", "--timeout", "1", code=1)

    def guest_home(self):
        [guest] = list((self.project.home / "fake").iterdir())
        return guest / "fs" / "home"

    def test_gone_inverts_process_file_and_log_conditions(self):
        (self.guest_home() / "app.log").write_text("boot\n")
        self.assertTrue(self.ui("wait-for", "--process", "no-such-process-vmlab", "--gone", "--timeout", "1")["met"])
        self.assertTrue(self.ui("wait-for", "--file", "~/nope", "--gone", "--timeout", "1")["met"])
        waited = json.loads(self.ui("wait-for", "--file", "~/app.log", "--gone", "--timeout", "1", code=1).out)
        self.assertEqual(waited["condition"], {"file": "~/app.log", "gone": True})
        self.assertTrue(self.ui("wait-for", "--log", "~/app.log", "--pattern", "ready", "--gone", "--timeout", "1")["met"])
        self.ui("wait-for", "--log", "~/app.log", "--pattern", "boot", "--gone", "--timeout", "1", code=1)

    def test_exec_is_met_by_exit_code_and_carries_the_last_answer(self):
        waited = self.ui("wait-for", "--timeout", "1", "--exec", "sh", "-c", "echo up; exit 0")
        self.assertTrue(waited["met"])
        self.assertEqual((waited["code"], waited["stdout"]), (0, "up\n"))
        self.assertEqual(waited["condition"], {"exec": ["sh", "-c", "echo up; exit 0"]})
        r = self.ui("wait-for", "--timeout", "1", "--exec", "sh", "-c", "echo down; exit 3", code=1)
        waited = json.loads(r.out)
        self.assertEqual((waited["met"], waited["code"], waited["stdout"]), (False, 3, "down\n"))

    def test_exec_with_a_pattern_is_met_by_its_output_whatever_the_exit_code(self):
        waited = self.ui("wait-for", "--pattern", r"ready: \d+", "--timeout", "1", "--exec", "sh", "-c", "echo ready: 7; exit 4")
        self.assertTrue(waited["met"])
        self.assertEqual(waited["code"], 4)
        self.ui("wait-for", "--pattern", "never", "--timeout", "1", "--exec", "echo", "ready", code=1)

    def test_exec_argv_may_follow_a_double_dash(self):
        self.assertTrue(self.ui("wait-for", "--timeout", "1", "--exec", "--", "sh", "-c", "exit 0")["met"])

    def test_exec_gone_waits_for_the_command_to_stop_succeeding(self):
        self.assertTrue(self.ui("wait-for", "--gone", "--timeout", "1", "--exec", "false")["met"])
        self.ui("wait-for", "--gone", "--timeout", "1", "--exec", "true", code=1)

    def test_exec_turns_true_mid_wait(self):
        flag = self.guest_home() / "flag"
        timer = threading.Timer(1, flag.write_text, ["x"])
        timer.start()
        self.addCleanup(timer.cancel)
        waited = self.ui("wait-for", "--timeout", "10", "--exec", "sh", "-c", "test -e ~/flag")
        self.assertTrue(waited["met"])
        self.assertGreaterEqual(waited["waited_s"], 0.5)

    def test_exec_of_a_command_the_guest_does_not_have_fails_at_once(self):
        started = time.time()
        r = self.ui("wait-for", "--timeout", "30", "--exec", "no-such-command-vmlab", "--flag", code=1)
        self.assertLess(time.time() - started, 15)
        self.assertIn("no-such-command-vmlab", r.err)
        self.assertIn("--flag", r.err)
        self.ui("wait-for", "--timeout", "30", "--exec", "sh", "-c", "no-such-command-vmlab", code=1)

    def test_exec_keeps_only_a_tail_of_its_output(self):
        waited = self.ui("wait-for", "--timeout", "1", "--exec", "sh", "-c", "seq 1 5000")
        self.assertTrue(waited["stdout"].endswith("4999\n5000\n"))
        self.assertLess(len(waited["stdout"]), 5000)

    def test_exec_needs_a_command(self):
        r = self.ui("wait-for", "--exec", code=2)
        self.assertIn("--exec", r.err)

    def test_pattern_goes_with_log_or_exec(self):
        r = self.ui("wait-for", "--file", "a", "--pattern", "x", code=2)
        self.assertIn("--pattern", r.err)

    def test_wait_for_needs_exactly_one_condition(self):
        r = self.ui("wait-for", "--file", "a", "--process", "b", code=2)
        self.assertIn("one condition", r.err)

    def test_screenshot_writes_a_png(self):
        shot = self.ui("screenshot")
        self.assertTrue(Path(shot["path"]).read_bytes().startswith(b"\x89PNG"))

    def test_ui_on_a_stopped_guest_says_how_to_start_it(self):
        self.assertExit(self.project.vmlab("down"), 0)
        r = self.ui("tree", code=1)
        self.assertIn("vmlab up mac", r.err)

    def test_lab_is_required_when_there_are_several(self):
        self.project.config(FAKE_LAB + '[labs.win]\nprovider = "fake"\nos = "windows"\n')
        r = self.ui("tree", code=2)
        self.assertIn("--lab", r.err)
        self.assertExit(self.project.vmlab("up", "win"), 0)
        self.assertEqual(self.ui("tree", "--lab", "win")["role"], "desktop")


class BrokenChannelWaitTest(VmlabTestCase):
    def test_a_failed_channel_is_not_met_yet(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh"]\nbroken_channels = ["ssh"]\n')
        self.assertExit(self.project.vmlab("up"), 0)
        started = time.time()
        r = self.project.vmlab("ui", "wait-for", "--timeout", "1", "--exec", "true")
        self.assertExit(r, 1)
        waited = json.loads(r.out)
        self.assertFalse(waited["met"])
        self.assertGreaterEqual(waited["waited_s"], 1)
        self.assertLess(time.time() - started, 15)
        self.assertIn("broken", waited["error"])
        self.assertEqual((waited["code"], waited["stdout"]), (None, ""))


class LinuxUiTest(VmlabTestCase):
    """Linux helpers report AT-SPI role names; the Host maps them like macOS's AX roles."""

    TREE = {
        "native_role": "desktop",
        "children": [{"native_role": "application", "name": "gnome-text-editor", "children": [{
            "native_role": "frame", "name": "notes.txt - Text Editor", "children": [
                {"native_role": "push button", "name": "Open"},
                {"native_role": "label", "name": "notes.txt"},
                {"native_role": "text", "role": "textarea", "value": "hello"},  # the helper tells text areas from fields
                {"native_role": "check box", "name": "Wrap"},
                {"native_role": "spin button", "name": "Size"},
            ],
        }]}],
    }  # fmt: skip

    def setUp(self):
        super().setUp()
        self.project.config('[labs.linux]\nprovider = "fake"\nos = "linux"\n[labs.linux.fake]\nui_tree = "tree.json"\n')
        (self.project.dir / "tree.json").write_text(json.dumps(self.TREE))
        self.assertExit(self.project.vmlab("up"), 0)

    def test_at_spi_roles_map_to_the_cross_os_roles(self):
        r = self.project.vmlab("ui", "tree")
        self.assertExit(r, 0)
        roles = [(n["native_role"], n["role"]) for n in walk(json.loads(r.out))]
        self.assertEqual(roles, [
            ("desktop", "desktop"), ("application", "application"), ("frame", "window"), ("push button", "button"),
            ("label", "text"), ("text", "textarea"), ("check box", "checkbox"), ("spin button", "spinbutton"),
        ])  # fmt: skip

    def test_stage_text_opens_the_stock_editor(self):
        r = self.project.vmlab("ui", "stage-text", "Ohm law")
        self.assertExit(r, 0)
        self.assertEqual(json.loads(r.out)["app"], "gnome-text-editor")


class WindowsUiTest(VmlabTestCase):
    """Windows helpers report UI Automation control types; the Host maps them like the others."""

    TREE = {
        "native_role": "desktop",
        "children": [{"native_role": "application", "name": "Notepad", "children": [{
            "native_role": "Window", "name": "notes.txt - Notepad", "children": [
                {"native_role": "Button", "name": "Open"},
                {"native_role": "Text", "name": "notes.txt"},
                {"native_role": "Document", "role": "textarea", "value": "hello"},  # the helper tells text areas from pages
                {"native_role": "CheckBox", "name": "Wrap"},
                {"native_role": "TabItem", "name": "notes.txt"},
                {"native_role": "Spinner", "name": "Size"},
            ],
        }]}],
    }  # fmt: skip

    def setUp(self):
        super().setUp()
        self.project.config('[labs.win]\nprovider = "fake"\nos = "windows"\n[labs.win.fake]\nui_tree = "tree.json"\n')
        (self.project.dir / "tree.json").write_text(json.dumps(self.TREE))
        self.assertExit(self.project.vmlab("up"), 0)

    def test_ui_automation_control_types_map_to_the_cross_os_roles(self):
        r = self.project.vmlab("ui", "tree")
        self.assertExit(r, 0)
        roles = [(n["native_role"], n["role"]) for n in walk(json.loads(r.out))]
        self.assertEqual(roles, [
            ("desktop", "desktop"), ("application", "application"), ("Window", "window"), ("Button", "button"),
            ("Text", "text"), ("Document", "textarea"), ("CheckBox", "checkbox"), ("TabItem", "tab"), ("Spinner", "spinner"),
        ])  # fmt: skip

    def test_stage_text_opens_notepad(self):
        r = self.project.vmlab("ui", "stage-text", "Ohm law")
        self.assertExit(r, 0)
        self.assertEqual(json.loads(r.out)["app"], "Notepad")


class UiScenarioTest(UiTestCase):
    def test_scenario_api_returns_the_cli_shapes(self):
        self.project.scenario("ui.py", """
            def scenario(g):
                g.check("find", g.find(text="Run", app="MyApp")["matches"][0]["name"] == "Run")
                g.check("click", g.click(text="Run", app="MyApp")["x"] == 250)
                g.check("press", g.press("cmd+shift+space")["chord"] == "shift+cmd+space")
                g.check("focus", g.focus("MyApp")["frontmost"] == "MyApp")
                staged = g.stage_text("hello", then="cmd+c")
                g.check("staged", staged["selected"] == "hello")
                g.check("clipboard", g.clipboard()["text"] == "hello")
                g.set_clipboard("other")
                g.check("set clipboard", g.clipboard()["text"] == "other")
                g.check("type", g.type("abc")["typed"] == 3)
                g.check("wait", g.wait_for(role="textarea", text="abc", timeout=5)["met"])
                g.check("wait not met", not g.wait_for(text="Nope", timeout=0.5)["met"])
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)

    def test_bad_chord_in_a_scenario_errors_with_its_line(self):
        self.project.scenario("chord.py", """
            def scenario(g):
                g.press("cmd+nope")
                g.check("unreachable", True)
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        [scenario] = self.project.report()["scenarios"]
        self.assertEqual(scenario["status"], "error")
        self.assertIn("chord.py:3", scenario["error"])
        self.assertIn("nope", scenario["error"])

    def test_wait_for_is_bounded_by_the_scenario_timeout(self):
        self.project.scenario("slow.py", """
            TIMEOUT = 1
            def scenario(g):
                g.wait_for(text="Nope", timeout=30)
                g.check("unreachable", True)
        """)
        started = time.time()
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertLess(time.time() - started, 15)
        self.assertIn("timeout", self.project.report()["scenarios"][0]["error"])

    def test_exec_and_gone_conditions_in_a_scenario(self):
        self.project.scenario("exec.py", """
            def scenario(g):
                g.check("exec", g.wait_for(exec=["sh", "-c", "echo ready"], pattern="ready", timeout=2)["stdout"] == "ready\\n")
                g.check("exec gone", g.wait_for(exec=["false"], gone=True, timeout=2)["met"])
                g.check("process gone", g.wait_for(process="no-such-process-vmlab", gone=True, timeout=2)["met"])
                g.check("not met", not g.wait_for(exec=["false"], timeout=0.5)["met"])
        """)
        self.assertExit(self.project.vmlab("run"), 0)

    def test_an_exec_wait_is_bounded_by_the_scenario_timeout(self):
        self.project.scenario("slow.py", """
            TIMEOUT = 1
            def scenario(g):
                g.wait_for(exec=["false"], timeout=30)
                g.check("unreachable", True)
        """)
        started = time.time()
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertLess(time.time() - started, 15)
        self.assertIn("timeout", self.project.report()["scenarios"][0]["error"])

    def test_a_missing_command_errors_the_scenario_naming_it(self):
        self.project.scenario("typo.py", """
            def scenario(g):
                g.wait_for(exec=["no-such-command-vmlab"], timeout=30)
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertIn("no-such-command-vmlab", self.project.report()["scenarios"][0]["error"])
