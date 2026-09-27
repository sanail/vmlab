"""A Windows Guest's clock against the Host's: the wizard behind `vmlab base create windows-11`
checks it after provisioning, and `vmlab doctor` warns about it (vmlab.providers.fusion_windows).

Windows Guests keep their clock in UTC, as Linux and macOS Guests do: vmlab sets Fusion's
hardware clock to UTC (rtc.diffFromUTC), and provisioning makes Windows read it as UTC
(RealTimeIsUniversal) in time zone UTC, so the Mac's time zone never matters. These load the
parts directly, against a stand-in Guest that answers the clock query with a clock of its own;
the contract suite (tests/contract/test_contract.py) and doctor against a real Lab ask a real one.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab.providers import fusion, fusion_windows  # noqa: E402
from vmlab.providers.base import OK, WARN, ExecResult, GuestError  # noqa: E402

PACIFIC = "Pacific Standard Time"


class StandInGuest:
    """Answers the clock query as Windows does: its UTC time in ms, its time zone's id, and
    whether it reads its hardware clock as UTC (RealTimeIsUniversal, 0 when unset). ahead:
    seconds its clock runs ahead of the Host's."""

    def __init__(self, ahead=0, zone="UTC", universal=1):
        self.ahead, self.zone, self.universal = ahead, zone, universal

    def exec(self, argv, timeout, env=None, stdin=None):
        if argv != fusion_windows.CLOCK:
            return ExecResult(argv, 1, "", "stand-in: unsupported %r" % (argv,))
        now = int((time.time() + self.ahead) * 1000)
        return ExecResult(argv, 0, "%d\r\n%s\r\n%d\r\n" % (now, self.zone, self.universal), "")


class WizardClockTest(unittest.TestCase):
    def wizard(self, guest, rtc="0"):
        said = []
        vm = mock.Mock(vmx=Path("/vms/vmlab-base-windows-11.vmwarevm/vmlab-base-windows-11.vmx"))
        vm.config = lambda key: rtc if key == "rtc.diffFromUTC" else None
        try:
            fusion_windows.check_clock("windows-11", guest, vm, said.append)
        except GuestError as exc:
            return "\n".join(said), exc
        return "\n".join(said), None

    def test_a_guest_in_utc_whose_clock_matches_lets_it_go_on_without_asking(self):
        out, error = self.wizard(StandInGuest(ahead=3))

        self.assertIsNone(error)
        self.assertIn("ok: ", out)
        self.assertIn("UTC", out)

    def test_provisioning_that_left_windows_in_its_own_time_zone_stops_it_with_the_cause(self):
        out, error = self.wizard(StandInGuest(ahead=10 * 3600, zone=PACIFIC, universal=0))

        self.assertIsNotNone(error, "no snapshot of a Guest whose clock follows its own time zone")
        self.assertIn(PACIFIC, error.message)
        self.assertIn("10 h ahead", error.message)
        self.assertIn("provisioning", error.message)
        self.assertIn("vmlab base create windows-11 --reprovision", error.fix)
        self.assertNotIn("Settings", error.message + error.fix, "the person sets nothing in Windows")

    def test_a_hardware_clock_not_read_as_utc_is_the_same_cause(self):
        out, error = self.wizard(StandInGuest(zone="UTC", universal=0))

        self.assertIsNotNone(error)
        self.assertIn("reads its hardware clock as local time", error.message)
        self.assertIn("--reprovision", error.fix)

    def test_a_guest_in_utc_still_hours_off_names_the_vms_hardware_clock(self):
        out, error = self.wizard(StandInGuest(ahead=-3 * 3600), rtc="-10800")

        self.assertIn("3 h behind", error.message)
        self.assertIn('rtc.diffFromUTC = "-10800"', error.message)
        self.assertIn("vmlab base create windows-11 --reprovision", error.fix)

    def test_a_guest_in_utc_still_off_with_its_hardware_clock_in_utc_boots_again(self):
        out, error = self.wizard(StandInGuest(ahead=600))

        self.assertIn("10 min ahead", error.message)
        self.assertIn("hardware clock is UTC", error.message)
        self.assertIn("--reprovision", error.fix)

    def test_a_clock_it_cannot_read_is_not_taken_as_right(self):
        guest = StandInGuest()
        guest.exec = lambda argv, timeout, env=None, stdin=None: ExecResult(argv, 1, "", "powershell: boom")

        out, error = self.wizard(guest)

        self.assertIn("cannot read the Guest's clock", error.message)
        self.assertIn("boom", error.message)


class DoctorClockTest(unittest.TestCase):
    def diagnose(self, guest, provisioned=fusion_windows.PROVISION_VERSION):
        provider = object.__new__(fusion.FusionProvider)
        provider.lab = mock.Mock(os="windows")
        provider.lab.name = "win"
        provider.options = dict(fusion.WINDOWS_DEFAULTS)
        registry = mock.Mock()
        registry.get = lambda name: {"provider": "fusion", "provisioned": provisioned} if name == "windows-11" else None
        with mock.patch.object(fusion.FusionProvider, "exec", lambda self, argv, timeout, env=None: guest.exec(argv, timeout, env)), \
                mock.patch.object(fusion.bases, "Registry", lambda: registry):  # fmt: skip
            return {check: (status, detail, fix) for check, status, detail, fix in provider.diagnose_guest()}

    def test_a_guest_in_utc_whose_clock_matches_is_ok(self):
        status, detail, fix = self.diagnose(StandInGuest(ahead=-2))["Clock"]

        self.assertEqual(status, OK)
        self.assertIn("UTC", detail)

    def test_a_guest_from_an_older_provisioning_is_a_warning_even_while_its_time_is_right(self):
        # e.g. Windows in "(UTC+03:00) Nairobi" on a Mac in Moscow: right only by chance
        status, detail, fix = self.diagnose(StandInGuest(zone="E. Africa Standard Time", universal=0), provisioned=fusion_windows.UTC_SINCE - 1)["Clock"]

        self.assertEqual(status, WARN)
        self.assertIn("E. Africa Standard Time", detail)
        self.assertIn("vmlab base create windows-11", fix)
        self.assertIn("older vmlab", fix)

    def test_a_guest_from_an_older_provisioning_hours_off_says_by_how_much(self):
        status, detail, fix = self.diagnose(StandInGuest(ahead=10 * 3600, zone=PACIFIC, universal=0), provisioned=fusion_windows.UTC_SINCE - 1)["Clock"]

        self.assertEqual(status, WARN)
        self.assertIn("10 h ahead", detail)
        self.assertIn("vmlab base create windows-11", fix)

    def test_a_guest_of_this_provisioning_whose_time_zone_was_changed_goes_back_to_clean_state(self):
        status, detail, fix = self.diagnose(StandInGuest(zone=PACIFIC))["Clock"]

        self.assertEqual(status, WARN)
        self.assertIn(PACIFIC, detail)
        self.assertIn("Clean state", fix)
        self.assertNotIn("vmlab base create", fix)

    def test_a_guest_in_utc_that_is_off_is_fixed_by_a_restart(self):
        status, detail, fix = self.diagnose(StandInGuest(ahead=600))["Clock"]

        self.assertEqual(status, WARN)
        self.assertIn("10 min ahead", detail)
        self.assertIn("vmlab down win && vmlab up win", fix)
        self.assertNotIn("base create", fix)


if __name__ == "__main__":
    unittest.main()
