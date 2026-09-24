"""Windows Labs on the Fusion Provider, and the wizard behind `vmlab base create windows-11`,
against the scripted vmrun of test_fusion.

What needs a real Windows Guest (the one UAC click, provisioning, both Channels
reaching the desktop) is covered by Seam 2: tests/contract/test_contract.py
with a Windows Lab.
"""

import json
import platform
import stat
import unittest

from pathlib import Path

from test_fusion import FusionTestCase

WINDOWS_LAB = """
[labs.win]
provider = "fusion"
os = "windows"
boot_timeout = 1
"""
VM_PASSWORD = "vm-secret"


class WindowsTestCase(FusionTestCase):
    def setUp(self):
        super().setUp()
        state = json.loads(self.state_path.read_text())
        state["vm_password"] = VM_PASSWORD
        self.state_path.write_text(json.dumps(state))

    def windows_vm(self, folder, name="Windows 11", guest_os="arm-windows11-64", snapshots=()):
        """A VM as Fusion makes it: encrypted for its TPM, with a .vmsd listing its snapshots."""
        vmx = folder / ("%s.vmwarevm" % name) / ("%s.vmx" % name)
        vmx.parent.mkdir(parents=True, exist_ok=True)
        vmx.write_text(
            'displayName = "%s"\nguestOS = "%s"\nvmx.encryptionType = "partial"\nencryption.keySafe = "vmware:key/..."\n'
            'ethernet0.generatedAddress = "00:0c:29:7c:6c:93"\n' % (name, guest_os)
        )
        (vmx.parent / ("%s.vmsd" % name)).write_text("\n".join(snapshots))
        (vmx.parent / "disk.vmdk").write_text("disk")
        return vmx

    def windows_base(self, provisioned_id="first", earlier=()):
        """A provisioned Windows Base guest, as the wizard leaves it; earlier: the ids of provisionings before."""
        vm = "vmlab-base-windows-11"
        snapshot = "vmlab-provisioned-%s" % provisioned_id
        earlier = ["vmlab-provisioned-%s" % each for each in earlier]
        vmx = self.windows_vm(self.project.home / "fusion", name=vm, snapshots=["clean"] + earlier + [snapshot])
        (self.project.home / "fusion" / (vm + ".credentials.json")).write_text(
            json.dumps({"user": "tester", "password": "win-secret", "vm_password": VM_PASSWORD})
        )
        path = self.project.home / "bases.json"
        records = json.loads(path.read_text()) if path.exists() else {}
        records["windows-11"] = {
            "provider": "fusion", "os": "windows", "arch": "arm64", "vm": vm, "vmx": str(vmx), "image": "/somewhere/Windows 11.vmx",
            "user": "tester", "installed": True, "provisioned": 3, "provisioned_id": provisioned_id, "snapshot": snapshot, "elevated": True,
        }  # fmt: skip
        path.write_text(json.dumps(records))
        return vmx

    def calls_on(self, vmx):
        """Every logged vmrun call, with its passwords, that names the VM vmx."""
        folder = vmx.rsplit("/", 2)[-2]
        return [c for c in self.raw_calls() if any(folder in a for a in c)]


class WindowsLabConfigTest(WindowsTestCase):
    def test_a_windows_lab_loads_with_its_own_defaults(self):
        self.project.config(WINDOWS_LAB)

        r = self.vmlab("doctor")

        self.assertExit(r, 1)
        self.assertIn("vmlab base create windows-11", r.out)
        self.assertNotIn("downloads", r.out, "Fusion gets Windows; vmlab downloads nothing")
        self.assertIn("Get Windows from Microsoft", r.out)

    def test_the_session_is_for_linux_labs_only(self):
        self.project.config(WINDOWS_LAB + '[labs.win.fusion]\nsession = "x11"\n')

        r = self.vmlab("status")

        self.assertExit(r, 2)
        self.assertIn("labs.win.fusion.session", r.err)
        self.assertIn("unknown key for a Windows Lab", r.err)

    def test_the_display_language_is_a_language_tag(self):
        self.project.config(WINDOWS_LAB + '[labs.win.fusion]\nlanguage = "English"\n')

        r = self.vmlab("status")

        self.assertExit(r, 2)
        self.assertIn("labs.win.fusion.language", r.err)
        self.assertIn('language = "en-US"', r.err)


