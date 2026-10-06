"""Installing a language pack into a Windows Base guest, and reading a Windows Guest's language
(vmlab.providers.fusion_windows, vmlab.home.BaseGuestLock, doctor's Language row).

These load the parts directly, against a stand-in Base guest VM and a stand-in elevated ssh session
that answers add-language.ps1 as Windows does; test_fusion_windows covers the Lab side through the
CLI, and the contract suite (tests/contract/test_contract.py) a real Windows Lab in ru-RU.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab import bases, doctor  # noqa: E402
from vmlab.home import BaseGuestLock  # noqa: E402
from vmlab.providers import fusion, fusion_windows  # noqa: E402
from vmlab.providers.base import FAIL, OK, ExecResult, GuestError  # noqa: E402

RECORD = {
    "provider": "fusion", "os": "windows", "arch": "arm64", "vm": "vmlab-base-windows-11", "vmx": "/vms/vmlab-base-windows-11.vmx",
    "user": "tester", "installed": True, "provisioned": 7, "provisioned_id": "ea5697763a604a6eb2da486b2fc2b0d9",
    "snapshot": "vmlab-provisioned-ea5697763a60", "language": "en-US", "elevated": True,
}  # fmt: skip


class StandInVM:
    """The Base guest's VM: records what is done to it."""

    def __init__(self, running=False):
        self.running, self.done = running, []
        self.vmx = Path(RECORD["vmx"])

    def is_running(self):
        return self.running

    def revert(self, name, timeout):
        self.done.append(("revert", name))

    def start(self, timeout=None):
        self.running = True
        self.done.append(("start",))

    def stop(self, timeout=None):
        self.running = False
        self.done.append(("stop",))

    def snapshot(self, name, timeout=None):
        self.done.append(("snapshot", name))

    def forget_ip(self):
        pass


class StandInSsh:
    """The elevated ssh session: answers add-language.ps1 with code, stdout and stderr."""

    def __init__(self, code=0, stdout="", stderr=""):
        self.answer = (code, stdout, stderr)
        self.scripts, self.closed = [], False

    def send_file(self, local, guest_path, timeout):
        self.scripts.append(Path(local).read_text(encoding="ascii"))

    def run_command(self, remote, argv, timeout, stdin):
        if remote == "cmd /c exit 0":
            return ExecResult(argv, 0, "", "")
        return ExecResult(argv, *self.answer)

    def close(self):
        self.closed = True


class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"VMLAB_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        bases.Registry().put("windows-11", dict(RECORD))


class AddLanguageTest(HomeTestCase):
    def add(self, vm, ssh):
        said = []
        try:
            record = fusion_windows.add_language("windows-11", "ru-RU", said.append, 60, vm=vm, ssh=ssh)
        except GuestError as exc:
            return said, None, exc
        return said, record, None

    def test_the_pack_goes_in_from_the_recorded_snapshot_and_a_new_snapshot_keeps_the_provisioning(self):
        vm = StandInVM()
        ssh = StandInSsh(0, "downloading and installing the ru-RU language pack\n  Windows Update is off, as before\nvmlab-languages: en-US ru-RU\n")

        said, record, error = self.add(vm, ssh)

        self.assertIsNone(error)
        snapshot = record["snapshot"]
        self.assertTrue(snapshot.startswith("vmlab-provisioned-ea5697763a60-"), "old_snapshots and `vmlab clean` count it as a provisioned snapshot")
        self.assertEqual(vm.done, [("revert", RECORD["snapshot"]), ("start",), ("stop",), ("snapshot", snapshot)])
        self.assertEqual(bases.Registry().get("windows-11"), dict(RECORD, snapshot=snapshot, languages=["ru-RU"]))
        self.assertEqual(bases.provisioning(record), bases.provisioning(RECORD), "copies made before stay current")
        self.assertIn("  Windows Update is off, as before", said)
        self.assertIn("Install-Language", ssh.scripts[0])
        self.assertTrue(ssh.closed)

    def test_a_failed_install_leaves_the_base_guest_stopped_at_its_snapshot_with_its_record_and_names_the_network(self):
        vm = StandInVM()
        ssh = StandInSsh(1, "downloading and installing the ru-RU language pack\n", "Install-Language : The service cannot be started\n")

        said, record, error = self.add(vm, ssh)

        self.assertIn("installing the ru-RU language pack into Base guest windows-11 failed", error.message)
        self.assertIn("The service cannot be started", error.message)
        self.assertIn("Windows Update", error.fix)
        self.assertIn("online", error.fix)
        self.assertEqual(vm.done, [("revert", RECORD["snapshot"]), ("start",), ("stop",), ("revert", RECORD["snapshot"])])
        self.assertFalse(vm.running)
        self.assertEqual(bases.Registry().get("windows-11"), RECORD)

    def test_a_pack_windows_does_not_list_afterwards_is_a_failure(self):
        said, record, error = self.add(StandInVM(), StandInSsh(0, "vmlab-languages: en-US\n"))

        self.assertIsNotNone(error)
        self.assertEqual(bases.Registry().get("windows-11"), RECORD)

    def test_a_base_guest_from_an_older_provisioning_is_provisioned_again_first(self):
        bases.Registry().put("windows-11", dict(RECORD, provisioned=5))
        vm = StandInVM()

        said, record, error = self.add(vm, StandInSsh())

        self.assertIn("older vmlab", error.message)
        self.assertIn("vmlab base create windows-11", error.fix)
        self.assertEqual(vm.done, [])

    def test_a_snapshot_that_fails_leaves_the_base_guest_stopped_at_its_snapshot(self):
        vm = StandInVM()

        def fail(name, timeout=None):
            raise GuestError("vmrun snapshot failed", "retry")

        vm.snapshot = fail

        said, record, error = self.add(vm, StandInSsh(0, "vmlab-languages: en-US ru-RU\n"))

        self.assertEqual(error.message, "vmrun snapshot failed")
        self.assertEqual(vm.done[-2:], [("stop",), ("revert", RECORD["snapshot"])])
        self.assertEqual(bases.Registry().get("windows-11"), RECORD)

    def test_a_running_base_guest_is_left_alone(self):
        vm = StandInVM(running=True)

        said, record, error = self.add(vm, StandInSsh())

        self.assertIn("running", error.message)
        self.assertEqual(vm.done, [])


