"""Architecture coverage: can this Host test a Lab's Build artifact, and how?

A Guest's architecture follows the Host's. A Lab whose arch differs is covered
only where the Guest OS itself runs the other architecture's programs;
otherwise it is not covered on this Host, which doctor and every Run report say.
"""

import os

from vmlab.config import ARCHES, UsageError, host_arch

NATIVE, EMULATED, UNCOVERED = "native", "emulated", "uncovered"

# (os, Guest arch, Build artifact arch) -> how the Guest OS runs it
EMULATION = {
    ("windows", "arm64", "x86_64"): "Windows on Arm's x64 emulation",
}

# Why the Guest OS cannot run the other architecture, by (os, Guest arch)
GAPS = {
    ("macos", "arm64"): "vmlab does not install Rosetta 2 in macOS Guests",
    ("macos", "x86_64"): "Intel Macs cannot run Apple silicon apps",
    ("windows", "x86_64"): "x64 Windows cannot run Arm apps",
    ("linux", "arm64"): "Linux has no built-in x86_64 emulation",
    ("linux", "x86_64"): "Linux has no built-in Arm emulation",
}

OS_NAMES = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}


def guest_arch():
    """The Host's arch, which every Guest has. VMLAB_HOST_ARCH plays the other Host, for tests only."""
    override = os.environ.get("VMLAB_HOST_ARCH")
    if override and override not in ARCHES:
        raise UsageError("VMLAB_HOST_ARCH=%s: use one of: %s" % (override, ", ".join(ARCHES)))
    return override or host_arch()


def coverage(lab):
    """{mode, arch, guest_arch, detail} for lab on this Host."""
    guest = guest_arch()
    result = {"mode": NATIVE, "arch": lab.arch, "guest_arch": guest, "detail": "native %s Guest" % guest}
    if lab.arch == guest:
        return result
    how = EMULATION.get((lab.os, guest, lab.arch))
    if how:
        result.update(mode=EMULATED, detail="%s Build artifacts are emulated in an %s Guest (%s)" % (lab.arch, guest, how))
    else:
        result.update(
            mode=UNCOVERED,
            detail="%s %s Build artifacts are not covered on this %s Host: %s" % (lab.arch, OS_NAMES[lab.os], guest, GAPS[(lab.os, guest)]),
        )
    return result


def fix(lab):
    """What to do about a Lab this Host does not cover."""
    return 'test it on an %s Host; a Lab with arch = "%s" covers %s on this one' % (lab.arch, guest_arch(), OS_NAMES[lab.os])


def warning(lab):
    """The warning for a Lab this Host does not cover; None when it is covered."""
    covered = coverage(lab)
    return "%s; %s" % (covered["detail"], fix(lab)) if covered["mode"] == UNCOVERED else None