class WindowsDoctorTest(WindowsTestCase):
    def setUp(self):
        super().setUp()
        self.project.config(WINDOWS_LAB)
        self.windows_base()
        ssh = self.project.home / "ssh"
        ssh.mkdir(exist_ok=True)
        (ssh / "known_hosts").write_text("vmlab-base-windows-11 ssh-ed25519 AAAA\n")

    def set_record(self, **fields):
        path = self.project.home / "bases.json"
        records = json.loads(path.read_text())
        records["windows-11"].update(fields)
        path.write_text(json.dumps(records))

    def test_a_ready_windows_base_guest(self):
        r = self.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"ok\s+win: Base guest windows-11: provisioned \(v3\)")

    def test_a_base_guest_provisioned_by_an_older_vmlab_is_a_warning(self):
        self.set_record(provisioned=2)

        r = self.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"warn\s+win: Base guest windows-11: provisioned by an older vmlab \(v2; this one provisions v3\)")
        self.assertIn("fix: vmlab base create windows-11", r.out)

    def test_a_guest_that_asks_for_elevation_is_a_warning(self):
        self.set_record(elevated=False)

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+win: Elevation: ")
        self.assertIn("vmlab base create windows-11", r.out)

    def test_a_base_guest_in_the_labs_display_language(self):
        self.set_record(language="en-US")

        r = self.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"ok\s+win: Display language: en-US")

    def test_a_base_guest_in_another_display_language_is_a_warning_until_the_guest_runs(self):
        self.set_record(language="de-DE")

        r = self.vmlab("doctor")

        self.assertExit(r, 0)  # the Guest can start; its running check fails
        self.assertRegex(r.out, r"warn\s+win: Display language: .*de-DE.*en-US")
        self.assertIn('language = "de-DE"', r.out)
        self.assertIn("vmlab base create windows-11 --image", r.out)
        self.assertRegex(r.out, r"info\s+win: Guest: stopped", "the checks after it still run")

    def test_a_lab_can_name_another_display_language(self):
        self.project.config(WINDOWS_LAB + '[labs.win.fusion]\nlanguage = "de-DE"\n')
        self.set_record(language="de-de")

        r = self.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"ok\s+win: Display language: de-de")

    def test_a_base_guest_provisioned_before_languages_were_recorded_is_checked_while_it_runs(self):
        r = self.vmlab("doctor")

        self.assertExit(r, 0)
        self.assertRegex(r.out, r"info\s+win: Display language: .*while the Guest runs")
        self.assertIn("vmlab base create windows-11 --reprovision", r.out)

    def test_missing_credentials_fail_since_vmrun_cannot_open_the_vm(self):
        (self.project.home / "fusion" / "vmlab-base-windows-11.credentials.json").unlink()

        r = self.vmlab("doctor")

        self.assertExit(r, 1)
        self.assertRegex(r.out, r"FAIL\s+win: Guest credentials: .*missing")


