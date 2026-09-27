"""How the Fusion Provider finds a Linux Guest's address and decides it is reachable, loaded directly
(vmlab.providers.fusion) against stand-ins: a DHCP lease file and scripted Channels. A Guest that
boots from a snapshot may be asked for its address before VMware Tools publish one."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab.providers import fusion  # noqa: E402
from vmlab.providers.base import ChannelError, ExecResult  # noqa: E402

MAC = "00:0c:29:6a:f3:1c"


def lease(ip, starts, mac=MAC):
    return "lease %s {\n\tstarts 5 %s;\n\tends 5 %s;\n\thardware ethernet %s;\n}\n" % (ip, starts, starts, mac)


class LeasedAddressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vmlab-leases-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vmx = self.tmp / "clone.vmx"
        self.vmx.write_text('ethernet0.generatedAddress = "%s"\n' % MAC)
        self.leases = self.tmp / "vmnet8.leases"

    def leased(self, text):
        self.leases.write_text(text)
        with mock.patch.object(fusion, "DHCP_LEASES", str(self.leases)):
            return fusion.FusionVM(self.vmx)._leased_ip()

    def test_the_newest_lease_wins_wherever_the_file_has_it(self):
        # Fusion's DHCP server rewrites the file: an old lease can come after the new one.
        text = lease("192.168.164.151", "2026/09/26 23:05:15") + lease("192.168.164.134", "2026/09/23 15:24:54")

        self.assertEqual(self.leased(text), "192.168.164.151")

    def test_other_macs_leases_do_not_count(self):
        text = lease("192.168.164.151", "2026/09/26 23:05:15") + lease("192.168.164.200", "2026/09/27 01:00:00", mac="00:0c:29:00:00:01")

        self.assertEqual(self.leased(text), "192.168.164.151")

    def test_no_lease_for_the_mac_is_none(self):
        self.assertIsNone(self.leased(lease("192.168.164.200", "2026/09/27 01:00:00", mac="00:0c:29:00:00:01")))


class Channel:
    """A Channel whose probe answers, or fails as ssh does against an address nobody has."""

    def __init__(self, name, answers):
        self.name = name
        self.answers = answers

    def exec(self, argv, timeout, env, stdin=None):
        if not self.answers:
            raise ChannelError("ssh vmlab@192.168.164.134 failed: ssh: connect to host 192.168.164.134 port 22: Host is down", "")
        return ExecResult(argv, 0, "x11\n", "")


class LinuxReachableTest(unittest.TestCase):
    def provider(self, booting, ssh_answers):
        p = fusion.FusionProvider.__new__(fusion.FusionProvider)
        p.lab = SimpleNamespace(os="linux", name="linux")
        p.vm = mock.Mock()
        p._channels = [Channel("ssh", ssh_answers), Channel("vmrun", True)]
        p._booting = booting
        p._seen_session = None
        p.is_running = lambda: True
        return p

    def test_while_it_boots_a_guest_is_reachable_only_once_every_channel_answers(self):
        # ssh at a stale address, vmrun fine: a Run starting now would send every call over vmrun.
        self.assertFalse(self.provider(booting=True, ssh_answers=False).is_reachable())
        self.assertTrue(self.provider(booting=True, ssh_answers=True).is_reachable())

    def test_once_up_one_channel_that_answers_is_enough(self):
        self.assertTrue(self.provider(booting=False, ssh_answers=False).is_reachable())


if __name__ == "__main__":
    unittest.main()
