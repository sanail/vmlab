"""`vmlab exec`: one command in a running Guest, for exploring by hand (logs, files, processes)."""

from harness import FAKE_LAB, VmlabTestCase


class ExecTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.project.config(FAKE_LAB)

    def test_prints_the_commands_output_and_exits_with_its_code(self):
        self.assertExit(self.project.vmlab("up"), 0)
        r = self.project.vmlab("exec", "--", "sh", "-c", "echo out; echo err >&2; exit 3")
        self.assertExit(r, 3)
        self.assertEqual(r.out, "out\n")
        self.assertEqual(r.err, "err\n")

    def test_a_stopped_guest_is_an_error_naming_the_fix(self):
        r = self.project.vmlab("exec", "--", "true")
        self.assertExit(r, 1)
        self.assertIn("Guest mac is not running", r.err)
        self.assertIn("vmlab up mac", r.err)

    def test_several_labs_need_lab(self):
        self.project.config(FAKE_LAB + '\n[labs.win]\nprovider = "fake"\nos = "windows"\narch = "arm64"\n')
        r = self.project.vmlab("exec", "--", "true")
        self.assertExit(r, 2)
        self.assertIn("--lab", r.err)

    def test_a_command_over_its_timeout_fails(self):
        self.assertExit(self.project.vmlab("up"), 0)
        r = self.project.vmlab("exec", "--timeout", "1", "--", "sleep", "5")
        self.assertExit(r, 1)
        self.assertIn("vmlab: error:", r.err)

    def test_a_timeout_must_be_positive(self):
        self.assertExit(self.project.vmlab("up"), 0)
        r = self.project.vmlab("exec", "--timeout", "0", "--", "true")
        self.assertExit(r, 2)
        self.assertIn("--timeout", r.err)
