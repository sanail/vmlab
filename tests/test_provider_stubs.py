from harness import VmlabTestCase

PASS = 'def scenario(g):\n    g.check("ok", True)\n'


class ProviderStubTest(VmlabTestCase):
    def assertNotImplementedYet(self, provider):
        self.project.config('[labs.x]\nprovider = "%s"\nos = "linux"\n' % provider)
        self.project.scenario("ok.py", PASS)
        for command in (["run"], ["up"], ["doctor"]):
            r = self.project.vmlab(*command)
            self.assertExit(r, 2)
            self.assertIn("labs.x.provider", r.err)
            self.assertIn("not implemented yet", r.err)
            self.assertIn("docs/adding-a-provider.md", r.err)

    def test_utm_fails_clearly(self):
        self.assertNotImplementedYet("utm")

    def test_parallels_fails_clearly(self):
        self.assertNotImplementedYet("parallels")