class WindowsCloneTest(WindowsTestCase):
    """A Windows Lab runs in an APFS copy of its Base guest: vmrun cannot clone an encrypted VM."""

    def setUp(self):
        super().setUp()
        self.project.config(WINDOWS_LAB)
        self.base_vmx = self.windows_base()

    def clone_vmx(self):
        [path] = self.clones()
        return path

    def test_up_copies_the_base_guest_reverts_it_to_the_provisioned_snapshot_and_starts_it(self):
        self.vmlab("up")  # never reachable here: no Guest really boots

        self.assertEqual(self.calls("clone"), [])
        clone = self.clone_vmx()
        vm = self.clones()[clone]
        self.assertTrue(clone.startswith(str((self.project.home / "fusion").resolve())))
        self.assertTrue(clone.endswith("-win.vmwarevm/%s.vmx" % clone.rsplit("/", 1)[1][:-4]), "files are named after the clone")
        self.assertEqual(vm["reverted_to"], ["vmlab-provisioned-first"])
        self.assertIn("vmlab-clean", vm["snapshots"])
        self.assertTrue(vm["running"])
        with open(clone) as f:
            text = f.read()
        self.assertIn('uuid.action = "create"', text)
        self.assertNotIn("generatedAddress", text, "the copy gets its own MAC address")
        self.assertTrue(self.base_vmx.exists())

    def test_the_copy_starts_without_the_sound_device_fusions_windows_vm_has(self):
        self.set_sound(self.base_vmx, True)

        self.vmlab("up")

        self.assertNoSound(self.clone_vmx())

    def test_every_vmrun_call_on_the_copy_carries_the_vm_password(self):
        self.vmlab("up")
        self.vmlab("down")

        calls = self.calls_on(self.clone_vmx())
        self.assertTrue(calls)
        self.assertEqual([c for c in calls if c[:2] != ["-vp", VM_PASSWORD]], [])

    def test_the_copy_is_reused_while_the_base_guest_is_unchanged_and_made_again_after_reprovisioning(self):
        self.vmlab("up")
        self.vmlab("down")
        self.vmlab("up")
        self.vmlab("down")
        self.assertEqual(len(self.calls("deleteVM")), 0)

        self.windows_base("second")
        self.vmlab("up")

        self.assertEqual(len(self.calls("deleteVM")), 1)
        self.assertEqual(self.clones()[self.clone_vmx()]["reverted_to"], ["vmlab-provisioned-second"])

    def test_clean_deletes_an_orphaned_copy_with_the_vm_password(self):
        self.vmlab("up")
        self.vmlab("down")
        clone = self.clone_vmx()
        self.project.config(WINDOWS_LAB.replace("[labs.win]", "[labs.other]"))

        r = self.vmlab("clean", "--yes")

        self.assertExit(r, 0)
        [delete] = [c for c in self.raw_calls() if "deleteVM" in c]
        self.assertEqual(delete[:2], ["-vp", VM_PASSWORD])
        self.assertNotIn(clone, self.vms())


class WindowsOldSnapshotsTest(WindowsTestCase):
    """A Windows Lab is a copy with no linked clones: its copy of the Base guest's earlier
    provisioned snapshots is never needed."""

    def setUp(self):
        super().setUp()
        self.project.config(WINDOWS_LAB)
        self.base_vmx = self.windows_base("third", earlier=("first", "second"))

    def test_a_new_copy_has_no_earlier_provisioned_snapshots(self):
        self.vmlab("up")

        [clone] = self.clones()
        self.assertEqual(self.snapshots(clone), ["clean", "vmlab-provisioned-third", "vmlab-clean"])
        [delete] = [c for c in self.raw_calls() if "deleteSnapshot" in c][:1]
        self.assertEqual(delete[:2], ["-vp", VM_PASSWORD])

    def test_base_create_deletes_the_base_guests_with_its_password(self):
        r = self.vmlab("base", "create", "windows-11", "--yes")

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(self.base_vmx), ["clean", "vmlab-provisioned-third"])
        self.assertTrue(all(c[:2] == ["-vp", VM_PASSWORD] for c in self.raw_calls() if "deleteSnapshot" in c))

    def test_doctor_warns_about_a_copy_made_by_an_older_vmlab_and_clean_deletes_them(self):
        self.vmlab("up")
        self.vmlab("down")
        [clone] = self.clones()
        self.set_state(clone, snapshots=["clean", "vmlab-provisioned-first", "vmlab-provisioned-third", "vmlab-clean"])

        r = self.vmlab("doctor")

        self.assertRegex(r.out, r"warn\s+Host: Old snapshots: Lab win of \S+ keeps 1 earlier snapshot no Lab needs: vmlab-provisioned-first")
        self.assertRegex(r.out, r"warn\s+Host: Old snapshots: Base guest windows-11 keeps 2 earlier snapshots")

        r = self.vmlab("clean", "--yes")

        self.assertExit(r, 0)
        self.assertEqual(self.snapshots(clone), ["clean", "vmlab-provisioned-third", "vmlab-clean"])
        self.assertEqual(self.snapshots(self.base_vmx), ["clean", "vmlab-provisioned-third"])
        self.assertNotIn("Old snapshots", self.vmlab("doctor").out)


