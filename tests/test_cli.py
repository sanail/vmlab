from harness import VmlabTestCase


class VersionTest(VmlabTestCase):
    def test_zipapp_builds_and_reports_its_version(self):
        r = self.project.vmlab("version")
        self.assertExit(r, 0)
        self.assertRegex(r.out.strip(), r"^vmlab \d+\.\d+\.\d+$")
