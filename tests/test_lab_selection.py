"""Every command that uses Labs takes `--lab LAB`; those whose only arguments are Labs (up, down,
doctor, status, deploy) also take them bare, and `exec LAB -- COMMAND` names its Lab before the --."""

import json

from harness import FAKE_LAB, VmlabTestCase

TWO_LABS = FAKE_LAB + '\n[labs.win]\nprovider = "fake"\nos = "windows"\narch = "arm64"\n'


class LabSelectionTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.project.config(TWO_LABS)

    def running(self):
        r = self.project.vmlab("status", "--json")
        self.assertExit(r, 0)
        return {row["lab"] for row in json.loads(r.out) if row["running"]}

    def test_up_and_down_take_lab_as_an_option_too(self):
        self.assertExit(self.project.vmlab("up", "--lab", "win"), 0)
        self.assertEqual(self.running(), {"win"})
        self.assertExit(self.project.vmlab("down", "--lab", "win"), 0)
        self.assertEqual(self.running(), set())

    def test_bare_labs_and_lab_options_add_up(self):
        self.assertExit(self.project.vmlab("up", "mac", "--lab", "win"), 0)
        self.assertEqual(self.running(), {"mac", "win"})

    def test_status_shows_the_labs_asked_for_either_way(self):
        for args in (["win"], ["--lab", "win"]):
            r = self.project.vmlab("status", *args)
            self.assertExit(r, 0)
            self.assertEqual([line.split()[0] for line in r.out.splitlines()], ["win"], args)

    def test_doctor_and_deploy_take_lab_as_an_option(self):
        r = self.project.vmlab("doctor", "--lab", "win")
        self.assertExit(r, 0)
        self.assertIn("win: ", r.out)
        self.assertNotIn("mac: ", r.out)
        r = self.project.vmlab("deploy", "--lab", "win")
        self.assertExit(r, 0)
        self.assertEqual(self.running(), {"win"})

    def test_exec_takes_its_lab_before_the_dashes(self):
        self.assertExit(self.project.vmlab("up", "win"), 0)
        r = self.project.vmlab("exec", "win", "--", "echo", "hi")
        self.assertExit(r, 0)
        self.assertEqual(r.out, "hi\n")

    def test_exec_takes_its_options_after_its_lab(self):
        self.assertExit(self.project.vmlab("up", "win"), 0)
        r = self.project.vmlab("exec", "win", "--timeout", "30", "--", "echo", "hi")
        self.assertExit(r, 0)
        self.assertEqual(r.out, "hi\n")

    def test_exec_takes_a_first_word_that_is_no_lab_as_the_command(self):
        self.project.config(FAKE_LAB)
        self.assertExit(self.project.vmlab("up"), 0)
        r = self.project.vmlab("exec", "echo", "--", "hi")
        self.assertExit(r, 0)
        self.assertEqual(r.out, "-- hi\n")

    def test_exec_refuses_two_different_labs(self):
        r = self.project.vmlab("exec", "--lab", "mac", "win", "--", "true")
        self.assertExit(r, 2)
        self.assertIn("mac", r.err)
        self.assertIn("win", r.err)

    def test_a_lab_named_twice_is_acted_on_once(self):
        r = self.project.vmlab("up", "win", "--lab", "win")
        self.assertExit(r, 0)
        self.assertEqual(r.out.count("win running"), 1, r.out)

    def test_an_unknown_lab_is_a_usage_error_either_way(self):
        for args in (["up", "--lab", "nope"], ["status", "nope"], ["exec", "--lab", "nope", "--", "true"]):
            r = self.project.vmlab(*args)
            self.assertExit(r, 2)
            self.assertIn("nope", r.err, args)
