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

    def test_a_hung_preferred_channel_fails_because_a_timeout_never_falls_back(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\nhung_channels = ["ssh"]\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor", timeout=90)

        self.assertExit(r, 1)
        self.assertRegex(r.out, r"FAIL\s+mac: Channel ssh: .*hangs")
        self.assertIn("channels", r.out)  # the fix: drop it or put the other one first
        self.assertRegex(r.out, r"ok\s+mac: Channel exec")

    def test_a_hung_fallback_channel_is_only_a_warning(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\nhung_channels = ["exec"]\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor", timeout=90)

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"warn\s+mac: Channel exec: .*hangs")

    def test_a_running_guest_gets_a_screenshot_check(self):
        self.project.config(FAKE_LAB)
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor")

        self.assertRegex(r.out, r"ok\s+mac: Screenshot")

    def test_a_guest_still_booting_fails_and_still_shows_its_channels(self):
        self.project.config(FAKE_LAB + "boot_timeout = 1\n[labs.mac.fake]\nboot_seconds = 600\n")
        self.project.vmlab("up")  # times out: the Guest is running but not reachable

        r = self.project.vmlab("doctor")

        self.assertExit(r, 1)
        self.assertRegex(r.out, r"FAIL\s+mac: Guest: running but not reachable")
        self.assertRegex(r.out, r"warn\s+mac: Channel ssh: .*booting")

    def test_the_host_section_names_the_host_and_its_hypervisors(self):
        self.project.config(FAKE_LAB)

        r = self.project.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"info\s+Host: Host: ")
        self.assertRegex(r.out, r"Host: Hypervisors: .*Tart")  # found or not, each is named
        self.assertRegex(r.out, r"ok\s+Host: vmlab home")

    def test_an_ssh_key_others_can_read_is_a_warning_with_the_chmod(self):
        self.project.config(FAKE_LAB)
        key = self.project.home / "ssh" / "id_ed25519"
        key.parent.mkdir(parents=True)
        key.write_text("secret")
        key.chmod(0o644)

        r = self.project.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+Host: SSH key")
        self.assertIn("chmod 600 %s" % key, r.out)

    def test_credentials_others_can_read_are_a_warning_with_the_chmod(self):
        self.project.config(FAKE_LAB)
        creds = self.project.home / "fusion" / "vmlab-base-ubuntu-26.04.credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text("{}")
        creds.chmod(0o644)

        r = self.project.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+Host: Guest credentials")
        self.assertIn("chmod 600 %s" % creds, r.out)

    def test_a_corrupt_base_guest_registry_fails(self):
        self.project.config(FAKE_LAB)
        self.project.home.mkdir(exist_ok=True)
        (self.project.home / "bases.json").write_text("{not json")

        r = self.project.vmlab("doctor")

        self.assertExit(r, 1)
        self.assertRegex(r.out, r"FAIL\s+Host: Base guest registry")

    def test_a_stopped_lab_that_needs_more_memory_than_is_free_is_a_warning(self):
        self.project.config(FAKE_LAB + "memory_gb = 16\n")

        r = self.project.vmlab("doctor", env={"VMLAB_FREE_MEMORY_GB": "4"})

        self.assertRegex(r.out, r"warn\s+mac: Memory: needs 16 GB, 4 GB free")

    def test_a_quit_recipe_with_no_process_to_wait_for_is_a_warning_naming_the_key(self):
        # ready waits for a Tray icon, not a process: nothing says when the app has gone
        self.project.config(FAKE_LAB + '[labs.mac.app]\nquit = "pkill -x MyApp"\nlaunch = "true"\nready = { tray = "MyApp" }\n')

        r = self.project.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+mac: App quit: .*waits for nothing")
        self.assertIn('process = "', r.out)

    def test_a_quit_recipe_that_waits_for_the_apps_process_is_ok(self):
        for app in ('process = "MyApp"\n', 'launch = "true"\nready = { process = "MyApp" }\n'):
            self.project.config(FAKE_LAB + '[labs.mac.app]\nquit = "pkill -x MyApp"\n' + app)

            r = self.project.vmlab("doctor")

            self.assertRegex(r.out, r"ok\s+mac: App quit: waits for MyApp to go", app)

    def test_a_lab_without_a_quit_recipe_has_no_quit_check(self):
        self.project.config(FAKE_LAB)
        self.assertNotIn("App quit", self.project.vmlab("doctor").out)


