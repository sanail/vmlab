"""The Tart Provider's Host-side behaviour, against a scripted stand-in for the tart CLI.

What needs a real macOS Guest (SSH, provisioning, restore by re-cloning) is
covered by Seam 2: tests/contract/test_contract.py with a Tart Lab.
"""

import json
import os
import shutil
import stat
import textwrap

from harness import VmlabTestCase

# A stand-in for `tart`: VMs live in a JSON file, every call is logged.
FAKE_TART = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    state_path, log_path = os.environ["FAKE_TART_STATE"], os.environ["FAKE_TART_LOG"]
    with open(log_path, "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    state = json.load(open(state_path))
    args = sys.argv[1:]
    if args == ["--version"]:
        print("2.37.0")
    elif args[:1] == ["list"]:
        source = args[args.index("--source") + 1] if "--source" in args else None
        rows = [
            {"Name": n, "Source": src, "Running": n in state["running"], "State": "running" if n in state["running"] else "stopped"}
            for src, key in (("local", "local"), ("OCI", "oci"))
            for n in state[key]
            if source in (None, src.lower())
        ]
        print(json.dumps(rows))
    elif args[:1] == ["clone"]:
        state["local"].append(args[2])
        json.dump(state, open(state_path, "w"))
    elif args[:1] == ["delete"]:
        state["local"].remove(args[1])
        json.dump(state, open(state_path, "w"))
    elif args[:1] == ["set"]:
        pass
    elif args[:1] == ["run"]:
        sys.exit("fake tart: no VMs really run here")
    else:
        sys.exit("fake tart: unsupported %r" % args)
    """
)

TART_LAB = """
[labs.mac]
provider = "tart"
os = "macos"
"""


class TartTestCase(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.tart = self.project.root / "fake-tart"
        self.tart.write_text(FAKE_TART)
        self.tart.chmod(self.tart.stat().st_mode | stat.S_IXUSR)
        self.state_path = self.project.root / "tart-state.json"
        self.log_path = self.project.root / "tart-log.jsonl"
        self.tart_state(local=[], oci=[], running=[])

    def tart_state(self, **state):
        self.state_path.write_text(json.dumps(state))

    def tart_calls(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def vmlab(self, *args, tart=None, cwd=None):
        env = {
            "VMLAB_TART": str(tart or self.tart),
            "FAKE_TART_STATE": str(self.state_path),
            "FAKE_TART_LOG": str(self.log_path),
        }
        return self.project.vmlab(*args, env=env, cwd=cwd)

    def base_record(self, provisioned_id="first", name="macos-tahoe"):
        record = {
            "provider": "tart", "os": "macos", "arch": "arm64", "vm": "vmlab-base-%s" % name,
            "image": "ghcr.io/cirruslabs/%s-base:latest" % name, "user": "admin",
            "provisioned": 5, "provisioned_id": provisioned_id,
        }  # fmt: skip
        self.project.home.mkdir(exist_ok=True)
        path = self.project.home / "bases.json"
        records = json.loads(path.read_text()) if path.exists() else {}
        records[name] = record
        path.write_text(json.dumps(records))

    def local_vms(self):
        return json.loads(self.state_path.read_text())["local"]


class BaseCreateTest(TartTestCase):
    def test_a_download_is_never_started_without_confirmation(self):
        r = self.vmlab("base", "create", "macos-tahoe")

        self.assertExit(r, 1)
        self.assertIn("ghcr.io/cirruslabs/macos-tahoe-base:latest", r.err)
        self.assertIn("--yes", r.err)
        self.assertNotIn("clone", [c[0] for c in self.tart_calls()])

    def test_an_unknown_base_lists_the_known_ones(self):
        r = self.vmlab("base", "create", "macos-leopard")

        self.assertExit(r, 2)
        self.assertIn("macos-tahoe", r.err)
        self.assertIn("--image", r.err)

    def test_missing_tart_says_how_to_install_it(self):
        r = self.vmlab("base", "create", "macos-tahoe", "--yes", tart=self.project.root / "no-such-tart")

        self.assertExit(r, 1)
        self.assertIn("brew install cirruslabs/cli/tart", r.err)

    def test_list_shows_no_base_guests_yet(self):
        r = self.vmlab("base", "list")

        self.assertExit(r, 0)
        self.assertIn("vmlab base create macos-tahoe", r.out)


class TartConfigTest(TartTestCase):
    def assertConfigError(self, toml, key, *fragments):
        self.project.config(toml)
        r = self.vmlab("status")
        self.assertExit(r, 2)
        self.assertIn(key, r.err)
        for fragment in fragments:
            self.assertIn(fragment, r.err)

    def test_tart_labs_are_macos_only(self):
        self.assertConfigError('[labs.l]\nprovider = "tart"\nos = "linux"\n', "labs.l.os", "macos", "fusion")

    def test_unknown_option(self):
        self.assertConfigError(TART_LAB + '[labs.mac.tart]\nbsae = "macos-tahoe"\n', "labs.mac.tart.bsae", "unknown key")

    def test_display_must_be_width_x_height(self):
        self.assertConfigError(TART_LAB + '[labs.mac.tart]\ndisplay = "big"\n', "labs.mac.tart.display", "1920x1080")

    def test_channels_must_be_known(self):
        self.assertConfigError(TART_LAB + '[labs.mac.tart]\nchannels = ["vnc"]\n', "labs.mac.tart.channels", "ssh", "exec")

    def test_a_valid_lab_loads(self):
        self.project.config(TART_LAB + '[labs.mac.tart]\nbase = "macos-tahoe"\ncpu = 4\ndisplay = "1920x1080"\n')
        r = self.vmlab("status", "--json")
        self.assertExit(r, 0)
        [row] = json.loads(r.out)
        self.assertEqual((row["provider"], row["running"]), ("tart", False))


class TartDoctorTest(TartTestCase):
    def test_a_missing_base_guest_fails_with_the_create_command(self):
        self.project.config(TART_LAB)

        r = self.vmlab("doctor")

        self.assertExit(r, 1)
        self.assertRegex(r.out, r"FAIL\s+mac: Provider tart")
        self.assertIn("vmlab base create macos-tahoe", r.out)

    def test_missing_tart_fails_with_the_install_command(self):
        self.project.config(TART_LAB)

        r = self.vmlab("doctor", tart=self.project.root / "no-such-tart")

        self.assertExit(r, 1)
        self.assertIn("brew install cirruslabs/cli/tart", r.out)

    def test_up_without_a_base_guest_names_the_create_command(self):
        self.project.config(TART_LAB)

        r = self.vmlab("up")

        self.assertExit(r, 1)
        self.assertIn("vmlab base create macos-tahoe", r.err)
        self.assertNotIn("clone", [c[0] for c in self.tart_calls()])


class TartCloneTest(TartTestCase):
    """A Lab's clone is made again whenever its Base guest has been provisioned since."""

    def clone_calls(self):
        calls = [c[0] for c in self.tart_calls() if c[0] in ("clone", "delete")]
        self.log_path.unlink()
        return calls

    def test_reprovisioning_the_base_recreates_the_clone_even_at_the_same_version(self):
        self.project.config(TART_LAB)
        self.tart_state(local=["vmlab-base-macos-tahoe"], oci=[], running=[])
        self.base_record("first")
        self.vmlab("up")  # the fake tart never boots a VM; only the clone matters here
        self.assertEqual(self.clone_calls(), ["clone"])

        self.vmlab("up")
        self.assertEqual(self.clone_calls(), [], "same provisioning: the clone is reused")

        self.base_record("second")  # `vmlab base create --reprovision`, same PROVISION_VERSION
        self.vmlab("up")
        self.assertEqual(self.clone_calls(), ["delete", "clone"])


class CleanTest(TartTestCase):
    """`vmlab clean`: leftovers of Labs that are gone, deleted only when confirmed."""

    def setUp(self):
        super().setUp()
        self.tart_state(local=["vmlab-base-macos-tahoe", "maclab"], oci=[], running=[])
        self.base_record()
        self.project.config(TART_LAB)

    def clone_for(self, cwd=None):
        """Start a Lab so its clone (and the clone's record) exists; the fake tart never boots it."""
        self.vmlab("up", cwd=cwd)
        [clone] = [vm for vm in self.local_vms() if vm.startswith("vmlab-") and not vm.startswith("vmlab-base-")][-1:]
        return clone

    def other_project(self, toml=TART_LAB):
        root = self.project.root / "other"
        (root / ".vmlab").mkdir(parents=True)
        (root / ".vmlab" / "vmlab.toml").write_text(toml)
        return root

    def test_nothing_to_clean_while_every_clone_belongs_to_a_lab(self):
        self.clone_for()
        r = self.vmlab("clean", "--yes")
        self.assertExit(r, 0)
        self.assertIn("nothing to clean", r.out)

    def test_the_clone_of_a_deleted_project_is_listed_and_deleted_when_confirmed(self):
        other = self.other_project()
        clone = self.clone_for(cwd=other)
        shutil.rmtree(str(other))

        r = self.vmlab("clean")  # no terminal: list only
        self.assertExit(r, 0)
        self.assertIn(clone, r.out)
        self.assertIn("no longer exists", r.out)
        self.assertIn("--yes", r.out)
        self.assertIn(clone, self.local_vms())

        r = self.vmlab("clean", "--yes")
        self.assertExit(r, 0)
        self.assertNotIn(clone, self.local_vms())
        self.assertEqual([f.name for f in (self.project.home / "tart").iterdir() if f.name.startswith(clone)], [])

    def test_the_clone_of_a_lab_removed_from_the_config_is_listed(self):
        other = self.other_project()
        clone = self.clone_for(cwd=other)
        (other / ".vmlab" / "vmlab.toml").write_text('[labs.linux]\nprovider = "fake"\nos = "linux"\n')
        r = self.vmlab("clean")
        self.assertIn(clone, r.out)
        self.assertIn("no Lab mac", r.out)

    def test_a_clone_without_a_record_is_listed_as_of_unknown_origin(self):
        self.tart_state(local=["vmlab-base-macos-tahoe", "vmlab-old-12345678-mac"], oci=[], running=[])
        r = self.vmlab("clean")
        self.assertIn("vmlab-old-12345678-mac", r.out)
        self.assertIn("unknown", r.out)

    def test_a_running_leftover_is_never_deleted(self):
        other = self.other_project()
        clone = self.clone_for(cwd=other)
        shutil.rmtree(str(other))
        self.tart_state(local=self.local_vms(), oci=[], running=[clone])
        r = self.vmlab("clean", "--yes")
        self.assertExit(r, 0)
        self.assertIn("running", r.out)
        self.assertIn(clone, self.local_vms())

    def test_vms_vmlab_did_not_make_are_never_touched(self):
        r = self.vmlab("clean", "--yes", "--bases")
        self.assertNotIn("maclab", r.out)
        self.assertIn("maclab", self.local_vms())

    def test_an_unused_base_guest_is_deleted_only_with_bases(self):
        self.clone_for()
        self.base_record(name="macos-sequoia")
        self.tart_state(local=self.local_vms() + ["vmlab-base-macos-sequoia"], oci=[], running=[])

        r = self.vmlab("clean", "--yes")
        self.assertIn("macos-sequoia", r.out)
        self.assertIn("--bases", r.out)
        self.assertIn("vmlab-base-macos-sequoia", self.local_vms())

        r = self.vmlab("clean", "--yes", "--bases")
        self.assertExit(r, 0)
        self.assertNotIn("vmlab-base-macos-sequoia", self.local_vms())
        self.assertIn("vmlab-base-macos-tahoe", self.local_vms(), "still used by this project's Lab")
        self.assertNotIn("macos-sequoia", json.loads((self.project.home / "bases.json").read_text()))

    def test_service_files_without_their_vm_are_removed(self):
        tart_dir = self.project.home / "tart"
        tart_dir.mkdir(parents=True)
        for name in ("vmlab-gone-1-mac.log", "vmlab-gone-1-mac.base-provisioned", "vmlab-gone-1-mac.json"):
            (tart_dir / name).write_text("x")
        r = self.vmlab("clean", "--yes")
        self.assertExit(r, 0)
        self.assertIn("vmlab-gone-1-mac", r.out)
        self.assertEqual(list(tart_dir.iterdir()), [])