class WindowsBaseWizardTest(WindowsTestCase):
    """Without a terminal the wizard stops at the first step that does not hold, and says what to do."""

    def keychain(self, password):
        """A stand-in for `security` that knows the password of every VM (or of none)."""
        security = self.project.root / "fake-security"
        security.write_text("#!/bin/sh\n%s\n" % ("echo '%s'" % password if password else "exit 44"))
        security.chmod(security.stat().st_mode | stat.S_IXUSR)
        return security

    def set_base_record(self, **fields):
        path = self.project.home / "bases.json"
        records = json.loads(path.read_text())
        records["windows-11"].update(fields)
        path.write_text(json.dumps(records))

    def wizard(self, *args, security=None, open_stub=None):
        env = {"VMLAB_SECURITY": str(security or self.keychain(None))}
        if open_stub:
            env["VMLAB_OPEN"] = str(open_stub)
        return self.project.vmlab("base", "create", "windows-11", *args, env=dict(env, **{
            "VMLAB_VMRUN": str(self.vmrun), "FAKE_VMRUN_STATE": str(self.state_path), "FAKE_VMRUN_LOG": str(self.log_path),
        }))  # fmt: skip

    def test_without_a_windows_vm_it_explains_fusions_get_windows_flow(self):
        r = self.wizard()

        self.assertExit(r, 1)
        self.assertIn("Get Windows from Microsoft", r.out)
        self.assertIn("Only the files\n     needed to support a TPM", r.out)
        self.assertIn("Install VMware Tools", r.out, "Fusion's Get Windows flow leaves them out; vmrun needs them")
        self.assertIn("no Windows VM in Fusion's folders", r.err)
        self.assertIn("in a terminal window of your own", r.err)
        self.assertIn("Claude Code's `!`", r.err, "an agent's shell cannot answer it either")

    def test_a_vm_that_is_not_windows_is_refused(self):
        vmx = self.windows_vm(self.project.root, name="Ubuntu", guest_os="arm-ubuntu-64")

        r = self.wizard("--image", vmx.parent)

        self.assertExit(r, 1)
        self.assertIn("is not a Windows VM", r.err)

    @unittest.skipUnless(platform.machine() == "arm64", "an arm64 Host")
    def test_an_x64_windows_vm_is_refused_on_an_arm_mac(self):
        vmx = self.windows_vm(self.project.fake_user_home / "Virtual Machines.localized", guest_os="windows11-64")

        r = self.wizard()

        self.assertExit(r, 1)
        self.assertIn("x64 VM", r.err)

    def test_the_vm_password_comes_from_the_keychain_and_is_checked(self):
        self.windows_vm(self.project.fake_user_home / "Virtual Machines.localized")

        r = self.wizard(security=self.keychain("wrong"))
        self.assertExit(r, 1)
        self.assertIn("that password does not open the VM", r.err)

        r = self.wizard(security=self.keychain(VM_PASSWORD))
        self.assertExit(r, 1)
        self.assertIn("ok: The VM's encryption password", r.out)
        self.assertIn("The Windows account", r.err, "the next step needs a person")

    def test_nothing_is_copied_before_every_step_holds(self):
        self.windows_vm(self.project.fake_user_home / "Virtual Machines.localized")

        self.wizard(security=self.keychain(VM_PASSWORD))

        self.assertFalse((self.project.home / "fusion" / "vmlab-base-windows-11.vmwarevm").exists())
        self.assertNotIn("windows-11", json.loads((self.project.home / "bases.json").read_text()) if (self.project.home / "bases.json").exists() else {})

    def test_another_image_is_adopted_in_place_of_a_ready_base_guest(self):
        # e.g. an English Windows VM replacing one installed in another display language
        base = self.windows_base()
        new = self.windows_vm(self.project.root / "elsewhere", name="Windows 11 en-US")

        r = self.wizard("--image", new.parent, security=self.keychain(VM_PASSWORD))

        self.assertExit(r, 1)
        self.assertIn("ok: The VM's encryption password", r.out)
        self.assertIn("The Windows account", r.err, "the next step needs a person")
        self.assertTrue(base.exists(), "nothing is replaced before every step holds")

    def test_the_adopted_image_again_leaves_a_ready_base_guest_alone(self):
        self.windows_base()

        r = self.wizard("--image", "/somewhere/Windows 11.vmx")

        self.assertExit(r, 0)
        self.assertIn("Base guest windows-11 is ready", r.out)

    def test_a_ready_base_guest_gets_its_sound_device_turned_off(self):
        base = self.windows_base()
        self.set_sound(base, True)

        r = self.wizard()

        self.assertExit(r, 0)
        self.assertIn("sound device off", r.out)
        self.assertNoSound(base)

    def test_provisioning_starts_the_base_guest_without_a_sound_device(self):
        base = self.windows_base()
        self.set_sound(base, True)
        self.set_base_record(provisioned=None, elevated=False)
        state = json.loads(self.state_path.read_text())
        state["gui_refused"] = True  # the wizard then stops right after trying to start it, with nobody there
        self.state_path.write_text(json.dumps(state))
        open_stub = self.project.root / "fake-open"
        open_stub.write_text("#!/bin/sh\n")
        open_stub.chmod(open_stub.stat().st_mode | stat.S_IXUSR)

        r = self.wizard(security=self.keychain(VM_PASSWORD), open_stub=open_stub)

        self.assertIn("Start the Guest in a Fusion window", r.err)
        self.assertNoSound(base)

    def test_a_guest_fusion_will_not_start_in_a_window_is_opened_in_fusion(self):
        # Fusion refuses `vmrun start gui` for an encrypted VM whose password it cannot read
        # from the Keychain; the person still needs a window for the UAC click.
        self.windows_base()
        self.set_base_record(provisioned=None, elevated=False)
        state = json.loads(self.state_path.read_text())
        state["gui_refused"] = True
        self.state_path.write_text(json.dumps(state))
        opened = self.project.root / "opened.txt"
        open_stub = self.project.root / "fake-open"
        open_stub.write_text('#!/bin/sh\necho "$@" >> "%s"\n' % opened)
        open_stub.chmod(open_stub.stat().st_mode | stat.S_IXUSR)

        r = self.wizard(security=self.keychain(VM_PASSWORD), open_stub=open_stub)

        self.assertExit(r, 1)
        self.assertIn("vmlab-base-windows-11.vmx", opened.read_text())
        self.assertIn("Always Allow", r.out)
        self.assertIn("Start the Guest in a Fusion window", r.err)

    def test_vmlabs_own_vm_is_never_adopted(self):
        # A Lab's clone is a Windows VM too; copying it onto itself would delete it.
        clone = self.windows_vm(self.project.home / "fusion", name="vmlab-app-1-win")

        r = self.wizard("--image", clone.parent, security=self.keychain(VM_PASSWORD))

        self.assertExit(r, 1)
        self.assertIn("vmlab's own VM", r.err)
        self.assertTrue(clone.exists())