class BenchTest(VmlabTestCase):
    def test_bench_measures_every_channel_of_a_running_guest(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor", "--bench", "--calls", "3")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"info\s+mac: Bench ssh: median \d+ ms \(min \d+, max \d+; 3 calls\)")
        self.assertRegex(r.out, r"info\s+mac: Bench exec: median \d+ ms")

    def test_bench_json_carries_the_numbers(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor", "--bench", "--calls", "2", "--json")

        checks = {c["check"]: c for c in json.loads(r.out) if c["lab"] == "mac"}
        latency = checks["Bench ssh"]["latency_ms"]
        self.assertEqual(latency["calls"], 2)
        self.assertLessEqual(latency["min"], latency["median"])
        self.assertLessEqual(latency["median"], latency["max"])

    def test_bench_says_when_a_fallback_is_faster_than_the_preferred_channel(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\nlatency = {ssh = 0.2}\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor", "--bench", "--calls", "2")

        self.assertRegex(r.out, r"warn\s+mac: Channel order: exec is faster than ssh")
        self.assertIn('channels = ["exec", "ssh"]', r.out)

    def test_bench_confirms_the_preferred_channel_when_it_is_fastest(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\nlatency = {exec = 0.2}\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor", "--bench", "--calls", "2")

        self.assertRegex(r.out, r"ok\s+mac: Channel order: ssh, the preferred Channel, is the fastest")

    def test_bench_of_a_failing_channel_is_a_warning(self):
        self.project.config(FAKE_LAB + '[labs.mac.fake]\nchannels = ["ssh", "exec"]\nbroken_channels = ["ssh"]\n')
        self.assertExit(self.project.vmlab("up"), 0)

        r = self.project.vmlab("doctor", "--bench", "--calls", "2")

        self.assertRegex(r.out, r"warn\s+mac: Bench ssh: ")
        self.assertRegex(r.out, r"info\s+mac: Bench exec: median")

    def test_bench_of_a_stopped_guest_says_how_to_start_it(self):
        self.project.config(FAKE_LAB)

        r = self.project.vmlab("doctor", "--bench")

        self.assertExit(r, 0)
        self.assertIn("vmlab up mac", r.out)
        self.assertNotIn("Bench", r.out)

    def test_calls_without_bench_is_a_usage_error(self):
        self.project.config(FAKE_LAB)

        r = self.project.vmlab("doctor", "--calls", "3")

        self.assertExit(r, 2)
        self.assertIn("--bench", r.err)


class HostOnlyDoctorTest(VmlabTestCase):
    def test_outside_a_project_it_checks_the_host_and_says_how_to_start_one(self):
        self.project.dir.rmdir()

        r = self.project.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"info\s+Host: Host: ")
        self.assertRegex(r.out, r"info\s+Host: Hypervisors: .*Tart not found")
        self.assertRegex(r.out, r"info\s+Host: Project: no \.vmlab/vmlab\.toml")
        self.assertIn("vmlab init", r.out)

    def test_outside_a_project_naming_a_lab_is_a_config_error(self):
        self.project.dir.rmdir()

        r = self.project.vmlab("doctor", "mac")

        self.assertExit(r, 2)
        self.assertIn("no vmlab config found", r.err)

    def test_outside_a_project_bench_is_a_usage_error(self):
        self.project.dir.rmdir()

        r = self.project.vmlab("doctor", "--bench")

        self.assertExit(r, 2)
        self.assertIn("--bench", r.err)
