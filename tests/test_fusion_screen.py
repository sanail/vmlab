"""Fusion's screenshots, loaded directly (vmlab.providers.fusion) against a stand-in vmcli: a picture of
1x1 pixels is no picture of the Guest's screen, and a Guest that just booted must show one."""

import os
import shutil
import stat
import struct
import sys
import tempfile
import textwrap
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vmlab.providers import fusion  # noqa: E402
from vmlab.providers.base import GuestError  # noqa: E402


def png(width, height):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    rows = b"".join(b"\0" + b"\0\0\0" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vmlab-screen-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vmx = self.tmp / "clone.vmx"
        self.vmx.write_text("")
        # vmcli VMX MKS captureScreenshot DEST: copies the picture the test chose (shots, one per call, the last repeated).
        self.shots = self.tmp / "shots"
        self.shots.mkdir()
        vmcli = self.tmp / "vmcli"
        vmcli.write_text(textwrap.dedent("""\
            #!/bin/sh
            first=$(ls "%s" | sort | head -n 1)
            cp "%s/$first" "$4"
            [ "$(ls "%s" | wc -l)" -gt 1 ] && rm "%s/$first"
            exit 0
            """ % ((self.shots,) * 4)))
        vmcli.chmod(vmcli.stat().st_mode | stat.S_IXUSR)
        patcher = mock.patch.dict(os.environ, {"VMLAB_VMCLI": str(vmcli)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def show(self, *sizes):
        for i, size in enumerate(sizes):
            (self.shots / ("%02d.png" % i)).write_bytes(png(*size))

    def test_a_picture_of_one_pixel_is_no_screenshot(self):
        self.show((1, 1))
        with self.assertRaises(GuestError) as caught:
            fusion.FusionVM(self.vmx).screenshot(self.tmp / "shot.png", 10)
        self.assertIn("1x1 pixels: Fusion gets no picture of the Guest's screen", caught.exception.message)

    def test_a_picture_of_the_screen_is_one(self):
        self.show((1280, 800))
        fusion.FusionVM(self.vmx).screenshot(self.tmp / "shot.png", 10)
        self.assertEqual(struct.unpack(">II", (self.tmp / "shot.png").read_bytes()[16:24]), (1280, 800))

    def screen_shown(self):
        """FusionProvider._screen_shown on just enough of a provider."""
        provider = SimpleNamespace(vm=fusion.FusionVM(self.vmx), lab=SimpleNamespace(name="linux", step_timeout=10))
        return fusion.FusionProvider._screen_shown(provider)

    def test_after_a_boot_the_screen_may_take_a_moment_to_show(self):
        self.show((1, 1), (1, 1), (1280, 800))
        with mock.patch.object(fusion.time, "sleep"):
            self.screen_shown()

    def test_a_boot_whose_screen_never_shows_fails_once_saying_so(self):
        self.show((1, 1))
        clock = iter(range(0, 1000, 10))
        with mock.patch.object(fusion.time, "sleep"), mock.patch.object(fusion.hostpower, "awake_time", lambda: next(clock)):
            with self.assertRaises(GuestError) as caught:
                self.screen_shown()
        self.assertIn("Guest linux booted, but its screen did not show within 30s", caught.exception.message)
        self.assertIn("vmlab down linux && vmlab up linux", caught.exception.fix)


if __name__ == "__main__":
    unittest.main()
