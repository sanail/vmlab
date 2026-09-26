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
    elif command == "deleteSnapshot":
        if args[2] not in vm["snapshots"]:
            fail("Invalid snapshot name '%s'" % args[2])
        vm["snapshots"].remove(args[2])
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
    elif command == "checkToolsState":
        print(vm.get("tools", "unknown") if vm and vm["running"] else "unknown")
    elif command == "readVariable" and args[2:] == ["guestVar", "ip"]:
        print(vm.get("ip", "") if vm and vm["running"] else "")
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

    def vmlab(self, *args, vmrun=None, cwd=None, env=None):
        env = dict({
            "VMLAB_VMRUN": str(vmrun or self.vmrun),
            "FAKE_VMRUN_STATE": str(self.state_path),
            "FAKE_VMRUN_LOG": str(self.log_path),
            "VMLAB_DELETE_OLD_SNAPSHOTS": "",  # not the Host's own setting
        }, **(env or {}))  # fmt: skip
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
        vmx = self.base_vmx_path(name)
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

    def set_state(self, vmx, **fields):
        state = json.loads(self.state_path.read_text())
        state["vms"][str(Path(vmx).resolve())].update(fields)
        self.state_path.write_text(json.dumps(state))

    def snapshots(self, vmx):
        return self.vms()[str(Path(vmx).resolve())]["snapshots"]

    def clones(self):
        return {path: vm for path, vm in self.vms().items() if "vmlab-base-" not in path}

    def base_vmx_path(self, name="ubuntu-26.04"):
        vm = "vmlab-base-%s" % name
        return self.project.home / "fusion" / ("%s.vmwarevm" % vm) / ("%s.vmx" % vm)

    def set_sound(self, vmx, on):
        """Give the VM a sound device (as Fusion's Get Windows does), or take it away."""
        text = "\n".join(line for line in vmx.read_text().splitlines() if not line.startswith("sound."))
        vmx.write_text(text + ('\nsound.present = "TRUE"\nsound.virtualDev = "hdaudio"\n' if on else '\nsound.present = "FALSE"\n'))

    def assertNoSound(self, vmx):
        text = Path(vmx).read_text()
        self.assertIn('sound.present = "FALSE"', text)
        self.assertNotIn('sound.present = "TRUE"', text)


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
        records["ubuntu-26.04"].update(dict({"provisioned": 6}, **record))
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
        self.assertRegex(r.out, r"ok\s+linux: Base guest ubuntu-26.04: provisioned \(v6\)")
        self.assertRegex(r.out, r"info\s+linux: Clone: none yet")

    def test_a_base_guest_provisioned_by_an_older_vmlab_is_a_warning(self):
        self.project.config(FUSION_LAB)
        self.ready_base(provisioned=1)

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+linux: Base guest ubuntu-26.04: provisioned by an older vmlab \(v1; this one provisions v6\)")

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

    def test_a_running_base_guest_is_a_warning_that_base_create_fixes_without_a_window(self):
        # A Base guest runs headless: its fix must not need its desktop.
        self.project.config(FUSION_LAB)
        self.ready_base()
        self.set_state(self.base_vmx_path(), running=True)

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+linux: Base guest ubuntu-26.04: running: Labs cannot clone it while it runs")
        self.assertIn("fix: vmlab base create ubuntu-26.04", r.out)

        r = self.vmlab("base", "create", "ubuntu-26.04")

        self.assertExit(r, 0)
        self.assertIn("shutting vmlab-base-ubuntu-26.04 down", r.out)
        self.assertEqual(len(self.calls("stop")), 1)
        self.assertNotIn("Base guest ubuntu-26.04: running", self.vmlab("doctor").out)

    def test_a_running_base_guest_is_not_cloned_and_up_says_how_to_stop_it(self):
        self.project.config(FUSION_LAB)
        self.ready_base()
        self.set_state(self.base_vmx_path(), running=True)

        r = self.vmlab("up")

        self.assertExit(r, 1)
        self.assertIn("Base guest ubuntu-26.04 is running", r.err)
        self.assertIn("vmlab base create ubuntu-26.04", r.err)
        self.assertEqual(self.clones(), {})

    def test_a_clone_for_another_session_is_made_again_at_the_next_up(self):
        self.project.config(FUSION_LAB)
        self.ready_base()
        self.vmlab("up")  # no Guest really boots here; the clone is made
        self.vmlab("down")
        self.project.config(FUSION_LAB + '[labs.linux.fusion]\nsession = "x11"\n')

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"info\s+linux: Clone: made for the Wayland session")
        self.assertIn("vmlab up linux", r.out)

    def test_a_base_guest_with_a_sound_device_is_a_warning_that_base_create_fixes(self):
        self.project.config(FUSION_LAB)
        self.ready_base()
        self.set_sound(self.base_vmx_path(), True)

        r = self.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"warn\s+Host: Guest sound: Base guest ubuntu-26.04 has a sound device: it plays through the Host's speakers")
        self.assertIn("fix: vmlab base create ubuntu-26.04", r.out)

        r = self.vmlab("base", "create", "ubuntu-26.04")

        self.assertExit(r, 0)
        self.assertIn("sound device off", r.out)
        self.assertNoSound(self.base_vmx_path())
        self.assertNotIn("Guest sound", self.vmlab("doctor").out)

    def test_base_create_shuts_a_running_base_guest_down_to_take_its_sound_device_away(self):
        # Fusion rewrites a running VM's .vmx, and a headless Base guest has no window to shut it down in.
        self.project.config(FUSION_LAB)
        self.ready_base()
        self.set_sound(self.base_vmx_path(), True)
        state = json.loads(self.state_path.read_text())
        for vm in state["vms"].values():
            vm["running"] = True
        self.state_path.write_text(json.dumps(state))

        r = self.vmlab("base", "create", "ubuntu-26.04")

        self.assertExit(r, 0)
        self.assertIn("shutting vmlab-base-ubuntu-26.04 down", r.out)
        self.assertEqual(len(self.calls("stop")), 1)
        self.assertNoSound(self.base_vmx_path())

    def test_a_lab_with_a_sound_device_is_a_warning_until_its_next_start(self):
        self.project.config(FUSION_LAB)
        self.ready_base()
        self.vmlab("up")
        [clone] = self.clones()
        self.set_sound(Path(clone), True)  # e.g. a Lab cloned by an older vmlab

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+Host: Guest sound: Lab linux of \S+ has a sound device")
        self.assertRegex(r.out, r"fix: in \S+: vmlab down linux && vmlab up linux")

        self.vmlab("down")
        self.assertRegex(self.vmlab("doctor").out, r"fix: in \S+: vmlab up linux ")

        self.vmlab("up")
        self.assertNotIn("Guest sound", self.vmlab("doctor").out)

    def test_every_fusion_vm_of_vmlab_is_checked_for_sound_not_only_this_projects(self):
        # A Base guest no Lab here uses, one whose provisioning did not finish (its Lab's own
        # checks stop there), and a clone no known Lab needs.
        self.project.config(FUSION_LAB)
        self.ready_base()
        self.set_sound(self.base_vmx_path(), True)
        self.base_guest(name="ubuntu-24.04")
        self.set_sound(self.base_vmx_path("ubuntu-24.04"), True)
        records = json.loads((self.project.home / "bases.json").read_text())
        records["ubuntu-26.04"]["provisioned"] = None
        (self.project.home / "bases.json").write_text(json.dumps(records))
        orphan = self.project.home / "fusion" / "vmlab-gone-0000-linux.vmwarevm" / "vmlab-gone-0000-linux.vmx"
        orphan.parent.mkdir(parents=True)
        orphan.write_text('displayName = "vmlab-gone-0000-linux"\n')
        self.set_sound(orphan, True)

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"FAIL\s+linux: Base guest ubuntu-26.04: created, but its install or provisioning did not finish")
        self.assertRegex(r.out, r"warn\s+Host: Guest sound: Base guest ubuntu-26.04 has a sound device")
        self.assertRegex(r.out, r"warn\s+Host: Guest sound: Base guest ubuntu-24.04 has a sound device")
        self.assertRegex(r.out, r"warn\s+Host: Guest sound: VM vmlab-gone-0000-linux has a sound device")
        self.assertIn("fix: vmlab clean", r.out)

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

    def test_deploy_prints_the_clone_the_boot_and_the_wait_for_channels(self):
        r = self.vmlab("deploy")  # never reachable here: the wait for Channels fails

        self.assertExit(r, 1)
        lines = [line.split(" done in ")[0] for line in r.out.splitlines() if line.startswith("linux: ")]
        self.assertEqual(lines, ["linux: cloning", "linux: cloning", "linux: booting", "linux: booting", "linux: waiting for Channels"])

    def test_a_guest_that_never_reached_its_os_is_not_called_slow(self):
        r = self.vmlab("up")  # VMware Tools never answer: the OS did not come up

        self.assertExit(r, 1)
        self.assertIn("Guest linux did not reach its OS within 1s of starting: VMware Tools never answered", r.err)
        self.assertIn("vmlab ui screenshot --lab linux", r.err)
        self.assertNotIn("boot_timeout", r.err)

    def test_a_guest_whose_os_is_up_but_channels_are_not_may_just_be_slow(self):
        self.vmlab("up")
        [clone] = self.clones()
        self.set_state(clone, tools="running", ip="192.168.64.9")

        r = self.vmlab("up")

        self.assertExit(r, 1)
        self.assertIn("Guest linux's OS is up (VMware Tools answer, IP 192.168.64.9), but its Channels did not answer within 1s", r.err)
        self.assertIn("raise labs.linux.boot_timeout if it is just slow", r.err)

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

    def test_a_run_on_a_stopped_clone_of_an_older_base_makes_it_again_without_reverting_it(self):
        # A Suite run on a stopped Guest boots it only by its first restore of Clean state.
        self.vmlab("up")
        self.vmlab("down")
        self.base_guest("second")
        self.project.scenario("ok.py", 'def scenario(g):\n    g.check("ok", True)\n')

        self.vmlab("run")  # never reachable here: the wait for Channels fails

        self.assertEqual(self.calls("revertToSnapshot"), [], "the old clone is deleted, not reverted first")
        self.assertEqual(len(self.calls("deleteVM")), 1)
        self.assertIn("-snapshot=vmlab-provisioned-second", self.calls("clone")[-1])

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

    def test_a_run_on_a_clone_without_clean_state_makes_it_again(self):
        self.project.config(FUSION_LAB + '[labs.linux.fusion]\nsession = "x11"\n')
        self.vmlab("up")  # leaves a clone whose session switch did not finish: no vmlab-clean
        self.project.scenario("ok.py", 'def scenario(g):\n    g.check("ok", True)\n')

        r = self.vmlab("run")

        self.assertNotIn("Invalid snapshot", r.out + r.err)
        self.assertIn("X11 session", r.out + r.err, "it got as far as switching the new clone's session")
        self.assertEqual(len(self.calls("deleteVM")), 1, "a clone without Clean state is made again")

    def test_changing_the_session_recreates_the_clone(self):
        self.vmlab("up")
        self.vmlab("down")
        self.project.config(FUSION_LAB + '[labs.linux.fusion]\nsession = "x11"\n')

        self.vmlab("up")

        self.assertEqual(len(self.calls("deleteVM")), 1)
        self.assertEqual(len(self.calls("clone")), 2)

    def test_the_clone_starts_without_a_sound_device(self):
        # A Guest must not play through the Host's speakers or take its Bluetooth headset. Reverting
        # to a snapshot brings back the sound of the moment it was taken: turned off at every start.
        self.set_sound(self.base_vmx_path(), True)

        self.vmlab("up")
        self.vmlab("down")
        [clone] = self.clones()
        self.set_sound(Path(clone), True)
        self.vmlab("up")

        self.assertNoSound(clone)

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

    def test_a_running_leftover_clone_is_stopped_and_deleted_when_confirmed(self):
        # No Lab needs it: no `vmlab down` can stop it, and clean needs no hint to do it itself.
        other = self.other_project()
        clone = self.clone_for(cwd=other)
        shutil.rmtree(str(other))
        self.set_state(clone, running=True)

        r = self.vmlab("clean")  # no terminal: list only
        self.assertIn("running: to stop and delete", r.out)
        self.assertNotIn("vmrun stop", r.out)
        self.assertTrue(clone.exists())

        before = len(self.raw_calls())
        r = self.vmlab("clean", "--yes")
        self.assertExit(r, 0)
        on_clone = [c[0] for c in map(_without_auth, self.raw_calls()[before:]) if c[0] in ("stop", "deleteVM") and Path(c[1]).resolve() == clone.resolve()]
        self.assertEqual(on_clone, ["stop", "deleteVM"])
        self.assertFalse(clone.parent.exists())

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

    def test_a_running_unused_base_guest_is_left_alone_with_a_fix_that_needs_no_window(self):
        self.clone_for()
        self.base_guest(name="ubuntu-24.04")
        self.set_state(self.base_vmx_path("ubuntu-24.04"), running=True)

        r = self.vmlab("clean", "--yes", "--bases")

        self.assertExit(r, 0)
        self.assertIn("running: left alone (vmlab base create ubuntu-24.04", r.out)
        self.assertTrue(self.base_vmx_path("ubuntu-24.04").exists())

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


