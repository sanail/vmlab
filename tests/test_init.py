import subprocess
import sys
import tempfile
import zipapp
from pathlib import Path

from harness import VmlabTestCase, zipapp_path

SMOKE = 'def scenario(g):\n    g.check("ok", True)\n'


def fake_zipapp(dest, version):
    """A stand-in vmlab zipapp that only knows `version`: an older or newer release."""
    with tempfile.TemporaryDirectory() as src:
        (Path(src) / "__main__.py").write_text(
            "import sys\nif sys.argv[1:] == ['version']:\n    print('vmlab %s')\n" % version
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(src, target=dest)
    return dest


def pyz_version(path):
    proc = subprocess.run([sys.executable, str(path), "version"], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


class InitTest(VmlabTestCase):
    def test_init_creates_the_layout_in_an_empty_project(self):
        r = self.project.vmlab("init")

        self.assertExit(r, 0)
        d = self.project.dir
        self.assertTrue((d / "scenarios").is_dir())
        self.assertIn("runs/", (d / ".gitignore").read_text().splitlines())
        self.assertEqual(pyz_version(d / "vmlab.pyz"), pyz_version(zipapp_path()))
        config = self.project.config_path.read_text()
        self.assertTrue(all(l.startswith("#") or not l.strip() for l in config.splitlines()), config)
        self.assertIn("[labs.", config)

    def test_template_config_is_valid_toml_that_asks_for_a_lab(self):
        self.assertExit(self.project.vmlab("init"), 0)
        r = self.project.vmlab("run")
        self.assertExit(r, 2)
        self.assertIn("no Labs declared", r.err)

    def test_the_vendored_copy_runs_scenarios_on_its_own(self):
        self.assertExit(self.project.vmlab("init"), 0)
        with self.project.config_path.open("a") as f:
            f.write('\n[labs.mac]\nprovider = "fake"\nos = "macos"\n')
        self.project.scenario("smoke.py", SMOKE)

        r = self.project.vmlab_vendored("run")

        self.assertExit(r, 0)

    def test_init_is_idempotent_and_keeps_the_users_config(self):
        self.assertExit(self.project.vmlab("init"), 0)
        edited = self.project.config_path.read_text() + '\n# my note\n[labs.mac]\nprovider = "fake"\nos = "macos"\n'
        self.project.config_path.write_text(edited)
        self.project.scenario("smoke.py", SMOKE)

        r = self.project.vmlab("init")

        self.assertExit(r, 0)
        self.assertEqual(self.project.config_path.read_text(), edited)
        self.assertTrue((self.project.dir / "scenarios" / "smoke.py").exists())
        self.assertEqual((self.project.dir / ".gitignore").read_text().count("runs/"), 1)

    def test_init_adds_the_runs_entry_to_an_existing_gitignore(self):
        (self.project.dir / ".gitignore").write_text("secrets.txt\n")
        self.assertExit(self.project.vmlab("init"), 0)
        self.assertEqual((self.project.dir / ".gitignore").read_text().splitlines(), ["secrets.txt", "runs/"])

    def test_init_does_not_replace_a_vendored_copy(self):
        fake_zipapp(self.project.dir / "vmlab.pyz", "0.0.1")
        r = self.project.vmlab("init")
        self.assertExit(r, 0)
        self.assertEqual(pyz_version(self.project.dir / "vmlab.pyz"), "vmlab 0.0.1")
        self.assertIn("self-update", r.out)


class SelfUpdateTest(VmlabTestCase):
    def test_self_update_from_the_skill_copy_reports_old_and_new_version(self):
        fake_zipapp(self.project.dir / "vmlab.pyz", "0.0.1")
        current = pyz_version(zipapp_path()).split()[1]

        r = self.project.vmlab("self-update")

        self.assertExit(r, 0)
        self.assertIn("0.0.1 -> %s" % current, r.out)
        self.assertEqual(pyz_version(self.project.dir / "vmlab.pyz"), "vmlab %s" % current)

    def test_self_update_run_from_the_vendored_copy_finds_the_installed_skill(self):
        self.assertExit(self.project.vmlab("init"), 0)
        fake_zipapp(self.project.fake_user_home / ".claude" / "skills" / "vmlab" / "scripts" / "vmlab.pyz", "9.0.0")

        r = self.project.vmlab_vendored("self-update")

        self.assertExit(r, 0)
        self.assertIn("-> 9.0.0", r.out)
        self.assertEqual(pyz_version(self.project.dir / "vmlab.pyz"), "vmlab 9.0.0")

    def test_self_update_from_an_explicit_path(self):
        self.assertExit(self.project.vmlab("init"), 0)
        newer = fake_zipapp(self.project.root / "new" / "vmlab.pyz", "9.1.0")

        r = self.project.vmlab_vendored("self-update", "--from", newer)

        self.assertExit(r, 0)
        self.assertEqual(pyz_version(self.project.dir / "vmlab.pyz"), "vmlab 9.1.0")

    def test_self_update_refuses_to_downgrade(self):
        self.assertExit(self.project.vmlab("init"), 0)
        older = fake_zipapp(self.project.root / "old" / "vmlab.pyz", "0.0.1")

        r = self.project.vmlab("self-update", "--from", older)

        self.assertExit(r, 2)
        self.assertIn("0.0.1", r.err)
        self.assertEqual(pyz_version(self.project.dir / "vmlab.pyz"), pyz_version(zipapp_path()))

    def test_self_update_without_a_skill_copy_says_where_it_looked(self):
        self.assertExit(self.project.vmlab("init"), 0)
        r = self.project.vmlab_vendored("self-update")
        self.assertExit(r, 2)
        self.assertIn(".claude/skills/vmlab/scripts/vmlab.pyz", r.err)
        self.assertIn("--from", r.err)

    def test_self_update_needs_an_initialised_project(self):
        r = self.project.vmlab("self-update")
        self.assertExit(r, 2)
        self.assertIn("vmlab init", r.err)
