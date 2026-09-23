"""The Fusion Provider's Host-side behaviour, against a scripted stand-in for vmrun.

What needs a real Linux Guest (the unattended install, provisioning, SSH and
the vmrun Channel) is covered by Seam 2: tests/contract/test_contract.py with
a Fusion Lab.
"""

import json
import shutil
import stat
import textwrap
from pathlib import Path

from harness import VmlabTestCase

# A stand-in for `vmrun` (and `vmcli`): VMs are .vmx files, their state lives in
# a JSON file, every call is logged. A .vmx the state does not know yet (a copy)
# is a stopped VM with the snapshots listed in its .vmsd, one per line. A .vmx
# that mentions encryption.keySafe opens only with -vp and the state's vm_password.
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
    auth = {}
    while args and args[0] in ("-vp", "-gu", "-gp"):
        auth[args[0]] = args[1]
        args = args[2:]
    state = json.load(open(state_path))
    def save():
        json.dump(state, open(state_path, "w"))
    def fail(message):
        print("Error: " + message)
        sys.exit(255)
    command = args[0] if args else ""
    vmx = os.path.realpath(args[1]) if len(args) > 1 else None
    if vmx and vmx not in state["vms"] and os.path.isfile(vmx) and vmx.endswith(".vmx"):
        vmsd = vmx[:-4] + ".vmsd"
        snapshots = open(vmsd).read().split() if os.path.exists(vmsd) else []
        state["vms"][vmx] = {"running": False, "snapshots": snapshots, "reverted": 0}
    vm = state["vms"].get(vmx) if vmx else None
    encrypted = vm is not None and "encryption.keySafe" in open(vmx).read()
    if encrypted and auth.get("-vp") != state.get("vm_password"):
        fail("Cannot open VM: %s, A password is required for this operation" % args[1])
    if command == "list":
        running = [p for p, v in state["vms"].items() if v["running"]]
        print("Total running VMs: %d" % len(running))
        for p in running:
            print(p)
    elif command == "start":
        if vm is None:
            fail("Cannot open VM: %s, The virtual machine cannot be found" % args[1])
        if "gui" in args[2:] and encrypted and state.get("gui_refused"):
            fail("The operation is not supported")  # Fusion without the VM's password from the Keychain
        vm["running"] = True
        save()
    elif command == "stop":
        vm["running"] = False
        save()
    elif command == "clone":
        if vm is None:
            fail("no such source VM")
        if encrypted:
            fail("Cannot read the virtual machine configuration file")
        snapshot = [a.split("=", 1)[1] for a in args if a.startswith("-snapshot=")][0]
        if snapshot not in vm["snapshots"]:
            fail("Invalid snapshot name '%s'" % snapshot)
        dest = args[2]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(args[1], dest)
        state["vms"][os.path.realpath(dest)] = {"running": False, "snapshots": [], "reverted": 0}
        save()
    elif command == "deleteVM":
        if state.get("open_in_fusion"):
            fail("Insufficient permissions")  # Fusion's window or library has the VM open
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
        vm.setdefault("reverted_to", []).append(args[2])
        vm["running"] = False
        save()
    elif command == "getGuestIPAddress":
        fail("The VMware Tools are not running in the virtual machine: " + args[1])
    else:
        fail("fake vmrun: unsupported %r" % args)
    """
)

def _without_auth(args):
    while args[:1] in (["-vp"], ["-gu"], ["-gp"]):
        args = args[2:]
    return args


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

    def raw_calls(self):
        """Every vmrun call as logged, with its passwords (-vp, -gu, -gp)."""
        return [json.loads(line) for line in self.log_path.read_text().splitlines()] if self.log_path.exists() else []

    def calls(self, command):
        """The calls of one vmrun command, without their passwords."""
        return [c for c in map(_without_auth, self.raw_calls()) if c[0] == command]

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

    def test_fusion_labs_are_linux_or_windows(self):
        self.assertConfigError('[labs.m]\nprovider = "fusion"\nos = "macos"\n', "labs.m.os", "linux or windows", 'provider = "tart"')

    def test_tart_points_linux_labs_to_fusion(self):
        self.assertConfigError('[labs.l]\nprovider = "tart"\nos = "linux"\n', "labs.l.os", 'provider = "fusion"')

    def test_unknown_option(self):
        self.assertConfigError(FUSION_LAB + '[labs.linux.fusion]\nbsae = "ubuntu-26.04"\n', "labs.linux.fusion.bsae", "unknown key")

    def test_channels_must_be_known(self):
        self.assertConfigError(FUSION_LAB + '[labs.linux.fusion]\nchannels = ["exec"]\n', "labs.linux.fusion.channels", "ssh", "vmrun")

    def test_session_is_x11_or_wayland(self):
        self.assertConfigError(FUSION_LAB + '[labs.linux.fusion]\nsession = "mir"\n', "labs.linux.fusion.session", "x11", "wayland")

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

    def ready_base(self, credentials=True, **record):
        self.base_guest("first")
        path = self.project.home / "bases.json"
        records = json.loads(path.read_text())
        records["ubuntu-26.04"].update(dict({"provisioned": 4}, **record))
        path.write_text(json.dumps(records))
        if credentials:
            (self.project.home / "fusion" / "vmlab-base-ubuntu-26.04.credentials.json").write_text('{"user": "vmlab", "password": "pw"}')
        ssh = self.project.home / "ssh"
        ssh.mkdir(exist_ok=True)
        (ssh / "known_hosts").write_text("vmlab-base-ubuntu-26.04 ssh-ed25519 AAAA\n")

    def test_a_ready_base_guest_and_no_clone_yet(self):
        self.project.config(FUSION_LAB)
        self.ready_base()

        r = self.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"ok\s+linux: Provider fusion")
        self.assertRegex(r.out, r"ok\s+linux: Base guest ubuntu-26.04: provisioned \(v4\)")
        self.assertRegex(r.out, r"info\s+linux: Clone: none yet")

    def test_a_base_guest_provisioned_by_an_older_vmlab_is_a_warning(self):
        self.project.config(FUSION_LAB)
        self.ready_base(provisioned=1)

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+linux: Base guest ubuntu-26.04: provisioned by an older vmlab \(v1; this one provisions v4\)")

    def test_a_base_guest_without_its_provisioned_snapshot_fails(self):
        self.project.config(FUSION_LAB)
        self.ready_base()
        state = json.loads(self.state_path.read_text())
        for vm in state["vms"].values():
            vm["snapshots"] = []
        self.state_path.write_text(json.dumps(state))

        r = self.vmlab("doctor")

        self.assertExit(r, 1)
        self.assertRegex(r.out, r"FAIL\s+linux: Base guest ubuntu-26.04: .*snapshot vmlab-provisioned-first is missing")
        self.assertIn("vmlab base create ubuntu-26.04 --reprovision", r.out)

    def test_missing_guest_credentials_break_the_vmrun_channel(self):
        self.project.config(FUSION_LAB)
        self.ready_base(credentials=False)

        r = self.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"warn\s+linux: Guest credentials: .*missing")

    def test_a_running_base_guest_is_a_warning_since_labs_cannot_clone_it(self):
        self.project.config(FUSION_LAB)
        self.ready_base()
        state = json.loads(self.state_path.read_text())
        for vm in state["vms"].values():
            vm["running"] = True
        self.state_path.write_text(json.dumps(state))

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+linux: Base guest ubuntu-26.04: running")
        self.assertIn("-T fusion stop '%s" % self.project.home, r.out)

    def test_a_clone_for_another_session_is_made_again_at_the_next_up(self):
        self.project.config(FUSION_LAB)
        self.ready_base()
        self.vmlab("up")  # no Guest really boots here; the clone is made
        self.vmlab("down")
        self.project.config(FUSION_LAB + '[labs.linux.fusion]\nsession = "x11"\n')

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"info\s+linux: Clone: made for the Wayland session")
        self.assertIn("vmlab up linux", r.out)

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

    def test_a_clone_open_in_fusion_says_how_to_let_it_go(self):
        self.vmlab("up")
        self.vmlab("down")
        self.base_guest("second")
        state = json.loads(self.state_path.read_text())
        state["open_in_fusion"] = True
        self.state_path.write_text(json.dumps(state))

        r = self.vmlab("up")

        self.assertExit(r, 1)
        self.assertIn("Insufficient permissions", r.err)
        self.assertIn("Remove from Library", r.err)

    def test_an_x11_lab_gets_clean_state_only_once_its_session_is_switched(self):
        # The session is switched in the booted clone, before vmlab-clean is taken; no Guest boots here.
        self.project.config(FUSION_LAB + '[labs.linux.fusion]\nsession = "x11"\n')

        r = self.vmlab("up")

        self.assertExit(r, 1)
        self.assertIn("X11 session", r.err)
        [vm] = self.clones().values()
        self.assertNotIn("vmlab-clean", vm["snapshots"])
        self.assertFalse(vm["running"], "a clone left half-prepared is stopped")

        self.vmlab("up")

        self.assertEqual(len(self.calls("deleteVM")), 1, "a clone without Clean state is made again")
        self.assertEqual(len(self.calls("clone")), 2)

    def test_changing_the_session_recreates_the_clone(self):
        self.vmlab("up")
        self.vmlab("down")
        self.project.config(FUSION_LAB + '[labs.linux.fusion]\nsession = "x11"\n')

        self.vmlab("up")

        self.assertEqual(len(self.calls("deleteVM")), 1)
        self.assertEqual(len(self.calls("clone")), 2)

    def test_down_stops_the_clone(self):
        self.vmlab("up")
        r = self.vmlab("down")
        self.assertExit(r, 0)
        [vm] = self.clones().values()
        self.assertFalse(vm["running"])


class FusionCleanTest(FusionTestCase):
    """`vmlab clean` also finds Fusion leftovers: clones of Labs that are gone, stray files, unused Base guests."""

    def setUp(self):
        super().setUp()
        self.base_guest()
        self.project.config(FUSION_LAB)

    def clone_for(self, cwd=None):
        """Make a Lab's clone (and its record); the fake vmrun never boots it."""
        self.vmlab("up", cwd=cwd)
        self.vmlab("down", cwd=cwd)
        [path] = [p for p in self.clones() if p not in getattr(self, "_known", ())]
        self._known = set(self.clones())
        return Path(path)

    def other_project(self):
        root = self.project.root / "other"
        (root / ".vmlab").mkdir(parents=True)
        (root / ".vmlab" / "vmlab.toml").write_text(FUSION_LAB)
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
        self.assertIn(clone.stem, r.out)
        self.assertIn("no longer exists", r.out)
        self.assertTrue(clone.exists())

        r = self.vmlab("clean", "--yes")
        self.assertExit(r, 0)
        self.assertEqual([c[0] for c in self.calls("deleteVM")], ["deleteVM"])
        self.assertFalse(clone.parent.exists())
        self.assertEqual([f.name for f in (self.project.home / "fusion").iterdir() if f.name.startswith(clone.stem)], [])

    def test_a_running_leftover_is_never_deleted(self):
        other = self.other_project()
        clone = self.clone_for(cwd=other)
        shutil.rmtree(str(other))
        state = json.loads(self.state_path.read_text())
        state["vms"][str(clone.resolve())]["running"] = True
        self.state_path.write_text(json.dumps(state))

        r = self.vmlab("clean", "--yes")

        self.assertExit(r, 0)
        self.assertIn("running", r.out)
        self.assertTrue(clone.exists())

    def test_an_unused_base_guest_is_deleted_only_with_bases_with_its_credentials(self):
        self.clone_for()
        self.base_guest(name="ubuntu-24.04")
        credentials = self.project.home / "fusion" / "vmlab-base-ubuntu-24.04.credentials.json"
        credentials.write_text("{}")

        r = self.vmlab("clean", "--yes")
        self.assertIn("ubuntu-24.04", r.out)
        self.assertIn("--bases", r.out)
        self.assertTrue(credentials.exists())

        r = self.vmlab("clean", "--yes", "--bases")
        self.assertExit(r, 0)
        self.assertFalse(credentials.exists())
        self.assertFalse((self.project.home / "fusion" / "vmlab-base-ubuntu-24.04.vmwarevm").exists())
        self.assertTrue((self.project.home / "fusion" / "vmlab-base-ubuntu-26.04.vmwarevm").exists(), "still used by this project's Lab")
        self.assertNotIn("ubuntu-24.04", json.loads((self.project.home / "bases.json").read_text()))

    def test_service_files_without_their_vm_are_removed(self):
        for name in ("vmlab-gone-1-linux.json", "vmlab-base-gone.credentials.json"):
            (self.project.home / "fusion" / name).write_text("{}")
        r = self.vmlab("clean", "--yes")
        self.assertExit(r, 0)
        self.assertIn("vmlab-gone-1-linux", r.out)
        self.assertIn("vmlab-base-gone", r.out)
        self.assertEqual(sorted(f.name for f in (self.project.home / "fusion").iterdir()), ["vmlab-base-ubuntu-26.04.vmwarevm"])

    def test_a_hypervisor_that_cannot_be_asked_is_skipped_with_a_warning_and_the_rest_still_cleaned(self):
        records = json.loads((self.project.home / "bases.json").read_text())
        records["macos-tahoe"] = {"provider": "tart", "os": "macos", "arch": "arm64", "vm": "vmlab-base-macos-tahoe", "provisioned": 6}
        (self.project.home / "bases.json").write_text(json.dumps(records))
        (self.project.home / "fusion" / "vmlab-gone-1-linux.json").write_text("{}")

        r = self.vmlab("clean", "--yes")  # the harness gives no Tart

        self.assertExit(r, 0)
        self.assertIn("warning: tart: skipped", r.out)
        self.assertIn("deleted files vmlab-gone-1-linux", r.out)