class FusionOldSnapshotsTest(FusionTestCase):
    """A re-provisioned Base guest keeps its earlier vmlab-provisioned-* snapshots only while a
    linked Lab clone still needs one: `base create` deletes the rest after asking, `vmlab clean`
    too, and doctor warns about them."""

    def setUp(self):
        super().setUp()
        self.project.config(FUSION_LAB)
        self.base_guest("first")

    def reprovisioned(self, provisioned_id="second"):
        """The Base guest provisioned again, as `base create` leaves it: a new snapshot, the old ones kept."""
        old = self.snapshots(self.base_vmx_path())
        self.base_guest(provisioned_id)
        path = self.project.home / "bases.json"
        records = json.loads(path.read_text())
        records["ubuntu-26.04"]["provisioned"] = 6  # this vmlab's PROVISION_VERSION: `base create` finds it ready (as ready_base)
        path.write_text(json.dumps(records))
        self.set_state(self.base_vmx_path(), snapshots=old + ["vmlab-provisioned-%s" % provisioned_id])

    def test_base_create_deletes_earlier_snapshots_no_lab_needs_when_told_yes(self):
        self.reprovisioned()

        r = self.vmlab("base", "create", "ubuntu-26.04", "--yes")

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-second"])
        self.assertIn("deleted snapshot vmlab-provisioned-first", r.out)
        self.assertIn("GB free", r.out)

    def test_without_a_terminal_nothing_is_deleted_and_it_says_how(self):
        self.reprovisioned()

        r = self.vmlab("base", "create", "ubuntu-26.04")

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-first", "vmlab-provisioned-second"])
        self.assertIn("vmlab-provisioned-first", r.out)
        self.assertIn("--yes", r.out)
        self.assertIn("VMLAB_DELETE_OLD_SNAPSHOTS", r.out)

    def test_the_setting_turns_the_question_off(self):
        self.reprovisioned()

        r = self.vmlab("base", "create", "ubuntu-26.04", env={"VMLAB_DELETE_OLD_SNAPSHOTS": "1"})

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-second"])

    def test_a_snapshot_a_lab_clone_was_made_from_stays_and_the_lab_is_named(self):
        self.vmlab("up")
        self.vmlab("down")
        self.reprovisioned()

        r = self.vmlab("base", "create", "ubuntu-26.04", "--yes")

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-first", "vmlab-provisioned-second"])
        self.assertRegex(r.out, r"kept snapshot vmlab-provisioned-first: Lab linux of \S+ still needs it")
        self.assertIn("vmlab up linux", r.out)

        self.vmlab("up")  # re-clones the Lab from the new snapshot
        self.vmlab("down")
        self.vmlab("base", "create", "ubuntu-26.04", "--yes")

        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-second"])

    def test_a_running_lab_clone_is_told_to_restart(self):
        self.vmlab("up")
        self.reprovisioned()

        r = self.vmlab("base", "create", "ubuntu-26.04", "--yes")

        self.assertIn("vmlab down linux && vmlab up linux", r.out)

    def test_a_linked_clone_fusion_lists_keeps_its_snapshot_even_without_a_record(self):
        # Fusion's own list of a snapshot's linked clones, in the Base guest's .vmsd
        orphan = self.project.home / "fusion" / "vmlab-gone-0000-linux.vmwarevm" / "vmlab-gone-0000-linux.vmx"
        orphan.parent.mkdir(parents=True)
        orphan.write_text('displayName = "vmlab-gone-0000-linux"\n')
        gone = self.project.home / "fusion" / "vmlab-deleted-0000-linux.vmwarevm" / "vmlab-deleted-0000-linux.vmx"
        self.reprovisioned("third")
        self.base_vmx_path().with_suffix(".vmsd").write_text(
            'snapshot0.uid = "1"\nsnapshot0.displayName = "vmlab-provisioned-first"\nsnapshot0.clone0 = "%s"\n'
            'snapshot1.uid = "2"\nsnapshot1.displayName = "vmlab-provisioned-third"\nsnapshot1.clone0 = "%s"\n' % (orphan, gone)
        )
        self.set_state(self.base_vmx_path(), snapshots=["vmlab-provisioned-first", "vmlab-provisioned-second", "vmlab-provisioned-third"])

        r = self.vmlab("base", "create", "ubuntu-26.04", "--yes")

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-first", "vmlab-provisioned-third"])
        self.assertIn("VM vmlab-gone-0000-linux still needs it", r.out)
        self.assertIn("vmlab clean", r.out)

    def test_fusions_stale_entry_for_a_lab_cloned_again_at_the_same_path_holds_nothing(self):
        # Fusion's .vmsd never forgets a linked clone; the Lab's record says what its clone is made from now.
        self.vmlab("up")
        self.vmlab("down")
        [clone] = self.clones()
        self.reprovisioned()
        self.vmlab("up")
        self.vmlab("down")
        self.base_vmx_path().with_suffix(".vmsd").write_text(
            'snapshot0.uid = "1"\nsnapshot0.displayName = "vmlab-provisioned-first"\nsnapshot0.clone0 = "%s"\n' % clone
        )

        r = self.vmlab("base", "create", "ubuntu-26.04", "--yes")

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-second"])

    def test_the_parent_disk_of_a_clone_without_a_record_says_which_snapshot_it_needs(self):
        orphan = self.project.home / "fusion" / "vmlab-gone-0000-linux.vmwarevm" / "vmlab-gone-0000-linux.vmx"
        orphan.parent.mkdir(parents=True)
        orphan.write_text('displayName = "vmlab-gone-0000-linux"\n')
        folder = self.base_vmx_path().parent
        (orphan.parent / "disk-cl1.vmdk").write_bytes(b'KDMV\x01\x00\x00\x00# Disk DescriptorFile\nparentFileNameHint="%s"\n' % str(folder / "disk-000001.vmdk").encode())
        self.reprovisioned("second")
        self.reprovisioned("third")
        self.base_vmx_path().with_suffix(".vmsd").write_text(
            'snapshot0.uid = "1"\nsnapshot0.displayName = "vmlab-provisioned-first"\nsnapshot0.disk0.fileName = "disk.vmdk"\n'
            'snapshot0.clone0 = "%s"\n'
            'snapshot1.uid = "2"\nsnapshot1.displayName = "vmlab-provisioned-second"\nsnapshot1.disk0.fileName = "disk-000001.vmdk"\n' % orphan
        )

        r = self.vmlab("base", "create", "ubuntu-26.04", "--yes")

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-second", "vmlab-provisioned-third"])

    def test_base_create_shuts_a_running_base_guest_down_before_deleting_them(self):
        self.reprovisioned()
        self.set_state(self.base_vmx_path(), running=True)

        r = self.vmlab("base", "create", "ubuntu-26.04", "--yes")

        self.assertExit(r, 0)
        self.assertIn("shutting vmlab-base-ubuntu-26.04 down", r.out)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-second"])

    def test_clean_leaves_them_alone_while_the_base_guest_runs_and_says_how_to_stop_it(self):
        self.reprovisioned()
        self.set_state(self.base_vmx_path(), running=True)

        r = self.vmlab("clean", "--yes")

        self.assertExit(r, 0)
        self.assertEqual(len(self.snapshots(self.base_vmx_path())), 2)
        self.assertIn("running: left alone (vmlab base create ubuntu-26.04", r.out)

    def test_doctor_warns_and_clean_deletes_them(self):
        self.reprovisioned()

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+Host: Old snapshots: Base guest ubuntu-26.04 keeps 1 earlier snapshot no Lab needs: vmlab-provisioned-first")
        self.assertIn("fix: vmlab clean", r.out)

        r = self.vmlab("clean")  # no terminal: list only
        self.assertIn("vmlab-provisioned-first", r.out)
        self.assertEqual(len(self.snapshots(self.base_vmx_path())), 2)

        r = self.vmlab("clean", "--yes")

        self.assertExit(r, 0)
        self.assertIn("deleted snapshot vmlab-base-ubuntu-26.04 vmlab-provisioned-first", r.out)
        self.assertEqual(self.snapshots(self.base_vmx_path()), ["vmlab-provisioned-second"])
        self.assertNotIn("Old snapshots", self.vmlab("doctor").out)

    def test_clean_keeps_a_snapshot_a_lab_needs_and_says_why(self):
        self.vmlab("up")
        self.vmlab("down")
        self.reprovisioned()

        r = self.vmlab("clean", "--yes")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"kept: Lab linux of \S+ still needs it")
        self.assertEqual(len(self.snapshots(self.base_vmx_path())), 2)
        self.assertNotIn("Old snapshots", self.vmlab("doctor").out)
