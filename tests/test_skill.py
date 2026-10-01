"""The skill in skills/vmlab/, and the Claude Code plugin that ships it: what an agent installs.

It must follow the open SKILL.md standard, every document it points to must be in
it, every vmlab command its documents name must exist, its launcher must vendor the
vmlab it runs into a project, and it must name the version the code has.
"""

import json
import re
import subprocess
import sys

from harness import REPO, VmlabTestCase, zipapp_path

LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
TOML_BLOCK = re.compile(r"^```toml\n(.*?)^```", re.M | re.S)
COMMAND = re.compile(r"(?:^|`)vmlab ((?:ui |base )?[a-z][a-z-]*)", re.M)  # in code: `vmlab run`, or a code block line


def skill_dir():
    return REPO / "skills" / "vmlab"


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

    def test_the_launcher_vendors_the_vmlab_it_runs_into_a_project(self):
        self.project.dir.rmdir()
        r = self.project.vmlab("init", launcher=skill_dir() / "scripts" / "vmlab", env={"VMLAB_PYZ": str(zipapp_path())})
        self.assertExit(r, 0)
        self.assertEqual((self.project.dir / "vmlab.pyz").read_bytes(), zipapp_path().read_bytes())
        self.assertExit(self.project.vmlab_vendored("version"), 0)

    def test_the_launcher_and_the_plugin_name_the_version_the_code_has(self):
        version = subprocess.run([sys.executable, str(zipapp_path()), "version"], capture_output=True, text=True).stdout.split()[1]
        launcher = re.search(r"^VERSION=(.*)$", (skill_dir() / "scripts" / "vmlab").read_text(), re.M).group(1)
        plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual((launcher, plugin["version"]), (version, version), "bump all three together")

    def help(self, *command):
        out = subprocess.run(
            [sys.executable, str(zipapp_path())] + list(command) + ["--help"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return set(re.findall(r"^    ([a-z][a-z-]*)", out, re.M))
