import json
import xml.etree.ElementTree as ET

from harness import VmlabTestCase

# (Host, Lab os, Lab arch) -> how the Lab's Build artifact is covered on that Host
COVERAGE = {
    ("arm64", "macos", "arm64"): "native",
    ("arm64", "macos", "x86_64"): "uncovered",
    ("arm64", "windows", "arm64"): "native",
    ("arm64", "windows", "x86_64"): "emulated",
    ("arm64", "linux", "arm64"): "native",
    ("arm64", "linux", "x86_64"): "uncovered",
    # the mirror, encoded though no x86_64 Host tests it for real
    ("x86_64", "macos", "arm64"): "uncovered",
    ("x86_64", "macos", "x86_64"): "native",
    ("x86_64", "windows", "arm64"): "uncovered",
    ("x86_64", "windows", "x86_64"): "native",
    ("x86_64", "linux", "arm64"): "uncovered",
    ("x86_64", "linux", "x86_64"): "native",
}


def lab(name, os, arch, extra=""):
    return '[labs.%s]\nprovider = "fake"\nos = "%s"\narch = "%s"\n%s\n' % (name, os, arch, extra)


class ArchTest(VmlabTestCase):
    def vmlab(self, *args, host="arm64"):
        return self.project.vmlab(*args, env={"VMLAB_HOST_ARCH": host})

    def test_doctor_reports_each_combination(self):
        for (host, os, arch), mode in COVERAGE.items():
            with self.subTest(host=host, os=os, arch=arch):
                self.project.config(lab("l", os, arch))

                r = self.vmlab("doctor", "--json", host=host)

                self.assertExit(r, 0)  # a coverage gap is a warning, never a failure
                [finding] = [f for f in json.loads(r.out) if f["check"] == "Architecture"]
                self.assertEqual(finding["status"], "warn" if mode == "uncovered" else "ok")
                if mode == "uncovered":  # nothing else is checked for a Lab that cannot run here
                    self.assertEqual([f["check"] for f in json.loads(r.out) if f["lab"] == "l"], ["Architecture"])
                self.assertIn(mode if mode != "uncovered" else "not covered", finding["detail"])
                if mode == "uncovered":
                    self.assertTrue(finding["fix"])

    def test_doctor_names_the_emulation(self):
        self.project.config(lab("win", "windows", "x86_64"))

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"ok\s+win: Architecture: .*x86_64.*emulat.*arm64 Guest")

    def test_an_uncovered_lab_is_skipped_loudly(self):
        self.project.config(
            lab("linux64", "linux", "x86_64", '[labs.linux64.app]\nartifact = "app.bin"\nbuild = "touch built && touch app.bin"')
            + lab("linux", "linux", "arm64")
        )
        self.project.scenario("hello.py", 'def scenario(g):\n    g.check("ran", g.exec(["true"]).ok)\n')

        r = self.vmlab("run")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"warning: linux64: .*x86_64 Linux.*not covered")
        self.assertRegex(r.out, r"SKIPPED linux64")
        self.assertRegex(r.out, r"PASSED linux")
        self.assertFalse((self.project.root / "app" / "built").exists(), "nothing built for a Lab that cannot run")
        status = {s["lab"]: s["running"] for s in json.loads(self.vmlab("status", "--json").out)}
        self.assertFalse(status["linux64"])
        [run_dir] = [d for d in self.project.run_dirs() if d.name.endswith("-linux64")]
        report = self.project.report(run_dir)
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["coverage"]["mode"], "uncovered")
        self.assertEqual(report["scenarios"], [])
        self.assertTrue(report["warnings"])
        summary = (run_dir / "summary.md").read_text()
        self.assertIn("SKIPPED", summary)
        self.assertIn("not covered", summary)
        suite = ET.parse(str(run_dir / "junit.xml")).getroot()
        self.assertEqual(suite.get("skipped"), "1")
        self.assertIn("not covered", suite.find("testcase/skipped").get("message"))

    def test_a_skipped_lab_takes_no_memory_in_parallel(self):
        self.project.config(lab("linux64", "linux", "x86_64", "memory_gb = 64") + lab("linux", "linux", "arm64", "memory_gb = 1"))
        self.project.scenario("hello.py", 'def scenario(g):\n    g.check("ran", True)\n')

        r = self.project.vmlab("run", "--parallel", env={"VMLAB_HOST_ARCH": "arm64", "VMLAB_FREE_MEMORY_GB": "2"})

        self.assertExit(r, 0)
        self.assertNotIn("queued", r.out)
        self.assertIn("SKIPPED linux64", r.out)
        self.assertIn("PASSED linux", r.out)

    def test_up_warns_about_an_uncovered_lab(self):
        self.project.config(lab("linux64", "linux", "x86_64"))

        r = self.vmlab("up")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"warning: linux64: .*not covered")

    def test_an_unknown_host_arch_override_is_a_usage_error(self):
        self.project.config(lab("mac", "macos", "arm64"))

        r = self.vmlab("doctor", host="amd64")

        self.assertExit(r, 2)
        self.assertIn("VMLAB_HOST_ARCH", r.err)

    def test_only_uncovered_labs_exit_zero_with_nothing_run(self):
        self.project.config(lab("linux64", "linux", "x86_64"))
        self.project.scenario("hello.py", 'def scenario(g):\n    g.check("ran", True)\n')

        r = self.vmlab("run")

        self.assertExit(r, 0)
        self.assertIn("SKIPPED linux64", r.out)

    def test_an_emulated_lab_runs_in_a_guest_of_the_host_arch(self):
        self.project.config(lab("win64", "windows", "x86_64"))
        self.project.scenario(
            "arch.py",
            'def scenario(g):\n    g.check("artifact arch", g.arch == "x86_64")\n    g.check("guest arch", g.guest_arch == "arm64")\n',
        )

        r = self.vmlab("run")

        self.assertExit(r, 0)
        self.assertNotIn("warning", r.out)
        report = self.project.report()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["coverage"]["mode"], "emulated")
        self.assertEqual(report["coverage"]["guest_arch"], "arm64")
        self.assertEqual(report["warnings"], [])
        self.assertIn("emulat", (self.project.only_run_dir() / "summary.md").read_text())

    def test_a_native_run_reports_native_coverage(self):
        self.project.config(lab("mac", "macos", "arm64"))
        self.project.scenario("hello.py", 'def scenario(g):\n    g.check("ran", g.guest_arch == g.arch)\n')

        r = self.vmlab("run")

        self.assertExit(r, 0)
        self.assertEqual(self.project.report()["coverage"], {"mode": "native", "arch": "arm64", "guest_arch": "arm64", "detail": "native arm64 Guest"})

    def test_deploy_skips_an_uncovered_lab_loudly(self):
        self.project.config(lab("linux64", "linux", "x86_64") + lab("linux", "linux", "arm64"))

        r = self.vmlab("deploy")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"warning: linux64: .*not covered.*skipped")
        self.assertIn("linux: deployed", r.out)
        self.assertNotIn("linux64: deployed", r.out)
