"""The Fusion Provider's Host-side behaviour, against a scripted stand-in for vmrun.

What needs a real Linux Guest (the unattended install, provisioning, SSH and
the vmrun Channel) is covered by Seam 2: tests/contract/test_contract.py with
a Fusion Lab.
"""

import json
import stat
import textwrap

from harness import VmlabTestCase

# A stand-in for `vmrun` (and `vmcli`): VMs are .vmx files, their state lives in
# a JSON file, every call is logged.
FAKE_VMRUN = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, shutil, sys
    state_path, log_path = os.environ["FAKE_VMRUN_STATE"], os.environ["FAKE_VMRUN_LOG"]
    args = sys.argv[1:]
    if args[:2] == ["-T", "fusion"]:
        args = args[2:]
    with open(log_path, "a") as log:
        log.write(json.dumps(args) + "\\n")
    state = json.load(open(state_path))
    def save():
        json.dump(state, open(state_path, "w"))
    def fail(message):
        print("Error: " + message)
        sys.exit(255)
    command = args[0] if args else ""
    vmx = os.path.realpath(args[1]) if len(args) > 1 else None
    vm = state["vms"].get(vmx) if vmx else None
    if command == "list":
        running = [p for p, v in state["vms"].items() if v["running"]]
        print("Total running VMs: %d" % len(running))
        for p in running:
            print(p)
    elif command == "start":
        if vm is None:
            fail("Cannot open VM: %s, The virtual machine cannot be found" % args[1])
        vm["running"] = True
        save()
    elif command == "stop":
        vm["running"] = False
        save()
    elif command == "clone":
        if vm is None:
            fail("no such source VM")
        snapshot = [a.split("=", 1)[1] for a in args if a.startswith("-snapshot=")][0]
        if snapshot not in vm["snapshots"]:
            fail("Invalid snapshot name '%s'" % snapshot)
        dest = args[2]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(args[1], dest)
        state["vms"][os.path.realpath(dest)] = {"running": False, "snapshots": [], "reverted": 0}
        save()
    elif command == "deleteVM":
        shutil.rmtree(os.path.dirname(args[1]), ignore_errors=True)
        del state["vms"][vmx]
        save()
    elif command == "snapshot":
        vm["snapshots"].append(args[2])
        save()
    elif command == "listSnapshots":
        print("Total snapshots: %d" % len(vm["snapshots"]))
        for s in vm["snapshots"]:
            print(s)
    elif command == "revertToSnapshot":
        if args[2] not in vm["snapshots"]:
            fail("Invalid snapshot name '%s'" % args[2])
        vm["reverted"] += 1
        vm["running"] = False
        save()
    elif command == "getGuestIPAddress":
        fail("The VMware Tools are not running in the virtual machine: " + args[1])
    else:
        fail("fake vmrun: unsupported %r" % args)
    """
)

FUSION_LAB = """
[labs.linux]
provider = "fusion"
os = "linux"
boot_timeout = 1
"""


class FusionTestCase(VmlabTestCase):
    def setUp(self):
        super().setUp()
        self.vmrun = self.project.root / "fake-vmrun"
        self.vmrun.write_text(FAKE_VMRUN)
        self.vmrun.chmod(self.vmrun.stat().st_mode | stat.S_IXUSR)
        self.state_path = self.project.root / "vmrun-state.json"
        self.log_path = self.project.root / "vmrun-log.jsonl"
        self.state_path.write_text(json.dumps({"vms": {}}))

    def vmlab(self, *args, vmrun=None, cwd=None):
        env = {
            "VMLAB_VMRUN": str(vmrun or self.vmrun),
            "FAKE_VMRUN_STATE": str(self.state_path),
            "FAKE_VMRUN_LOG": str(self.log_path),
        }
        return self.project.vmlab(*args, env=env, cwd=cwd)

    def calls(self, command):
        if not self.log_path.exists():
            return []
        return [c for c in (json.loads(line) for line in self.log_path.read_text().splitlines()) if c[0] == command]

    def vms(self):
        return json.loads(self.state_path.read_text())["vms"]

    def base_guest(self, provisioned_id="first", name="ubuntu-26.04"):
        """A provisioned Base guest: its VM, its snapshot and its registry record."""
        vm = "vmlab-base-%s" % name
        vmx = self.project.home / "fusion" / ("%s.vmwarevm" % vm) / ("%s.vmx" % vm)
        vmx.parent.mkdir(parents=True, exist_ok=True)
        vmx.write_text('displayName = "%s"\n' % vm)
        snapshot = "vmlab-provisioned-%s" % provisioned_id
        state = json.loads(self.state_path.read_text())
        state["vms"][str(vmx.resolve())] = {"running": False, "snapshots": [snapshot], "reverted": 0}
        self.state_path.write_text(json.dumps(state))
        record = {
            "provider": "fusion", "os": "linux", "arch": "arm64", "vm": vm, "vmx": str(vmx), "image": "ubuntu.iso",
            "user": "vmlab", "provisioned": 1, "provisioned_id": provisioned_id, "snapshot": snapshot,
        }  # fmt: skip
        path = self.project.home / "bases.json"
        records = json.loads(path.read_text()) if path.exists() else {}
        records[name] = record
        path.write_text(json.dumps(records))

    def clones(self):
        return {path: vm for path, vm in self.vms().items() if "vmlab-base-" not in path}


class FusionConfigTest(FusionTestCase):
    def assertConfigError(self, toml, key, *fragments):
        self.project.config(toml)
        r = self.vmlab("status")
        self.assertExit(r, 2)
        self.assertIn(key, r.err)
        for fragment in fragments:
            self.assertIn(fragment, r.err)

    def test_fusion_labs_are_linux_only_so_far(self):
        self.assertConfigError('[labs.w]\nprovider = "fusion"\nos = "windows"\n', "labs.w.os", "linux")

    def test_tart_points_linux_labs_to_fusion(self):
        self.assertConfigError('[labs.l]\nprovider = "tart"\nos = "linux"\n', "labs.l.os", 'provider = "fusion"')

    def test_unknown_option(self):
        self.assertConfigError(FUSION_LAB + '[labs.linux.fusion]\nbsae = "ubuntu-26.04"\n', "labs.linux.fusion.bsae", "unknown key")

    def test_channels_must_be_known(self):
        self.assertConfigError(FUSION_LAB + '[labs.linux.fusion]\nchannels = ["exec"]\n', "labs.linux.fusion.channels", "ssh", "vmrun")

    def test_a_valid_lab_loads(self):
        self.project.config(FUSION_LAB + '[labs.linux.fusion]\nbase = "ubuntu-26.04"\ncpu = 2\nchannels = ["vmrun"]\n')
        r = self.vmlab("status", "--json")
        self.assertExit(r, 0)
        [row] = json.loads(r.out)
        self.assertEqual((row["provider"], row["os"], row["running"]), ("fusion", "linux", False))


class FusionDoctorTest(FusionTestCase):
    def test_missing_fusion_fails_with_where_to_get_it(self):
        self.project.config(FUSION_LAB)

        r = self.vmlab("doctor", vmrun=self.project.root / "no-such-vmrun")

        self.assertExit(r, 1)
        self.assertRegex(r.out, r"FAIL\s+linux: Provider fusion")
        self.assertIn("VMware Fusion", r.out)

    def test_a_missing_base_guest_fails_with_the_create_command(self):
        self.project.config(FUSION_LAB)

        r = self.vmlab("doctor")

        self.assertExit(r, 1)
        self.assertIn("vmlab base create ubuntu-26.04", r.out)

    def test_up_without_a_base_guest_names_the_create_command(self):
        self.project.config(FUSION_LAB)

        r = self.vmlab("up")

        self.assertExit(r, 1)
        self.assertIn("vmlab base create ubuntu-26.04", r.err)
        self.assertEqual(self.calls("clone"), [])


class UbuntuBaseCreateTest(FusionTestCase):
    def test_the_iso_is_never_downloaded_without_confirmation(self):
        r = self.vmlab("base", "create", "ubuntu-26.04")

        self.assertExit(r, 1)
        self.assertIn("ubuntu-26.04.1-desktop-arm64.iso", r.err)
        self.assertIn("--yes", r.err)
        self.assertFalse((self.project.home / "images").exists() and any((self.project.home / "images").iterdir()))

    def test_a_missing_local_iso_is_named(self):
        r = self.vmlab("base", "create", "ubuntu-26.04", "--image", self.project.root / "nope.iso", "--yes")

        self.assertExit(r, 1)
        self.assertIn("nope.iso", r.err)

    def test_missing_fusion_says_where_to_get_it(self):
        r = self.vmlab("base", "create", "ubuntu-26.04", "--yes", vmrun=self.project.root / "no-such-vmrun")

        self.assertExit(r, 1)
        self.assertIn("VMware Fusion", r.err)

    def test_list_shows_both_kinds_of_base_guest(self):
        self.base_guest()
        r = self.vmlab("base", "list")
        self.assertExit(r, 0)
        self.assertRegex(r.out, r"ubuntu-26.04\s+fusion\s+linux/arm64\s+ready")


class FusionCloneTest(FusionTestCase):
    """A Lab runs in a linked clone of its Base guest's provisioned snapshot."""

    def setUp(self):
        super().setUp()
        self.project.config(FUSION_LAB)
        self.base_guest("first")

    def test_up_makes_a_linked_clone_of_the_provisioned_snapshot_and_starts_it(self):
        self.vmlab("up")  # never reachable here: no Guest really boots

        [clone] = self.calls("clone")
        self.assertIn("linked", clone)
        self.assertIn("-snapshot=vmlab-provisioned-first", clone)
        [(path, vm)] = self.clones().items()
        self.assertIn("vmlab-clean", vm["snapshots"])
        self.assertTrue(vm["running"])
        self.assertTrue(path.startswith(str(self.project.home.resolve())), "clones live in vmlab's home, not the project")

    def test_the_clone_is_reused_while_the_base_guest_is_unchanged(self):
        self.vmlab("up")
        self.vmlab("down")
        self.vmlab("up")

        self.assertEqual(len(self.calls("clone")), 1)

    def test_reprovisioning_the_base_recreates_the_clone(self):
        self.vmlab("up")
        self.vmlab("down")
        self.base_guest("second")

        self.vmlab("up")

        self.assertEqual(len(self.calls("deleteVM")), 1)
        self.assertIn("-snapshot=vmlab-provisioned-second", self.calls("clone")[-1])
        self.assertEqual(len(self.clones()), 1)

    def test_down_stops_the_clone(self):
        self.vmlab("up")
        r = self.vmlab("down")
        self.assertExit(r, 0)
        [vm] = self.clones().values()
        self.assertFalse(vm["running"])
