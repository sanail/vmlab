"""The skill package tools/build.py assembles next to the zipapp: what an agent installs.

It must follow the open SKILL.md standard, every document it points to must be in
it, every vmlab command its documents name must exist, and its bundled zipapp must
vendor itself into a project.
"""

import re
import subprocess
import sys

from harness import VmlabTestCase, zipapp_path

LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
TOML_BLOCK = re.compile(r"^```toml\n(.*?)^```", re.M | re.S)
COMMAND = re.compile(r"(?:^|`)vmlab ((?:ui |base )?[a-z][a-z-]*)", re.M)  # in code: `vmlab run`, or a code block line


def skill_dir():
    return zipapp_path().parent / "skill" / "vmlab"


def frontmatter(text):
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, text[match.end():]


class SkillPackageTest(VmlabTestCase):
    def docs(self):
        return sorted(skill_dir().rglob("*.md"))

    def test_skill_md_frontmatter_follows_the_open_standard(self):
        fields, body = frontmatter((skill_dir() / "SKILL.md").read_text())
        self.assertEqual(fields.get("name"), "vmlab")  # lowercase, hyphens; equals the folder's name
        self.assertTrue(fields.get("description"))
        self.assertLessEqual(len(fields["description"]), 1024)
        self.assertLess(len(body.splitlines()), 500, "SKILL.md is a router; detail goes in its references")

    def test_every_linked_document_is_in_the_package(self):
        for doc in self.docs():
            for target in LINK.findall(doc.read_text()):
                if "://" in target:
                    continue
                self.assertTrue((doc.parent / target).exists(), "%s links to missing %s" % (doc, target))

    def test_every_document_is_reachable_from_skill_md(self):
        reached, todo = set(), [skill_dir() / "SKILL.md"]
        while todo:
            doc = todo.pop().resolve()
            if doc in reached:
                continue
            reached.add(doc)
            todo += [doc.parent / t for t in LINK.findall(doc.read_text()) if t.endswith(".md") and "://" not in t]
        self.assertEqual({d.resolve() for d in self.docs()}, reached)

    def test_every_vmlab_command_the_documents_name_exists(self):
        top = self.help()
        for doc in self.docs():
            for command in set(COMMAND.findall(doc.read_text())):
                words = command.split()
                if len(words) == 2:
                    self.assertIn(words[1], self.help(words[0]), "%s names `vmlab %s`" % (doc.name, command))
                else:
                    self.assertIn(words[0], top, "%s names `vmlab %s`" % (doc.name, command))

    def test_each_guest_os_has_a_traps_reference_that_setup_and_scenarios_point_to(self):
        for os_name in ("macos", "windows", "linux"):
            traps = "traps-%s.md" % os_name
            self.assertTrue((skill_dir() / "references" / traps).is_file(), traps)
            for doc in ("setup.md", "scenarios.md"):
                self.assertIn(traps, LINK.findall((skill_dir() / "references" / doc).read_text()), doc)

    def test_every_toml_example_is_a_config_vmlab_loads(self):
        for doc in self.docs():
            for block in TOML_BLOCK.findall(doc.read_text()):
                labs = sorted(set(re.findall(r"^\[labs\.([a-z0-9_-]+)", block, re.M)))
                declared = set(re.findall(r"^\[labs\.([a-z0-9_-]+)\]", block, re.M))
                stubs = "".join('[labs.%s]\nprovider = "fake"\nos = "linux"\n' % lab for lab in labs if lab not in declared)
                self.project.config(stubs + block)

                r = self.project.vmlab("doctor", "--json")

                self.assertNotEqual(r.code, 2, "%s: a TOML example does not load:\n%s\n%s" % (doc.name, block, r.err))

    def test_the_bundled_zipapp_vendors_itself_into_a_project(self):
        pyz = skill_dir() / "scripts" / "vmlab.pyz"
        self.project.dir.rmdir()
        self.assertExit(self.project.vmlab("init", pyz=pyz), 0)
        self.assertTrue((self.project.dir / "vmlab.pyz").is_file())

    def help(self, *command):
        out = subprocess.run(
            [sys.executable, str(skill_dir() / "scripts" / "vmlab.pyz")] + list(command) + ["--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return set(re.findall(r"^    ([a-z][a-z-]*)", out, re.M))
