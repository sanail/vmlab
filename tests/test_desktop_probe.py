"""The Linux Guest's desktop probe (vmlab.providers.fusion.DESKTOP_PROBE), which says when a
Guest can be driven, run by /bin/sh against stand-ins for systemctl, loginctl, wmctrl and sudo.

What a real Guest adds (GDM, plymouth, the VT switch itself) is covered by Seam 2:
tests/contract/test_contract.py with a Linux Lab.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab.providers.fusion import DESKTOP_PROBE  # noqa: E402

# Each stand-in answers from FAKE_* variables and logs its argv to FAKE_LOG.
STANDINS = {
    "systemctl": """\
        if [ "$1" = --user ] && [ "$2" = show-environment ]; then printf '%s\\n' "$FAKE_USER_ENV"; exit 0; fi
        if [ "$1" = --user ] && [ "$2" = is-active ]; then exit 0; fi
        if [ "$1 $3" = "show LoadState" ]; then printf '%s\\n' "$FAKE_PLYMOUTH_LOAD"; exit 0; fi
        if [ "$1 $3" = "show ActiveState" ]; then printf '%s\\n' "$FAKE_PLYMOUTH_QUIT_WAIT"; exit 0; fi
        exit 1
        """,
    "loginctl": """\
        case "$1" in
        show-user) printf '%s\\n' "$FAKE_SESSION" ;;
        show-session) if [ "$4" = Type ]; then printf '%s\\n' "$FAKE_TYPE"; else printf '%s\\n' "$FAKE_ACTIVE"; fi ;;
        activate) [ "$(id -u)" = "$FAKE_ROOT_UID" ] || exit 1 ;;
        esac
        exit 0
        """,
    "wmctrl": "exit 0\n",
    "sudo": """\
        [ "$1" = -n ] && shift
        FAKE_ROOT_UID=$(id -u) exec "$@"
        """,
}


class DesktopProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vmlab-probe-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "log"
        for name, body in STANDINS.items():
            path = self.bin / name
            path.write_text("#!/bin/sh\nprintf '%%s\\n' \"$(basename \"$0\") $*\" >> \"$FAKE_LOG\"\n%s" % textwrap.dedent(body))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.guest = {
            "FAKE_SESSION": "2",
            "FAKE_TYPE": "x11",
            "FAKE_ACTIVE": "yes",
            "FAKE_PLYMOUTH_LOAD": "loaded",
            "FAKE_PLYMOUTH_QUIT_WAIT": "active",
            "FAKE_USER_ENV": "XDG_SESSION_TYPE=x11\nDISPLAY=:0\nXAUTHORITY=/run/user/1000/gdm/Xauthority",
            "FAKE_ROOT_UID": "-1",
        }

    def probe(self):
        env = dict(os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"]), FAKE_LOG=str(self.log), **self.guest)
        return subprocess.run(DESKTOP_PROBE, env=env, capture_output=True, text=True, timeout=30)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_an_active_session_after_the_boot_is_ready_and_says_its_type(self):
        result = self.probe()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "x11"), result.stderr)
        self.assertFalse([c for c in self.calls() if "activate" in c], self.calls())

    def test_a_guest_without_plymouth_is_ready(self):
        self.guest.update(FAKE_PLYMOUTH_LOAD="not-found", FAKE_PLYMOUTH_QUIT_WAIT="inactive")  # what systemctl shows for a unit that is not there
        self.assertEqual(self.probe().returncode, 0)

    def test_not_ready_while_the_boot_may_still_hand_the_console_back(self):
        # plymouth quits at the end of the boot, and the console it hands back can switch the
        # screen away from the session: GDM then shows its login screen there.
        for state in ("inactive", "activating"):  # not started yet, then waiting for plymouth to quit
            self.guest["FAKE_PLYMOUTH_QUIT_WAIT"] = state
            self.assertNotEqual(self.probe().returncode, 0, state)

    def test_a_session_off_the_screen_is_not_ready_and_is_put_back_on_it(self):
        self.guest["FAKE_ACTIVE"] = "no"
        result = self.probe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sudo -n loginctl activate 2", self.calls(), "as root when polkit refuses the user")

    def test_a_session_off_the_screen_is_put_back_without_sudo_where_polkit_lets_the_user(self):
        self.guest.update(FAKE_ACTIVE="no", FAKE_ROOT_UID=str(os.getuid()))
        self.assertNotEqual(self.probe().returncode, 0)
        self.assertIn("loginctl activate 2", self.calls())
        self.assertNotIn("sudo -n loginctl activate 2", self.calls())


if __name__ == "__main__":
    unittest.main()