class WindowsCleanTest(WindowsTestCase):
    """An encrypted copy is deleted even when what named its Base guest is gone."""

    def setUp(self):
        super().setUp()
        self.project.config(WINDOWS_LAB)
        self.windows_base()
        self.vmlab("up")
        self.vmlab("down")
        [self.clone] = self.clones()

    def test_a_copy_whose_record_is_gone_is_deleted_with_a_base_guests_password(self):
        record = next(p for p in (self.project.home / "fusion").glob("*-win.json"))
        record.unlink()

        r = self.vmlab("clean", "--yes")

        self.assertExit(r, 0)
        self.assertIn("its project is unknown", r.out)
        self.assertNotIn(self.clone, self.vms())
        [delete] = [c for c in self.raw_calls() if "deleteVM" in c]
        self.assertEqual(delete[:2], ["-vp", VM_PASSWORD])

    def test_a_copy_that_did_not_finish_is_made_again_instead_of_copied_into(self):
        bundle = Path(self.clone).parent
        for path in bundle.iterdir():
            if path.suffix in (".vmx", ".vmsd"):
                path.unlink()  # a copy interrupted before its files were renamed

        self.vmlab("up")

        self.assertTrue(Path(self.clone).is_file(), sorted(p.name for p in bundle.iterdir()))
        self.assertEqual(sorted(p.name for p in bundle.iterdir() if p.is_dir()), [])
