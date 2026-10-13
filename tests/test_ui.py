"""The cross-OS UI contract through the CLI (`vmlab ui ...`) and the Scenario API.

The Fake Provider serves a scripted tree and emulates a text editor for
stage-text, so the same shapes can be checked here as on real Guests (Seam 2).
"""

import json
import os
import struct
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from harness import FAKE_LAB, VmlabTestCase


def write_whole(path, text):
    """Write path whole or not at all: a command polling the file must never read it half-written."""
    part = path.with_suffix(".part")
    part.write_text(text)
    os.replace(part, path)

TREE = {
    "role": "desktop",
    "children": [
        {
            "role": "application",
            "name": "MyApp",
            "bundle_id": "com.example.myapp",
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
NODE_KEYS = {"role", "name", "value", "description", "bounds", "focused", "enabled", "checked", "native_role", "children"}


def palette(tree):
    """The elements of MyApp's Palette window in tree (Run is [1])."""
    return tree["children"][0]["children"][0]["children"]


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

    def staged_files(self):
        """The Staged documents' files left in the Fake Guest's temp folder."""
        return sorted(p.name for p in self.project.home.glob("fake/*/fs/tmp/vmlab-stage-*.txt"))

    def write_tree(self, covered_by=None):
        """The scripted tree, with MyApp's Run button covered by covered_by (the Fake refuses to click it)."""
        tree = json.loads(json.dumps(TREE))
        if covered_by:
            palette(tree)[1]["covered_by"] = covered_by
        write_whole(self.project.dir / "tree.json", json.dumps(tree))

    def later(self, seconds, fn):
        """fn in seconds, on a thread of its own; cancelled at cleanup if it has not run yet."""
        timer = threading.Timer(seconds, fn)
        timer.start()
        self.addCleanup(timer.cancel)


class UiCliTest(UiTestCase):
    def test_tree_has_the_shared_shape_on_every_node(self):
        tree = self.ui("tree")
        self.assertEqual(tree["role"], "desktop")
        for node in walk(tree):
            self.assertLessEqual(NODE_KEYS, set(node), node)
        self.assertEqual([a["name"] for a in tree["children"]], ["MyApp", "Other"])

    def test_a_node_the_helper_gives_no_tick_state_reads_checked_null(self):
        self.assertEqual({node["checked"] for node in walk(self.ui("tree"))}, {None})

    def test_tree_of_one_app(self):
        tree = self.ui("tree", "--app", "myapp")
        self.assertEqual([a["name"] for a in tree["children"]], ["MyApp"])

    def test_tree_of_one_app_by_its_bundle_id(self):
        tree = self.ui("tree", "--app", "com.example.MyApp")
        self.assertEqual([a["name"] for a in tree["children"]], ["MyApp"])

    def test_find_by_the_apps_bundle_id_names_the_app_by_its_name(self):
        matches = self.ui("find", "--text", "Run", "--app", "com.example.myapp")["matches"]
        self.assertEqual([(m["name"], m["app"]) for m in matches], [("Run", "MyApp")])

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

    def test_a_closed_menu_is_left_out_of_the_tree_and_an_open_one_kept(self):
        tree = json.loads(json.dumps(TREE))

        def icon(menu_bounds):
            item = {"role": "menuitem", "name": "Settings", "bounds": dict(menu_bounds, h=menu_bounds["h"] and 20)}
            return {"role": "menubaritem", "name": "MyApp", "bounds": {"x": 900, "y": 0, "w": 24, "h": 24},
                    "children": [{"role": "menu", "bounds": menu_bounds, "children": [item]}]}  # fmt: skip

        tree["children"][0]["children"].append(icon({"x": 0, "y": 1080, "w": 0, "h": 0}))  # macOS: a Tray menu not open
        write_whole(self.project.dir / "tree.json", json.dumps(tree))
        self.assertEqual(self.ui("find", "--text", "Settings")["matches"], [])
        self.assertEqual([m["role"] for m in self.ui("find", "--text", "MyApp", "--role", "menubaritem")["matches"]], ["menubaritem"])
        tree["children"][0]["children"][-1] = icon({"x": 900, "y": 24, "w": 200, "h": 60})
        write_whole(self.project.dir / "tree.json", json.dumps(tree))
        self.assertEqual([m["role"] for m in self.ui("find", "--text", "Settings")["matches"]], ["menuitem"])

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

    def test_click_on_a_covered_element_fails_at_once_naming_what_covers_it(self):
        self.write_tree(covered_by="loginwindow")
        started = time.time()
        r = self.ui("click", "--text", "Run", "--app", "MyApp", code=1)
        self.assertIn("loginwindow", r.err)
        self.assertLess(time.time() - started, 8)

    def test_click_with_a_timeout_waits_for_the_element_to_appear(self):
        tree = json.loads(json.dumps(TREE))
        palette(tree).append({"role": "button", "name": "Later", "bounds": {"x": 300, "y": 350, "w": 40, "h": 20}})
        self.later(1, lambda: write_whole(self.project.dir / "tree.json", json.dumps(tree)))
        self.assertEqual(self.ui("click", "--text", "Later", "--timeout", "10")["element"]["name"], "Later")

    def test_click_with_a_timeout_waits_for_the_element_to_be_uncovered(self):
        self.write_tree(covered_by="loginwindow")
        self.later(1, self.write_tree)
        clicked = self.ui("click", "--text", "Run", "--app", "MyApp", "--timeout", "10")
        self.assertEqual((clicked["x"], clicked["y"]), (250, 360))

    def test_click_that_times_out_reports_the_last_reason(self):
        started = time.time()
        r = self.ui("click", "--text", "Nope", "--timeout", "1", code=1)
        self.assertGreaterEqual(time.time() - started, 1)
        self.assertIn("within 1s", r.err)
        self.assertIn("no element to click matches text='Nope'", r.err)
        self.write_tree(covered_by="loginwindow")
        r = self.ui("click", "--text", "Run", "--app", "MyApp", "--timeout", "1", code=1)
        self.assertIn("loginwindow", r.err)

    def test_click_at_coordinates_ignores_the_timeout(self):
        clicked = self.ui("click", "--at", "5", "7", "--timeout", "10")
        self.assertEqual((clicked["x"], clicked["y"]), (5, 7))

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

    def editor_windows(self, app):
        return [n["name"] for n in walk(self.ui("tree", "--app", app)) if n["role"] == "window"]

    def test_a_stage_closes_nothing(self):
        first = self.ui("stage-text", "first")
        second = self.ui("stage-text", "second")
        self.assertNotEqual(first["file"], second["file"])
        self.assertEqual(sorted(self.editor_windows(second["app"])), sorted([Path(first["file"]).name, Path(second["file"]).name]))
        [area, _] = self.ui("find", "--role", "textarea", "--app", second["app"])["matches"]
        self.assertEqual((area["value"], second["selected"]), ("second", "second"))

    def test_close_staged_closes_just_that_staged_document(self):
        first = self.ui("stage-text", "first")
        second = self.ui("stage-text", "second")
        self.assertEqual(self.ui("close-staged", "--file", first["file"]), {"file": first["file"], "closed": True})
        self.assertEqual(self.editor_windows(second["app"]), [Path(second["file"]).name])
        [area] = self.ui("find", "--role", "textarea", "--app", second["app"])["matches"]
        self.assertEqual(area["value"], "second")

    def test_closing_a_staged_document_that_is_gone_succeeds(self):
        staged = self.ui("stage-text", "text")
        self.ui("close-staged", "--file", staged["file"])
        self.assertEqual(self.ui("close-staged", "--file", staged["file"]), {"file": staged["file"], "closed": False})

    def test_close_staged_removes_the_staged_documents_file_from_the_guest(self):
        first = self.ui("stage-text", "first")
        second = self.ui("stage-text", "second")
        self.assertEqual(self.staged_files(), sorted([Path(first["file"]).name, Path(second["file"]).name]))
        self.ui("close-staged", "--file", first["file"])
        self.assertEqual(self.staged_files(), [Path(second["file"]).name])

    def test_close_staged_removes_the_file_of_one_the_editor_closed_already(self):
        staged = self.ui("stage-text", "text")
        [state] = self.project.home.glob("fake/*/fs/ui-state.json")
        state.unlink()  # the editor closed it
        self.assertEqual(self.ui("close-staged", "--file", staged["file"]), {"file": staged["file"], "closed": False})
        self.assertEqual(self.staged_files(), [])

    def test_close_staged_refuses_a_file_not_named_as_staged(self):
        r = self.ui("close-staged", "--file", "/tmp/notes.txt", code=2)
        self.assertIn("not a Staged document", r.err)

    def test_close_staged_refuses_a_file_outside_the_staging_folder(self):
        staged = self.ui("stage-text", "text")
        r = self.ui("close-staged", "--file", "/home/me/" + Path(staged["file"]).name, code=1)
        self.assertIn("not a Staged document", r.err)
        self.assertEqual(self.editor_windows(staged["app"]), [Path(staged["file"]).name])

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

    def test_a_powershell_command_not_found_fails_at_once_in_any_language(self):
        # Windows PowerShell names the error in every language; its message is localized.
        german = 'echo Die Benennung Get-Nope wurde nicht erkannt. >&2; echo "    + FullyQualifiedErrorId : CommandNotFoundException" >&2; exit 1'
        started = time.time()
        r = self.ui("wait-for", "--timeout", "30", "--exec", "sh", "-c", german, code=1)
        self.assertLess(time.time() - started, 15)
        self.assertIn("no such command", r.err)

    def test_process_names_are_matched_literally(self):
        name = "vmlab (t%s)" % uuid.uuid4().hex[:4]
        self.assert_process_waits(name)

    def assert_process_waits(self, name):
        link = self.guest_home() / name
        link.symlink_to("/bin/sleep")
        proc = subprocess.Popen([str(link), "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertTrue(self.ui("wait-for", "--process", name, "--timeout", "10")["met"])
        self.ui("wait-for", "--process", name[:-1], "--timeout", "1", code=1)
        self.ui("wait-for", "--process", name, "--gone", "--timeout", "1", code=1)
        proc.kill()
        proc.wait()
        self.assertTrue(self.ui("wait-for", "--process", name, "--gone", "--timeout", "10")["met"])

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

    def test_app_goes_with_an_element(self):
        r = self.ui("wait-for", "--process", "MyApp", "--app", "MyApp", code=2)
        self.assertIn("--app goes with --text or --role", r.err)

    def test_wait_for_needs_exactly_one_condition(self):
        r = self.ui("wait-for", "--file", "a", "--process", "b", code=2)
        self.assertIn("one condition", r.err)

    def test_screenshot_writes_a_png(self):
        shot = self.ui("screenshot")
        self.assertTrue(Path(shot["path"]).read_bytes().startswith(b"\x89PNG"))

    def test_displays_lists_the_guests_one_display(self):
        self.assertEqual(self.ui("displays"), {"displays": [{"bounds": {"x": 0, "y": 0, "w": 1024, "h": 768}, "scale": 1, "primary": True}]})

    def test_the_desktop_node_and_a_screenshot_cover_the_display_in_desktop_pixels(self):
        write_whole(self.project.dir / "tree.json", json.dumps(dict(TREE, bounds={"x": 0, "y": 0, "w": 2048, "h": 1536})))
        [display] = self.ui("displays")["displays"]
        self.assertEqual(display["bounds"], {"x": 0, "y": 0, "w": 2048, "h": 1536})
        self.assertEqual(self.ui("tree")["bounds"], display["bounds"])
        png = Path(self.ui("screenshot")["path"]).read_bytes()
        self.assertEqual(struct.unpack(">II", png[16:24]), (2048, 1536))

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


# MyApp's Tray icon, as the Fake scripts one: a trayicon node of the app, its Tray menu's items as menuitems.
TRAY_ICON = {
    "role": "trayicon",
    "children": [
        {"role": "menuitem", "name": "Open"},
        {"role": "separator"},
        {"role": "menuitem", "name": "Settings", "children": [
            {"role": "menuitem", "name": "Advanced"},
            {"role": "menuitem", "name": "Dark mode", "checked": True},
        ]},
        {"role": "menuitem", "name": "Update", "enabled": False},
        {"role": "menuitem", "name": "Quit"},
    ],
}  # fmt: skip
TRAY_ITEMS = [
    {"name": "Open", "enabled": True, "checked": False, "children": []},
    {"name": "Settings", "enabled": True, "checked": False, "children": [
        {"name": "Advanced", "enabled": True, "checked": False, "children": []},
        {"name": "Dark mode", "enabled": True, "checked": True, "children": []},
    ]},
    {"name": "Update", "enabled": False, "checked": False, "children": []},
    {"name": "Quit", "enabled": True, "checked": False, "children": []},
]  # fmt: skip


class TrayTest(UiTestCase):
    def setUp(self):
        super().setUp()
        self.write_tray_tree()

    def write_tray_tree(self, icon=TRAY_ICON):
        tree = json.loads(json.dumps(TREE))
        if icon:
            tree["children"][0]["children"].append(icon)
        write_whole(self.project.dir / "tree.json", json.dumps(tree))

    def tray(self, *args):
        """(exit code, JSON printed, stderr) of `vmlab ui tray ARGS`."""
        r = self.project.vmlab("ui", "tray", *args)
        return r.code, json.loads(r.out), r.err

    def choices(self):
        log = next(self.project.home.glob("fake/*/commands.jsonl")).read_text()
        return [e["chosen"] for e in map(json.loads, log.splitlines()) if e["event"] == "tray"]

    def test_lists_the_tray_menu_with_its_submenus(self):
        self.assertEqual(self.tray("--app", "myapp"), (0, {"items": TRAY_ITEMS, "chosen": None}, ""))
        self.assertEqual(self.choices(), [])

    def test_chooses_a_top_level_item(self):
        code, result, _ = self.tray("--app", "MyApp", "--choose", "Quit")
        self.assertEqual((code, result["chosen"]), (0, ["Quit"]))
        self.assertEqual(result["items"], TRAY_ITEMS)
        self.assertEqual(self.choices(), [["Quit"]])

    def test_chooses_an_item_of_a_submenu_one_label_per_level(self):
        code, result, _ = self.tray("--app", "MyApp", "--choose", "Settings", "--choose", "Advanced")
        self.assertEqual((code, result["chosen"]), (0, ["Settings", "Advanced"]))
        self.assertEqual(self.choices(), [["Settings", "Advanced"]])

    def test_an_app_without_a_tray_icon_fails_at_once(self):
        started = time.time()
        code, result, err = self.tray("--app", "Other")
        self.assertEqual((code, result["items"], result["chosen"]), (1, [], None))
        self.assertIn("Other has no Tray icon", err)
        self.assertIn(result["error"], err)
        self.assertLess(time.time() - started, 8)

    def test_a_missing_label_fails_with_the_items_of_its_level(self):
        code, result, err = self.tray("--app", "MyApp", "--choose", "Settings", "--choose", "Nope")
        self.assertEqual((code, result["items"], result["chosen"]), (1, TRAY_ITEMS, None))
        self.assertIn('no item "Nope" under Settings', err)
        self.assertIn("Advanced, Dark mode", err)
        self.assertEqual(self.choices(), [])

    def test_a_disabled_item_fails(self):
        code, result, err = self.tray("--app", "MyApp", "--choose", "Update")
        self.assertEqual((code, result["items"], result["chosen"]), (1, TRAY_ITEMS, None))
        self.assertIn('"Update" is disabled', err)
        self.assertEqual(self.choices(), [])

    def test_a_label_past_an_item_without_a_submenu_fails(self):
        code, result, err = self.tray("--app", "MyApp", "--choose", "Quit", "--choose", "Now")
        self.assertEqual(code, 1)
        self.assertIn('"Quit" has no submenu', err)

    def test_choosing_a_submenus_parent_fails_naming_its_items(self):
        code, result, err = self.tray("--app", "MyApp", "--choose", "Settings")
        self.assertEqual((code, result["items"], result["chosen"]), (1, TRAY_ITEMS, None))
        self.assertIn('MyApp\'s Tray menu item "Settings" opens a submenu; choose one of its items: Advanced, Dark mode', err)
        self.assertIn(result["error"], err)
        self.assertEqual(self.choices(), [])

    def test_choosing_a_deeper_or_empty_submenus_parent_says_where_and_what_it_holds(self):
        icon = json.loads(json.dumps(TRAY_ICON))
        icon["children"][2]["children"].append({"role": "menuitem", "name": "Network", "children": [
            {"role": "menuitem", "name": "Proxy", "children": [{"role": "menuitem", "name": "Auto"}, {"role": "menuitem", "name": "Manual"}]},
        ]})  # fmt: skip
        icon["children"].append({"role": "menuitem", "name": "Recent", "children": []})
        self.write_tray_tree(icon)
        code, _, err = self.tray("--app", "MyApp", "--choose", "Settings", "--choose", "Network", "--choose", "Proxy")
        self.assertEqual(code, 1)
        self.assertIn('MyApp\'s Tray menu item "Proxy" under Settings > Network opens a submenu; choose one of its items: Auto, Manual', err)
        code, _, err = self.tray("--app", "MyApp", "--choose", "Recent")
        self.assertEqual(code, 1)
        self.assertIn('MyApp\'s Tray menu item "Recent" opens a submenu; choose one of its items: none', err)
        self.assertEqual(self.choices(), [])

    def test_a_timeout_waits_for_the_tray_icon_to_appear(self):
        self.write_tray_tree(icon=None)
        self.later(1, self.write_tray_tree)
        code, result, _ = self.tray("--app", "MyApp", "--choose", "Open", "--timeout", "10")
        self.assertEqual((code, result["chosen"]), (0, ["Open"]))

    def test_a_timeout_waits_for_the_item_to_choose_while_the_menu_is_filled(self):
        unnamed = json.loads(json.dumps(TRAY_ICON))
        for item in unnamed["children"]:
            item.pop("name", None)
        self.write_tray_tree(icon=unnamed)  # macOS, right after a launch: every item read untitled
        self.later(1, self.write_tray_tree)
        code, result, _ = self.tray("--app", "MyApp", "--choose", "Open", "--timeout", "10")
        self.assertEqual((code, result["chosen"]), (0, ["Open"]))
        self.write_tray_tree(icon=unnamed)
        code, _, err = self.tray("--app", "MyApp", "--choose", "Open", "--timeout", "1")
        self.assertEqual(code, 1)
        self.assertIn('no item "Open"', err)

    def test_a_timeout_that_runs_out_says_so(self):
        self.write_tray_tree(icon=None)
        started = time.time()
        code, _, err = self.tray("--app", "MyApp", "--timeout", "1")
        self.assertGreaterEqual(time.time() - started, 1)
        self.assertEqual(code, 1)
        self.assertIn("MyApp has no Tray icon within 1s", err)

    def test_app_is_required(self):
        r = self.project.vmlab("ui", "tray")
        self.assertExit(r, 2)
        self.assertIn("--app", r.err)

    def test_scenario_api_returns_the_cli_json(self):
        self.project.scenario("tray.py", """
            def scenario(g):
                listed = g.tray("MyApp")
                g.check("listed", listed == {"items": %r, "chosen": None}, detail=listed)
                g.check("one label", g.tray("MyApp", choose="Quit")["chosen"] == ["Quit"])
                g.check("a path", g.tray("MyApp", choose=["Settings", "Dark mode"])["chosen"] == ["Settings", "Dark mode"])
                try:
                    g.tray("MyApp", choose="Nope")
                    g.check("a missing label raises", False)
                except Exception as exc:
                    g.check("a missing label raises", "Open, Settings, Update, Quit" in str(exc), detail=str(exc))
        """ % TRAY_ITEMS)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)
        self.assertEqual(self.choices(), [["Quit"], ["Settings", "Dark mode"]])

    def test_choose_takes_labels_only(self):
        self.project.scenario("tray.py", """
            def scenario(g):
                g.tray("MyApp", choose=["Settings", 3])
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertIn("choose takes a label or a list of labels", self.project.report()["scenarios"][0]["error"])


class TrayWaitTest(TrayTest):
    """wait_for's tray condition: met once g.tray would find the app's Tray icon; it never opens the menu."""

    def tray_calls(self):
        log = next(self.project.home.glob("fake/*/commands.jsonl")).read_text()
        return [e["params"] for e in map(json.loads, log.splitlines()) if e["event"] == "ui" and e["command"] == "tray"]

    def test_is_met_once_the_tray_icon_appears(self):
        self.write_tray_tree(icon=None)
        self.later(1, self.write_tray_tree)
        waited = self.ui("wait-for", "--tray", "MyApp", "--timeout", "10")
        self.assertTrue(waited["met"])
        self.assertGreaterEqual(waited["waited_s"], 0.5)
        self.assertEqual(waited["condition"], {"tray": "MyApp"})
        # Every poll asks for the icon only: no menu is read or chosen from.
        calls = self.tray_calls()
        self.assertGreater(len(calls), 1)
        self.assertEqual(calls, [{"app": "MyApp", "icon_only": True}] * len(calls))
        self.assertEqual(self.choices(), [])

    def test_is_unmet_within_the_timeout_when_it_never_appears(self):
        self.write_tray_tree(icon=None)
        started = time.time()
        waited = json.loads(self.ui("wait-for", "--tray", "MyApp", "--timeout", "1", code=1).out)
        self.assertLess(time.time() - started, 8)
        self.assertEqual((waited["met"], waited["condition"]), (False, {"tray": "MyApp"}))
        self.assertGreaterEqual(waited["waited_s"], 1)

    def test_gone_is_met_once_the_icon_goes(self):
        self.later(1, lambda: self.write_tray_tree(icon=None))
        waited = self.ui("wait-for", "--tray", "MyApp", "--gone", "--timeout", "10")
        self.assertTrue(waited["met"])
        self.assertEqual(waited["condition"], {"tray": "MyApp", "gone": True})
        self.ui("wait-for", "--tray", "MyApp", "--timeout", "1", code=1)

    def test_a_guest_that_shows_no_tray_icons_is_never_met_and_says_why(self):
        tree = json.loads(json.dumps(TREE))
        tree["tray_detail"] = "the Desktop session has no StatusNotifierWatcher"
        write_whole(self.project.dir / "tree.json", json.dumps(tree))
        for gone in ([], ["--gone"]):  # it cannot tell, so the icon is neither there nor gone
            waited = json.loads(self.ui("wait-for", "--tray", "MyApp", "--timeout", "1", *gone, code=1).out)
            self.assertEqual(waited["detail"], "the Desktop session has no StatusNotifierWatcher")
            self.assertIn("no StatusNotifierWatcher", waited["error"])
        code, _, err = self.tray("--app", "MyApp")
        self.assertEqual(code, 1)
        self.assertIn("no StatusNotifierWatcher", err)

    def test_it_is_one_condition_without_app_pattern_or_since(self):
        r = self.ui("wait-for", "--tray", "MyApp", "--process", "MyApp", code=2)
        self.assertIn("exactly one condition", r.err)
        self.assertIn("--tray", r.err)
        r = self.ui("wait-for", "--tray", "MyApp", "--text", "Run", code=2)
        self.assertIn("exactly one condition", r.err)
        r = self.ui("wait-for", "--tray", "MyApp", "--app", "MyApp", code=2)
        self.assertIn("--app goes with --text or --role", r.err)
        r = self.ui("wait-for", "--tray", "MyApp", "--pattern", "x", code=2)
        self.assertIn("--pattern goes with --log or --exec", r.err)
        r = self.ui("wait-for", "--tray", "MyApp", "--since", "2026-09-25T10:00:00Z", code=2)
        self.assertIn("--since goes with --notification", r.err)
        r = self.ui("wait-for", "--tray", "", code=2)
        self.assertIn("--tray needs the app", r.err)

    def test_scenario_api(self):
        self.write_tray_tree(icon=None)
        self.later(1, self.write_tray_tree)
        self.project.scenario("tray.py", """
            def scenario(g):
                waited = g.wait_for(tray="MyApp", timeout=10)
                g.check("met", waited["met"] and waited["condition"] == {"tray": "MyApp"}, detail=waited)
                g.check("the menu reads at once", g.tray("MyApp")["items"][0]["name"] == "Open")
                g.check("another app's is not there", not g.wait_for(tray="Other", timeout=0.5)["met"])
                g.check("nor gone from MyApp", not g.wait_for(tray="MyApp", gone=True, timeout=0.5)["met"])
                try:
                    g.wait_for(tray="MyApp", app="MyApp")
                    g.check("app does not go with tray", False)
                except Exception as exc:
                    g.check("app does not go with tray", "app goes with text or role" in str(exc), detail=str(exc))
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)
        self.assertEqual(self.choices(), [])


def iso(seconds_ago=0):
    """A time seconds_ago before now, as a helper gives a Notification's time (the Fake's Guest clock is the Host's)."""
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class NotificationsTest(VmlabTestCase):
    """Notifications the Fake scripts in notifications.json, read on every call."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.notifications = self.project.dir / "notifications.json"
        self.earlier = {"app": "com.example.myapp", "title": "Build done", "body": "earlier run", "time": iso(3600)}
        self.other = {"app": "com.apple.ScriptEditor2", "title": "Script", "body": "hello from Script Editor", "time": iso(1800)}
        self.write([self.other, self.earlier])
        self.assertExit(self.project.vmlab("up"), 0)

    def configure(self, app=""):
        self.project.config(FAKE_LAB + app + '[labs.mac.fake]\nnotifications = "notifications.json"\n')

    def write(self, notifications):
        write_whole(self.notifications, json.dumps(notifications))

    def post(self, title, body, app="com.example.myapp"):
        """Add a Notification posted now to the scripted ones."""
        self.write(json.loads(self.notifications.read_text()) + [{"app": app, "title": title, "body": body, "time": iso()}])

    def listed(self, *args):
        r = self.project.vmlab("ui", "notifications", *args)
        self.assertExit(r, 0)
        return json.loads(r.out)["notifications"]

    def later(self, seconds, fn):
        timer = threading.Timer(seconds, fn)
        timer.start()
        self.addCleanup(timer.cancel)

    def test_lists_every_apps_notifications_oldest_first(self):
        listed = self.listed()
        self.assertEqual([n["body"] for n in listed], ["earlier run", "hello from Script Editor"])
        self.assertEqual(set(listed[0]), {"app", "title", "body", "time"})
        self.assertEqual(listed[0]["app"], "com.example.myapp")
        self.assertRegex(listed[0]["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_app_narrows_them_to_one_apps(self):
        self.assertEqual([n["title"] for n in self.listed("--app", "com.apple.scripteditor2")], ["Script"])
        self.assertEqual(self.listed("--app", "com.example.none"), [])

    def test_app_defaults_to_the_labs_notification_id(self):
        self.configure('[labs.mac.app]\nnotification_id = "com.example.myapp"\n')
        self.assertEqual([n["title"] for n in self.listed()], ["Build done"])
        self.assertEqual([n["title"] for n in self.listed("--app", "com.apple.ScriptEditor2")], ["Script"])

    def test_text_is_a_pattern_matched_against_title_and_body(self):
        self.assertEqual([n["body"] for n in self.listed("--text", "^Build")], ["earlier run"])
        self.assertEqual([n["body"] for n in self.listed("--text", r"from \w+ Editor")], ["hello from Script Editor"])
        self.assertEqual(self.listed("--text", "nowhere"), [])

    def test_since_leaves_out_earlier_ones(self):
        self.assertEqual([n["title"] for n in self.listed("--since", iso(2400))], ["Script"])
        self.assertEqual(self.listed("--since", iso(-60)), [])

    def test_since_must_be_an_iso_time(self):
        r = self.project.vmlab("ui", "notifications", "--since", "yesterday")
        self.assertExit(r, 2)
        self.assertIn("yesterday", r.err)

    def test_wait_for_is_met_by_one_posted_during_the_wait(self):
        self.later(1, lambda: self.post("Build done", "nonce 42"))
        r = self.project.vmlab("ui", "wait-for", "--notification", "nonce 42", "--app", "com.example.myapp", "--timeout", "10")
        self.assertExit(r, 0)
        waited = json.loads(r.out)
        self.assertEqual(waited["condition"], {"notification": "nonce 42", "app": "com.example.myapp"})
        self.assertEqual([n["body"] for n in waited["notifications"]], ["nonce 42"])

    def test_wait_for_counts_only_those_posted_after_its_start(self):
        started = time.time()
        r = self.project.vmlab("ui", "wait-for", "--notification", "earlier run", "--timeout", "1")
        self.assertExit(r, 1)
        self.assertFalse(json.loads(r.out)["met"])
        self.assertLess(time.time() - started, 8)
        r = self.project.vmlab("ui", "wait-for", "--notification", "earlier run", "--since", iso(7200), "--timeout", "1")
        self.assertExit(r, 0)

    def test_wait_for_a_notification_cannot_be_gone(self):
        r = self.project.vmlab("ui", "wait-for", "--notification", "x", "--gone")
        self.assertExit(r, 2)
        self.assertIn("--gone", r.err)

    def test_since_goes_with_a_notification_only(self):
        r = self.project.vmlab("ui", "wait-for", "--file", "~/x", "--since", iso())
        self.assertExit(r, 2)
        self.assertIn("--since", r.err)

    def test_scenario_api_defaults_to_the_runs_start(self):
        self.project.scenario("notified.py", """
            import json, pathlib, threading, time
            from datetime import datetime, timezone
            def post():
                path = pathlib.Path(%r)
                now = datetime.now(timezone.utc).isoformat()
                part = path.with_suffix(".part")
                part.write_text(json.dumps(json.loads(path.read_text()) + [{"app": "com.example.myapp", "title": "Done", "body": "nonce 7", "time": now}]))
                part.replace(path)
            def scenario(g):
                g.check("none from earlier Runs", g.notifications() == {"notifications": []})
                threading.Timer(1, post).start()
                waited = g.wait_for(notification="nonce 7", timeout=10)
                g.check("met once posted", waited["met"], detail=waited)
                g.check("listed", [n["body"] for n in g.notifications()["notifications"]] == ["nonce 7"])
                g.check("all", len(g.notifications(since="all")["notifications"]) == 3)
                g.check("one app", [n["title"] for n in g.notifications(app="com.example.myapp", since="all")["notifications"]] == ["Build done", "Done"])
                g.check("text", [n["title"] for n in g.notifications(text="Script", since="all")["notifications"]] == ["Script"])
                g.check("unmet", g.wait_for(notification="never sent", timeout=0.5) ["met"] is False)
        """ % str(self.notifications))
        r = self.project.vmlab("run")
        self.assertExit(r, 0)

    def test_scenario_api_app_defaults_to_the_labs_notification_id(self):
        self.configure('[labs.mac.app]\nnotification_id = "com.apple.ScriptEditor2"\n')
        self.project.scenario("notified.py", """
            def scenario(g):
                g.check("only the app's", [n["title"] for n in g.notifications(since="all")["notifications"]] == ["Script"])
                g.check("wait too", g.wait_for(notification="Build", since="all", timeout=0.5)["met"] is False)
        """)
        self.assertExit(self.project.vmlab("run"), 0)

    def test_scenario_api_refuses_gone_with_a_notification(self):
        self.project.scenario("gone.py", """
            def scenario(g):
                g.wait_for(notification="x", gone=True, timeout=1)
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertIn("gone", self.project.report()["scenarios"][0]["error"])

    def test_without_the_option_there_are_none(self):
        self.project.config(FAKE_LAB)
        self.assertEqual(self.listed(), [])


def move(x, y):
    return {"do": "move", "x": x, "y": y}


class PointerTest(UiTestCase):
    """pointer, drag and click's button and hold: the Guest's helper gets the events, in order."""

    def calls(self, command):
        log = next(self.project.home.glob("fake/*/commands.jsonl")).read_text()
        return [e["params"] for e in map(json.loads, log.splitlines()) if e["event"] == "ui" and e["command"] == command]

    def test_down_holds_the_modifiers_and_up_releases_them(self):
        self.assertEqual(self.ui("pointer", "down", "10", "20", "--button", "right", "--hold", "Shift+cmd"),
                         {"action": "down", "x": 10, "y": 20, "button": "right", "hold": "shift+cmd"})
        self.ui("pointer", "move", "30", "40", "--button", "right")
        self.ui("pointer", "up", "30", "40", "--button", "right", "--hold", "shift+cmd")
        down, moved, up = self.calls("pointer")
        self.assertEqual(down, {"button": "right", "events": [
            {"do": "keydown", "key": "shift"}, {"do": "keydown", "key": "cmd"}, move(10, 20), dict(move(10, 20), do="down")]})
        self.assertEqual(moved["events"], [move(30, 40)])
        self.assertEqual(up["events"], [move(30, 40), dict(move(30, 40), do="up"), {"do": "keyup", "key": "cmd"}, {"do": "keyup", "key": "shift"}])

    def test_a_move_holds_its_modifiers_for_that_move_only(self):
        self.ui("pointer", "move", "1", "2", "--hold", "alt")
        self.assertEqual(self.calls("pointer")[0]["events"], [{"do": "keydown", "key": "alt"}, move(1, 2), {"do": "keyup", "key": "alt"}])

    def test_drag_between_elements_presses_pauses_moves_evenly_and_releases(self):
        dragged = self.ui("drag", "--from-text", "Run again", "--to-text", "Run", "--app", "MyApp", "--steps", "3", "--duration", "0.3")
        self.assertEqual((dragged["from"]["x"], dragged["from"]["y"], dragged["from"]["element"]["name"]), (160, 360, "Run again"))
        self.assertEqual((dragged["to"]["x"], dragged["to"]["y"], dragged["to"]["element"]["name"]), (250, 360, "Run"))
        [call] = self.calls("pointer")
        self.assertEqual(call["button"], "left")
        wait, pause = {"do": "wait", "s": 0.1}, {"do": "wait", "s": 0.1}
        self.assertEqual(call["events"], [
            move(160, 360), dict(move(160, 360), do="down"), pause,
            move(190, 360), wait, move(220, 360), wait, move(250, 360), wait,
            pause, dict(move(250, 360), do="up"),
        ])  # fmt: skip

    def test_drag_between_points_with_a_button_and_hold(self):
        dragged = self.ui("drag", "--from-at", "0", "0", "--to-at", "10", "-10", "--steps", "2", "--button", "middle", "--hold", "alt")
        self.assertEqual((dragged["from"], dragged["to"]), ({"x": 0, "y": 0, "element": None, "index": None}, {"x": 10, "y": -10, "element": None, "index": None}))
        events = self.calls("pointer")[0]["events"]
        self.assertEqual((events[0], events[-1]), ({"do": "keydown", "key": "alt"}, {"do": "keyup", "key": "alt"}))
        self.assertEqual([e for e in events if e["do"] == "move"], [move(0, 0), move(5, -5), move(10, -10)])

    def test_drag_needs_both_ends(self):
        self.assertIn("--to", self.ui("drag", "--from-at", "0", "0", code=2).err)

    def test_drag_from_nothing_fails_naming_what_was_sought(self):
        self.assertIn("no element to drag matches text='Nope'", self.ui("drag", "--from-text", "Nope", "--to-at", "1", "1", code=1).err)

    def test_a_click_is_pointer_events_expecting_the_element_under_its_press(self):
        self.ui("click", "--text", "Run", "--app", "MyApp", "--button", "back", "--hold", "cmd+alt")
        self.ui("click", "--at", "5", "7")
        named, plain = self.calls("pointer")
        self.assertEqual(named, {"button": "back", "expect": {"label": "Run", "bounds": {"x": 220, "y": 350, "w": 60, "h": 20}}, "events": [
            {"do": "keydown", "key": "alt"}, {"do": "keydown", "key": "cmd"},
            move(250, 360), dict(move(250, 360), do="down"), dict(move(250, 360), do="up"),
            {"do": "keyup", "key": "cmd"}, {"do": "keyup", "key": "alt"},
        ]})  # fmt: skip
        self.assertEqual(plain, {"button": "left", "events": [move(5, 7), dict(move(5, 7), do="down"), dict(move(5, 7), do="up")]})
        self.assertEqual(self.calls("click"), [])

    def test_drag_from_a_covered_element_fails_at_once_naming_what_covers_it(self):
        self.write_tree(covered_by="loginwindow")
        r = self.ui("drag", "--from-text", "Run", "--from-role", "button", "--app", "MyApp", "--to-at", "1", "1", code=1)
        self.assertIn("loginwindow", r.err)

    def test_drag_with_a_timeout_waits_for_its_element_to_be_uncovered(self):
        self.write_tree(covered_by="loginwindow")
        self.later(1, self.write_tree)
        dragged = self.ui("drag", "--from-text", "Run", "--from-role", "button", "--app", "MyApp", "--to-at", "1", "1", "--timeout", "10")
        self.assertEqual(dragged["from"]["element"]["name"], "Run")
        self.assertEqual(self.calls("pointer")[-1]["expect"]["label"], "Run")

    def test_drag_from_a_point_expects_nothing(self):
        self.ui("drag", "--from-at", "0", "0", "--to-at", "1", "1")
        self.assertNotIn("expect", self.calls("pointer")[0])

    def test_unknown_buttons_and_keys_in_hold_are_refused(self):
        self.assertIn("buttons: left, right, middle, back, forward", self.ui("pointer", "down", "1", "1", "--button", "wheel", code=2).err)
        self.assertIn("modifiers", self.ui("click", "--at", "1", "1", "--hold", "shift+a", code=2).err)
        self.assertEqual(self.calls("pointer") + self.calls("click"), [])

    def test_a_scenario_drags_and_what_it_leaves_held_is_released_at_the_end_of_its_run(self):
        self.project.scenario("held.py", """
            def scenario(g):
                g.drag({"text": "Run again", "app": "MyApp"}, (300, 400), button="right", steps=1, duration=0)
                g.pointer("down", 10, 20, hold="shift")
                g.pointer("move", 30, 40, hold="shift+alt")
                g.check("fails", False)
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        dragged, down, moved, released = self.calls("pointer")
        self.assertEqual((dragged["button"], dragged["events"][-1]), ("right", dict(move(300, 400), do="up")))
        self.assertEqual(down["events"][0], {"do": "keydown", "key": "shift"})
        # shift is held already: only alt is pressed and released around the move
        self.assertEqual(moved["events"], [{"do": "keydown", "key": "alt"}, move(30, 40), {"do": "keyup", "key": "alt"}])
        self.assertEqual(released, {"button": "left", "events": [move(30, 40), dict(move(30, 40), do="up"), {"do": "keyup", "key": "shift"}]})

    def test_a_scenario_drags_an_element_find_found(self):
        self.project.scenario("found.py", """
            def scenario(g):
                run = g.find(text="Run", role="button", app="MyApp")["matches"][0]
                dragged = g.drag(run, (1, 1))
                g.check("from its middle", (dragged["from"]["x"], dragged["from"]["y"], dragged["from"]["element"]) == (250, 360, run))
                hidden = g.find(text="Run", app="Other")["matches"][0]
                try:
                    g.drag(hidden, (1, 1))
                except Exception as exc:
                    g.check("one without bounds is refused", "no bounds" in str(exc), detail=str(exc))
        """)
        self.assertExit(self.project.vmlab("run"), 0)
        self.assertEqual(self.calls("pointer")[0]["expect"], {"label": "Run", "bounds": {"x": 220, "y": 350, "w": 60, "h": 20}})

    def test_a_scenario_points_at_fractional_desktop_pixels_and_the_guest_gets_whole_ones(self):
        self.project.scenario("scaled.py", """
            def scenario(g):
                dragged = g.drag((280.0, 382.5), [281, 383.4], steps=1, duration=0)
                g.check("ends rounded", (dragged["from"], dragged["to"]) == ({"x": 280, "y": 382, "element": None, "index": None}, {"x": 281, "y": 383, "element": None, "index": None}))
                g.click(at=(280.6, 383.0))
                g.pointer("move", 1.5, 2.5)
                for end in ((True, 1), (1, 2, 3), (1, "2")):
                    try:
                        g.drag(end, (1, 1))
                        g.check("refused %r" % (end,), False)
                    except Exception as exc:
                        g.check("refused %r" % (end,), "a drag's ends" in str(exc), detail=str(exc))
        """)
        self.assertExit(self.project.vmlab("run"), 0)
        dragged, clicked, moved = self.calls("pointer")
        self.assertEqual([e for e in dragged["events"] if "x" in e], [move(280, 382), dict(move(280, 382), do="down"), move(281, 383), dict(move(281, 383), do="up")])
        self.assertEqual(clicked["events"][0], move(281, 383))
        self.assertEqual(moved["events"], [move(2, 2)])

    def test_every_held_button_is_released_and_the_modifiers_with_the_last(self):
        self.project.scenario("two.py", """
            def scenario(g):
                g.pointer("down", 10, 20, button="right", hold="shift")
                g.pointer("down", 10, 20)
                g.check("held", True)
        """)
        self.assertExit(self.project.vmlab("run"), 0)
        released = [c for c in self.calls("pointer") if c["events"][-1]["do"] in ("up", "keyup")]
        self.assertEqual([c["button"] for c in released], ["right", "left"])
        self.assertEqual(released[-1]["events"][-1], {"do": "keyup", "key": "shift"})

    def test_an_up_in_a_scenario_releases_what_its_down_held(self):
        self.project.scenario("up.py", """
            def scenario(g):
                g.pointer("down", 10, 20, hold="ctrl")
                g.pointer("up", 10, 20)
                g.check("released", True)
        """)
        self.assertExit(self.project.vmlab("run"), 0)
        down, up = self.calls("pointer")
        self.assertEqual(up["events"][-1], {"do": "keyup", "key": "ctrl"})


class LookalikeTest(UiTestCase):
    """Two Add buttons, one in each of two dialogs of MyApp: without an index, the first in view is pressed."""

    calls = PointerTest.calls

    def write_dialogs(self, *covered):
        dialogs = [
            {"role": "window", "name": "Add Shortcut", "bounds": {"x": 100 * i, "y": 100, "w": 300, "h": 200},
             "children": [dict({"role": "button", "name": "Add", "bounds": {"x": 100 * i + 10, "y": 250, "w": 60, "h": 20}},
                               **({"covered_by": covered[i - 1]} if covered[i - 1] else {}))]}
            for i in (1, 2)
        ]  # fmt: skip
        tree = {"role": "desktop", "children": [{"role": "application", "name": "MyApp", "children": dialogs}]}
        write_whole(self.project.dir / "tree.json", json.dumps(tree))

    def test_a_click_passes_over_a_covered_match_and_clicks_the_next(self):
        self.write_dialogs("Add Shortcut", None)
        clicked = self.ui("click", "--text", "Add")
        self.assertEqual((clicked["x"], clicked["index"]), (240, 1))
        self.assertEqual([c["expect"]["bounds"]["x"] for c in self.calls("pointer")], [110, 210])

    def test_a_click_with_every_match_passed_over_names_each_reason(self):
        self.write_dialogs("Add Shortcut", "Text Editor")
        r = self.ui("click", "--text", "Add", code=1)
        self.assertIn("none of the 2 elements matching text='Add' could be pressed", r.err)
        self.assertIn('[0] something else is at (140, 260): Add Shortcut; [1]', r.err)
        self.assertIn('[1] something else is at (240, 260): Text Editor\n', r.err)

    def test_a_click_with_an_index_takes_that_match_even_covered(self):
        self.write_dialogs("Add Shortcut", None)
        self.assertIn("Add Shortcut", self.ui("click", "--text", "Add", "--index", "0", code=1).err)
        self.assertEqual(len(self.calls("pointer")), 1)
        self.assertEqual(self.ui("click", "--text", "Add", "--index", "1")["index"], 1)

    def test_a_drag_passes_over_a_covered_source_but_its_target_is_the_first_match(self):
        self.write_dialogs("Add Shortcut", None)
        dragged = self.ui("drag", "--from-text", "Add", "--to-text", "Add")
        self.assertEqual((dragged["from"]["index"], dragged["to"]["index"], dragged["to"]["x"]), (1, 0, 140))

    def test_a_scenarios_trace_names_the_match_it_clicked(self):
        self.write_dialogs("Add Shortcut", None)
        self.project.scenario("lookalike.py", """
            def scenario(g):
                g.click(text="Add")
                g.drag({"text": "Add"}, (1, 1))
                g.click(at=(5, 5))
                g.check("fails", False)
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        calls = self.project.report()["scenarios"][0]["calls"]
        self.assertEqual([c.get("index") for c in calls], [1, 1, None, None])


class StagedCleanupTest(UiTestCase):
    """What a Scenario stages and leaves open is closed at the end of its Run, however the Run ends."""

    def run_leaving_it_open(self, ending):
        self.project.scenario("stage.py", """
            def scenario(g):
                kept = g.stage_text("kept")
                g.close_staged(g.stage_text("closed"))
                g.check("staged", kept["selected"] == "kept")
                %s
        """ % ending)
        r = self.project.vmlab("run")
        return r, self.project.report()["scenarios"][0]

    def assertClosed(self, scenario):
        kept, closed = scenario["staged"]
        self.assertEqual(closed["ended"], "scenario", closed)
        self.assertEqual(kept["ended"], "run", kept)
        self.assertEqual(self.ui("find", "--role", "textarea")["matches"], [])
        self.assertEqual(self.staged_files(), [], "a closed Staged document's file stays in the Guest")
        self.assertIn("%s: closed at the end of the Run" % kept["file"], (self.project.only_run_dir() / "summary.md").read_text())

    def test_a_passing_scenario(self):
        r, scenario = self.run_leaving_it_open("pass")
        self.assertEqual(scenario["status"], "passed", scenario)
        self.assertClosed(scenario)

    def test_a_failing_scenario(self):
        r, scenario = self.run_leaving_it_open('g.check("fails", False)')
        self.assertEqual(scenario["status"], "failed", scenario)
        self.assertClosed(scenario)

    def test_an_erroring_scenario(self):
        r, scenario = self.run_leaving_it_open('raise RuntimeError("boom")')
        self.assertEqual(scenario["status"], "error", scenario)
        self.assertClosed(scenario)

    def test_one_the_app_closed_is_noted_as_gone(self):
        r, scenario = self.run_leaving_it_open('g.exec(["sh", "-c", \'rm "$VMLAB_HOME"/fake/*/fs/ui-state.json\'])')
        self.assertEqual(scenario["staged"][0]["ended"], "gone", scenario)

    def test_one_vmlab_cannot_close_is_a_warning_and_the_result_stands(self):
        # The Scenario takes the Fake Guest down under vmlab, as a Guest that stopped answering.
        r, scenario = self.run_leaving_it_open('g.exec(["sh", "-c", \'rm "$VMLAB_HOME"/fake/*/running\'])')
        self.assertEqual(scenario["status"], "passed", scenario)
        self.assertExit(r, 0)
        kept = scenario["staged"][0]
        self.assertEqual(kept["ended"], "failed", kept)
        self.assertIn("not running", kept["close_error"])
        self.assertIn("warning: mac/stage: Staged document %s is still open" % kept["file"], r.out)
        self.assertEqual(self.staged_files(), [os.path.basename(kept["file"])])  # it is still open: its file stays
        self.assertIn("still open", (self.project.only_run_dir() / "summary.md").read_text())


    def test_one_vmlab_cannot_close_is_warned_about_when_the_run_ends_with_no_report(self):
        # A path outside the Guest is a ConfigError under the Fake Provider: the Run ends with no report.
        self.project.scenario("stage.py", """
            def scenario(g):
                staged = g.stage_text("kept")
                g.exec(["sh", "-c", 'rm "$VMLAB_HOME"/fake/*/running'])
                g.put("/../../outside", "x")
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn("leaves the Guest", r.err)
        self.assertRegex(r.out, r"warning: mac/stage: Staged document /tmp/vmlab-stage-[0-9a-f]{8}\.txt is still open")

    def test_close_staged_needs_a_stage_text_result_or_its_file(self):
        self.project.scenario("stage.py", """
            def scenario(g):
                g.close_staged({"app": "TextEdit"})
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertIn("close_staged takes a stage_text result", self.project.report()["scenarios"][0]["error"])


class LinuxProcessWaitTest(UiTestCase):
    def setUp(self):
        VmlabTestCase.setUp(self)
        self.project.config('[labs.lin]\nprovider = "fake"\nos = "linux"\n')
        self.assertExit(self.project.vmlab("up"), 0)

    guest_home = UiCliTest.guest_home
    assert_process_waits = UiCliTest.assert_process_waits

    def test_a_name_longer_than_linux_keeps_is_matched_in_full(self):
        # Linux keeps 15 bytes of a process's name: the probe matches those, then the full name.
        self.assert_process_waits("vmlab-a-long-process-(%s)" % uuid.uuid4().hex[:4])


class MuteChannelWaitTest(VmlabTestCase):
    def test_a_call_that_comes_back_without_an_answer_is_never_met(self):
        # A Channel may fail without saying so, as ssh exiting 255 with no message of its own.
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh"]\nmute_channels = ["ssh"]\n')
        self.assertExit(self.project.vmlab("up"), 0)
        for condition in (["--process", "no-such-process-vmlab"], ["--file", "~/nope"], ["--log", "~/nope", "--pattern", "x"], ["--exec", "false"]):
            for gone in ([], ["--gone"]):
                r = self.project.vmlab("ui", "wait-for", "--timeout", "1", *(gone + condition))
                self.assertExit(r, 1)
                waited = json.loads(r.out)
                self.assertFalse(waited["met"], (condition, gone))
                self.assertIn("no answer", waited["error"])


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


SETTINGS = {
    "role": "desktop",
    "children": [
        {
            "role": "application",
            "name": "MyApp",
            "children": [
                {
                    "role": "window",
                    "name": "Settings",
                    "bounds": {"x": 0, "y": 0, "w": 400, "h": 300},
                    "children": [
                        {"role": "checkbox", "name": "Pictures", "checked": True, "bounds": {"x": 10, "y": 10, "w": 100, "h": 20}},
                        {"role": "checkbox", "name": "Music", "checked": False, "bounds": {"x": 10, "y": 40, "w": 100, "h": 20}},
                        {"role": "checkbox", "name": "Some", "checked": "mixed", "bounds": {"x": 10, "y": 70, "w": 100, "h": 20}},
                        {"role": "radiobutton", "name": "Light", "checked": True, "bounds": {"x": 10, "y": 100, "w": 100, "h": 20}},
                        {"role": "radiobutton", "name": "Dark", "checked": False, "bounds": {"x": 10, "y": 130, "w": 100, "h": 20}},
                        {"role": "button", "name": "Save", "bounds": {"x": 10, "y": 160, "w": 100, "h": 20}},
                    ],
                }
            ],
        }
    ],
}


class CheckedTest(UiTestCase):
    """A node's tick state ("checked") and the queries that match on it."""

    def setUp(self):
        super().setUp()
        self.write_settings()

    def write_settings(self, music=False):
        tree = json.loads(json.dumps(SETTINGS))
        tree["children"][0]["children"][0]["children"][1]["checked"] = music
        write_whole(self.project.dir / "tree.json", json.dumps(tree))

    def names(self, *args):
        return [m["name"] for m in self.ui("find", *args)["matches"]]

    def test_every_node_says_its_tick_state(self):
        states = {m["name"]: m["checked"] for m in self.ui("find", "--app", "MyApp", "--text", "")["matches"]}
        self.assertEqual(
            states, {"MyApp": None, "Settings": None, "Pictures": True, "Music": False, "Some": "mixed", "Light": True, "Dark": False, "Save": None}
        )

    def test_find_matches_the_tick_state_exactly(self):
        self.assertEqual(self.names("--role", "checkbox", "--checked"), ["Pictures"])
        self.assertEqual(self.names("--role", "checkbox", "--unchecked"), ["Music"])
        self.assertEqual(self.names("--role", "checkbox", "--mixed"), ["Some"])
        self.assertEqual(self.names("--role", "radiobutton", "--unchecked"), ["Dark"])
        self.assertEqual(self.names("--text", "Save", "--unchecked"), [])

    def test_the_tick_flags_exclude_each_other(self):
        r = self.ui("find", "--role", "checkbox", "--checked", "--unchecked", code=2)
        self.assertIn("not allowed with argument", r.err)

    def test_a_tick_state_alone_is_not_a_query(self):
        r = self.ui("find", "--checked", code=2)
        self.assertIn("needs --text or --role", r.err)

    def test_a_tick_state_goes_with_an_element_in_wait_for(self):
        r = self.ui("wait-for", "--process", "MyApp", "--checked", code=2)
        self.assertIn("--checked goes with --text or --role", r.err)

    def test_click_takes_the_tick_state(self):
        clicked = self.ui("click", "--role", "checkbox", "--unchecked")
        self.assertEqual((clicked["element"]["name"], clicked["y"]), ("Music", 50))

    def test_wait_for_a_box_to_be_ticked(self):
        r = self.ui("wait-for", "--text", "Music", "--role", "checkbox", "--checked", "--timeout", "0.5", code=1)
        self.assertEqual(json.loads(r.out)["condition"], {"text": "Music", "role": "checkbox", "checked": True})
        self.later(1, lambda: self.write_settings(music=True))
        waited = self.ui("wait-for", "--text", "Music", "--role", "checkbox", "--checked", "--timeout", "10")
        self.assertEqual([m["name"] for m in waited["matches"]], ["Music"])

    def test_scenario_api(self):
        self.later(1, lambda: self.write_settings(music=True))
        self.project.scenario("ticks.py", """
            def scenario(g):
                g.check("find", [m["name"] for m in g.find(role="checkbox", checked=False)["matches"]] == ["Music"])
                g.check("click", g.click(role="checkbox", checked="mixed")["element"]["name"] == "Some")
                waited = g.wait_for(text="Music", role="checkbox", checked=True, timeout=10)
                g.check("ticked", waited["met"], detail=waited)
                try:
                    g.click(text="Dark", checked=True)
                    g.check("named", False)
                except Exception as exc:
                    g.check("named", "checked=True" in str(exc), detail=str(exc))
                try:
                    g.find(role="checkbox", checked="yes")
                    g.check("bad state", False)
                except Exception as exc:
                    g.check("bad state", "checked" in str(exc), detail=str(exc))
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)


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
                g.check("displays", g.displays()["displays"][0]["primary"] is True)
        """)
        r = self.project.vmlab("run")
        self.assertExit(r, 0)

    def test_app_may_be_given_by_its_bundle_id(self):
        self.project.scenario("bundle.py", """
            def scenario(g):
                [match] = g.find(text="Run", app="com.example.myapp")["matches"]
                g.check("find", match["app"] == "MyApp")
                clicked = g.click(text="Run", app="com.example.myapp")
                g.check("click", (clicked["x"], clicked["element"]["app"]) == (250, "MyApp"))
                g.check("wait", g.wait_for(text="Run", app="com.example.myapp", timeout=5)["met"])
        """)
        self.assertExit(self.project.vmlab("run"), 0)

    def test_close_staged_takes_a_stage_text_result_or_its_file(self):
        self.project.scenario("close.py", """
            def scenario(g):
                first, second = g.stage_text("first"), g.stage_text("second")
                g.check("by result", g.close_staged(first) == {"file": first["file"], "closed": True})
                g.check("by file", g.close_staged(second["file"]) == {"file": second["file"], "closed": True})
                g.check("gone", g.close_staged(second)["closed"] is False)
                g.check("no editor", not g.find(role="textarea")["matches"])
        """)
        self.assertExit(self.project.vmlab("run"), 0)
        [scenario] = self.project.report()["scenarios"]
        self.assertEqual([d["ended"] for d in scenario["staged"]], ["scenario", "scenario"])

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

    def test_click_with_a_timeout_in_a_scenario(self):
        self.write_tree(covered_by="loginwindow")
        self.project.scenario("click.py", """
            import json, pathlib, threading
            def scenario(g):
                tree = pathlib.Path(%r)
                uncovered = json.loads(tree.read_text())
                del uncovered["children"][0]["children"][0]["children"][1]["covered_by"]
                part = tree.with_suffix(".part")
                part.write_text(json.dumps(uncovered))
                threading.Timer(1, lambda: part.replace(tree)).start()
                g.check("clicked once uncovered", g.click(text="Run", app="MyApp", timeout=10)["x"] == 250)
        """ % str(self.project.dir / "tree.json"))
        self.assertExit(self.project.vmlab("run"), 0)

    def test_a_click_timeout_is_bounded_by_the_scenario_timeout(self):
        self.project.scenario("slow.py", """
            TIMEOUT = 1
            def scenario(g):
                g.click(text="Nope", timeout=30)
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

    def test_app_without_an_element_errors_the_scenario(self):
        self.project.scenario("app.py", """
            def scenario(g):
                g.wait_for(process="MyApp", app="MyApp", timeout=1)
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertIn("app goes with text or role", self.project.report()["scenarios"][0]["error"])

    def test_a_missing_command_errors_the_scenario_naming_it(self):
        self.project.scenario("typo.py", """
            def scenario(g):
                g.wait_for(exec=["no-such-command-vmlab"], timeout=30)
        """)
        self.assertExit(self.project.vmlab("run"), 1)
        self.assertIn("no-such-command-vmlab", self.project.report()["scenarios"][0]["error"])
