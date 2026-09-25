"""A Windows Guest's clock against the Host's: the wizard behind `vmlab base create windows-11`
stops on it, and `vmlab doctor` warns about it (vmlab.providers.fusion_windows).

Fusion hands Windows the Mac's local time as its hardware clock, which Windows reads in its own
time zone: in another one than the Mac's, its clock runs hours off. These load the parts
directly, against a stand-in Guest that answers the clock query with a clock of its own; the
contract suite (tests/contract/test_contract.py) and doctor against a real Lab ask a real one.
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab.providers import fusion, fusion_windows  # noqa: E402
from vmlab.providers.base import OK, WARN, ExecResult, GuestError  # noqa: E402

PACIFIC = "(UTC-08:00) Pacific Time (US & Canada)"
MOSCOW = ("Europe/Moscow", 180)  # the Mac's time zone and its UTC offset now, in minutes


class StandInGuest:
    """Answers the clock query as Windows does: its UTC time in ms, its UTC offset now in
    minutes, its time zone's display name. ahead: seconds its clock runs ahead of the Host's."""

    def __init__(self, ahead=0, offset=-420, zone=PACIFIC):
        self.ahead, self.offset, self.zone = ahead, offset, zone

    def exec(self, argv, timeout, env=None, stdin=None):
        if argv != fusion_windows.CLOCK:
            return ExecResult(argv, 1, "", "stand-in: unsupported %r" % (argv,))
        now = int((time.time() + self.ahead) * 1000)
        return ExecResult(argv, 0, "%d\r\n%d\r\n%s\r\n" % (now, self.offset, self.zone), "")


class NoTerminal:
    interactive = False

    def pause(self, message):
        return False


class ClockTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fusion_windows, "host_time_zone", lambda: MOSCOW)
        patcher.start()
        self.addCleanup(patcher.stop)


class WizardClockTest(ClockTestCase):
    def wizard(self, guest):
        said = []
        self.shown = []
        vm = mock.Mock(vmx=Path("/vms/vmlab-base-windows-11.vmwarevm/vmlab-base-windows-11.vmx"))
        wizard = fusion_windows.Wizard("windows-11", NoTerminal(), said.append)
        try:
            fusion_windows.check_clock(wizard, guest, vm, show=lambda: self.shown.append(True))
        except GuestError as exc:
            return "\n".join(said), exc
        return "\n".join(said), None

    def test_a_clock_hours_off_stops_the_wizard_with_the_macs_time_zone_and_where_to_set_it(self):
        out, error = self.wizard(StandInGuest(ahead=10 * 3600))

        self.assertIsNotNone(error, "the wizard must not go on to provisioning")
        self.assertIn("10 h ahead", error.message)
        self.assertIn(PACIFIC, error.message)
        self.assertIn("Europe/Moscow", out)
        self.assertIn("UTC+03:00", out)
        self.assertIn("Settings > Time & language > Date & time", out)
        self.assertIn("Sync now", out, "a new time zone leaves Windows' UTC clock as it was")
        self.assertEqual(self.shown, [True], "the person needs the Guest in a window to set it")

    def test_a_clock_that_matches_the_hosts_lets_it_go_on(self):
        out, error = self.wizard(StandInGuest(ahead=3, offset=180, zone="(UTC+03:00) Moscow, St. Petersburg"))

        self.assertIsNone(error)
        self.assertIn("ok: ", out)
        self.assertEqual(self.shown, [], "no window when nobody has anything to do in it")

    def test_in_the_macs_time_zone_but_still_off_it_says_to_sync_the_clock(self):
        out, error = self.wizard(StandInGuest(ahead=-10 * 3600, offset=180, zone="(UTC+03:00) Moscow, St. Petersburg"))

        self.assertIn("10 h behind", error.message)
        self.assertIn("time zone is the Mac's", error.message)
        self.assertIn("Sync now", error.message)

    def test_a_clock_it_cannot_read_is_not_taken_as_right(self):
        guest = StandInGuest()
        guest.exec = lambda argv, timeout, env=None, stdin=None: ExecResult(argv, 1, "", "powershell: boom")

        out, error = self.wizard(guest)

        self.assertIn("cannot read the Guest's clock", error.message)
        self.assertIn("boom", error.message)


class DoctorClockTest(ClockTestCase):
    def diagnose(self, guest):
        provider = object.__new__(fusion.FusionProvider)
        provider.lab = mock.Mock(os="windows")
        provider.lab.name = "win"
        provider.options = dict(fusion.WINDOWS_DEFAULTS)
        with mock.patch.object(fusion.FusionProvider, "exec", lambda self, argv, timeout, env=None: guest.exec(argv, timeout, env)):
            return {check: (status, detail, fix) for check, status, detail, fix in provider.diagnose_guest()}

    def test_a_running_guest_whose_clock_is_off_is_a_warning_with_the_fix(self):
        status, detail, fix = self.diagnose(StandInGuest(ahead=10 * 3600))["Clock"]

        self.assertEqual(status, WARN)
        self.assertIn("10 h ahead", detail)
        self.assertIn("Europe/Moscow", fix)
        self.assertIn("vmlab base create windows-11 --reprovision", fix, "a Lab starts with its Base guest's time zone")

    def test_in_the_macs_time_zone_but_still_off_a_restart_reads_the_macs_time_again(self):
        status, detail, fix = self.diagnose(StandInGuest(ahead=600, offset=180, zone="(UTC+03:00) Moscow, St. Petersburg"))["Clock"]

        self.assertEqual(status, WARN)
        self.assertIn("10 min ahead", detail)
        self.assertIn("vmlab down win && vmlab up win", fix)
        self.assertNotIn("--reprovision", fix)

    def test_a_clock_that_matches_is_ok_and_shown(self):
        status, detail, fix = self.diagnose(StandInGuest(ahead=-2, offset=180, zone="(UTC+03:00) Moscow, St. Petersburg"))["Clock"]

        self.assertEqual(status, OK)
        self.assertIn("Moscow, St. Petersburg", detail)


class HostTimeZoneTest(unittest.TestCase):
    def test_the_macs_time_zone_is_named(self):
        name, offset = fusion_windows.host_time_zone()

        self.assertTrue(name)
        self.assertEqual(offset, time.localtime().tm_gmtoff // 60)


if __name__ == "__main__":
    unittest.main()
