import json

from harness import FAKE_LAB, VmlabTestCase


class DoctorTest(VmlabTestCase):
    def test_healthy_running_guest(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"ok\s+mac: Provider fake")
        self.assertRegex(r.out, r"ok\s+mac: Channel ssh")
        self.assertRegex(r.out, r"ok\s+mac: Channel exec")
        self.assertRegex(r.out, r"ok\s+mac: UI helper")

    def test_a_broken_preferred_channel_is_a_warning_with_a_fix(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\nbroken_channels = ["ssh"]\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"warn\s+mac: Channel ssh")
        self.assertIn("broken_channels", r.out)  # the fix
        self.assertRegex(r.out, r"ok\s+mac: Channel exec")

    def test_no_working_channel_fails(self):
        self.project.config(
            FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\nbroken_channels = ["ssh", "exec"]\n'
        )
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor")

        self.assertExit(r, 1)
        self.assertRegex(r.out, r"FAIL\s+mac: no Channel reaches the Guest")

    def test_a_stopped_guest_says_how_to_check_its_channels(self):
        self.project.config(FAKE_LAB)

        r = self.project.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertIn("vmlab up mac", r.out)

    def test_json_output(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\nbroken_channels = ["ssh"]\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor", "--json")

        self.assertExit(r, 0)
        checks = {c["check"]: c for c in json.loads(r.out) if c["lab"] == "mac"}
        self.assertEqual(checks["Channel ssh"]["status"], "warn")
        self.assertTrue(checks["Channel ssh"]["fix"])
        self.assertEqual(checks["Channel exec"]["status"], "ok")
