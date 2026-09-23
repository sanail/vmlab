"""The Regression workflow as the skill documents it (skill/references/regression.md).

A saved Scenario runs through the project's pinned copy with no agent, and the
measure practice proves a new Check red on a Build artifact without the fix,
then green with it: the build hook rebuilds whenever the source is newer.
"""

import os
import time
import xml.etree.ElementTree as ET

from harness import VmlabTestCase

# The "app" is one source file; its build copies it into the Build artifact and
# its install recipe puts that in the Guest, where the Scenario reads it.
APP = """
[labs.mac]
provider = "fake"
os = "macos"

[labs.mac.app]
artifact = "dist/app.txt"
build = "mkdir -p dist && cp src/app.txt dist/app.txt"
inputs = ["src"]
install = "cp \\"$VMLAB_ARTIFACT\\" ~/app.txt"
"""

SCENARIO = """
def scenario(g):
    app = g.exec(["sh", "-c", "cat ~/app.txt"]).stdout
    g.check("the palette closes after typing", "closes-after-typing" in app, detail=app)
"""


class RegressionTest(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.project.dir.rmdir()
        self.assertExit(self.project.vmlab("init"), 0)
        with self.project.config_path.open("a") as f:
            f.write(APP)
        self.src = self.project.dir.parent / "src" / "app.txt"
        self.src.parent.mkdir()
        self.project.scenario("palette_closes_after_typing.py", SCENARIO)

    def edit_source(self, text):
        """Write the source and age the Build artifact, so the edit is newer than it however fast the test runs."""
        artifact = self.project.dir.parent / "dist" / "app.txt"
        if artifact.exists():
            os.utime(str(artifact), (time.time() - 60,) * 2)
        self.src.write_text(text)

    def run_like_ci(self):
        """`python3 .vmlab/vmlab.pyz run` with nothing of the agent's environment; returns the exit code and new Run folder."""
        before = set(self.project.run_dirs())
        r = self.project.vmlab_vendored("run", bare=True)
        (run_dir,) = set(self.project.run_dirs()) - before
        return r.code, run_dir

    def failures(self, run_dir):
        return [c.get("name") for c in ET.parse(str(run_dir / "junit.xml")).iter("testcase") if c.find("failure") is not None]

    def test_measure_the_check_is_red_without_the_fix_and_green_with_it(self):
        self.edit_source("palette: stays-open")  # without the fix
        code, without_fix = self.run_like_ci()

        self.assertEqual(code, 1)
        self.assertEqual(self.failures(without_fix), ["the palette closes after typing"])
        self.assertTrue(self.project.report(without_fix)["deploy"]["built"])

        self.edit_source("palette: closes-after-typing")  # the fix put back
        code, with_fix = self.run_like_ci()

        self.assertEqual(code, 0)
        self.assertEqual(self.failures(with_fix), [])
        self.assertTrue(self.project.report(with_fix)["deploy"]["built"], "the second Run must test a fresh build")