class SetLanguageTest(unittest.TestCase):
    def test_the_copy_is_set_by_the_scripts_own_last_line(self):
        fusion_windows.set_language(StandInSsh(0, "vmlab-language-set: display language ru-RU, formats ru-RU\n"), "ru-RU")

    def test_a_script_that_never_ran_is_a_failure_although_powershell_exits_0(self):
        with self.assertRaises(GuestError) as caught:
            fusion_windows.set_language(StandInSsh(0, "", "The argument to the -File parameter does not exist."), "ru-RU")

        self.assertIn("-File parameter does not exist", caught.exception.message)


class BaseLanguagesTest(unittest.TestCase):
    def test_a_record_from_before_the_list_has_its_display_language_alone(self):
        self.assertEqual(fusion_windows.base_languages(RECORD), ["en-US"])
        self.assertTrue(fusion_windows.has_language(RECORD, "en-us"))
        self.assertFalse(fusion_windows.has_language(RECORD, "ru-RU"))

    def test_installed_packs_count(self):
        record = dict(RECORD, languages=["ru-RU"])

        self.assertEqual(fusion_windows.base_languages(record), ["en-US", "ru-RU"])
        self.assertTrue(fusion_windows.has_language(record, "ru-RU"))

    def test_provisioning_tells_the_display_language_as_information(self):
        note = fusion_windows.display_language_note("en-US", ["en-US", "ru-RU"])

        self.assertIn("display language en-US", note)
        self.assertIn("ru-RU", note)
        self.assertNotIn("expect", note)
        self.assertNotIn("warn", note.lower())


class BaseGuestLockTest(HomeTestCase):
    def test_a_second_holder_waits_for_the_first_and_says_so(self):
        order, waited = [], []
        first = BaseGuestLock("windows-11").__enter__()

        def second():
            with BaseGuestLock("windows-11", lambda: waited.append(True)):
                order.append("second")

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.3)
        order.append("first")
        first.__exit__(None, None, None)
        thread.join(5)

        self.assertEqual(order, ["first", "second"])
        self.assertEqual(waited, [True])

    def test_other_base_guests_do_not_wait(self):
        with BaseGuestLock("windows-11"):
            waited = []
            with BaseGuestLock("ubuntu-26.04", lambda: waited.append(True)):
                pass

        self.assertEqual(waited, [])


class RunningWindowsLanguageTest(HomeTestCase):
    """doctor's Language row reads a running Windows Guest's display language and regional formats."""

    def row(self, stdout, lab_language):
        provider = fusion.FusionProvider.__new__(fusion.FusionProvider)
        provider.lab = mock.Mock(os="windows", language=lab_language)
        provider.lab.name = "win"
        provider.exec = lambda argv, timeout: ExecResult(argv, 0, stdout, "")
        rows = []
        doctor._check_language(provider, provider.lab, True, lambda *row: rows.append(row))
        return rows[0]

    def test_a_guest_in_the_lab_language(self):
        self.assertEqual(self.row("ru-RU\r\nru-RU\r\n", "ru-RU")[:3], ("Language", OK, "ru-RU"))

    def test_formats_in_another_language_fail(self):
        check, status, detail, fix = self.row("ru-RU\r\nen-US\r\n", "ru-RU")

        self.assertEqual(status, FAIL)
        self.assertIn("the Guest shows ru-RU en-US", detail)
        self.assertEqual(fix, "vmlab down win && vmlab up win   (the clone is made again, in ru-RU)")


if __name__ == "__main__":
    unittest.main()
