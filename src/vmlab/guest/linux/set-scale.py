"""Give a Linux clone its Display scale, from the GNOME on Wayland session of its first boot, for the
session it logs into from its next boot on.

    python3 - SCALE SESSION     (this file on stdin; SESSION: wayland or x11)

The Desktop keeps about the size it has at scale 1: its mode is the one, of those GNOME allows SCALE
on, whose size divided by SCALE is nearest the current mode's. Wayland: ~/.config/monitors.xml,
which GNOME reads at login. X11: that mode as the X server's preferred one, and Xfce's window
scaling (Xfce scales by whole numbers only).
"""

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

XORG_CONF = "/etc/X11/xorg.conf.d/90-vmlab-scale.conf"

MONITORS = """<monitors version="2">
  <configuration>
    <logicalmonitor>
      <x>0</x>
      <y>0</y>
      <scale>{scale!r}</scale>
      <primary>yes</primary>
      <monitor>
        <monitorspec>
          <connector>{connector}</connector>
          <vendor>{vendor}</vendor>
          <product>{product}</product>
          <serial>{serial}</serial>
        </monitorspec>
        <mode>
          <width>{width}</width>
          <height>{height}</height>
          <rate>{rate!r}</rate>
        </mode>
      </monitor>
    </logicalmonitor>
  </configuration>
</monitors>
"""

XORG = """Section "Monitor"
  Identifier "{connector}"
  Option "PreferredMode" "{width}x{height}"
EndSection
"""


def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(1)


def current_state():
    """GNOME's DisplayConfig GetCurrentState: (serial, monitors, logical monitors, properties)."""
    from gi.repository import Gio

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync(
        "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig", "org.gnome.Mutter.DisplayConfig",
        "GetCurrentState", None, None, Gio.DBusCallFlags.NONE, 5000, None,
    ).unpack()  # fmt: skip


def choose(state, scale):
    """(monitor spec, (width, height, rate), scale): the Guest's one monitor, the mode to show scale on,
    and scale as GNOME lists it for that mode (monitors.xml must name one it lists)."""
    spec, modes, _ = state[1][0]
    _, width, height = next(m for m in modes if m[6].get("is-current"))[:3]
    fits = [(m, s) for m in modes for s in m[5] if abs(s - scale) < 0.01]
    if not fits:
        fail("GNOME shows %s at no scale %g" % (spec[0], scale))
    # the Desktop's logical size nearest its size at scale 1; then the fewer pixels
    mode, listed = min(fits, key=lambda f: (abs(f[0][1] / scale - width) + abs(f[0][2] / scale - height), f[0][1] * f[0][2]))
    return spec, mode[1:4], listed


def monitors_xml(spec, mode, scale):
    connector, vendor, product, serial = (escape(v) for v in spec)
    width, height, rate = mode
    return MONITORS.format(scale=float(scale), connector=connector, vendor=vendor, product=product, serial=serial, width=width, height=height, rate=rate)


def main(scale, session):
    spec, mode, scale = choose(current_state(), scale)
    if session == "wayland":
        path = Path.home() / ".config" / "monitors.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(monitors_xml(spec, mode, scale), encoding="utf-8")
        return
    conf = XORG.format(connector=spec[0], width=mode[0], height=mode[1])
    written = subprocess.run(["sudo", "-n", "sh", "-c", 'mkdir -p "${1%/*}" && cat > "$1"', "sh", XORG_CONF], input=conf, text=True)
    if written.returncode:
        fail("writing %s failed" % XORG_CONF)
    factor = subprocess.run(["xfconf-query", "-c", "xsettings", "-p", "/Gdk/WindowScalingFactor", "-n", "-t", "int", "-s", str(int(scale))])
    if factor.returncode:
        fail("setting Xfce's window scaling failed")


if __name__ == "__main__":
    main(float(sys.argv[1]), sys.argv[2])
