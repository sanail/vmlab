"""VMware Fusion Provider: Linux and Windows Guests.

A Lab's Guest is a clone of its Base guest's provisioned snapshot (`vmlab
base create ubuntu-26.04` or `windows-11`), so it costs little disk and no
project ever runs in the Base guest: a linked clone for Linux, an APFS copy
for Windows, whose VM is encrypted for its TPM and which vmrun cannot clone
(vmlab.providers.fusion_windows). Clean state is the clone's own snapshot,
vmlab-clean, taken when the clone is made: restoring reverts to it. VMs live
under $VMLAB_HOME/fusion, never in Fusion's own Virtual Machines folder. Each
provisioning of a Base guest takes a new snapshot; an earlier one goes, after
asking, once no linked clone needs it (delete_old_snapshots, `vmlab clean`);
a Windows Base guest's only in Fusion's window, which vmlab explains: vmrun
cannot merge an encrypted VM's disks.

Channels (ADR 0003): SSH first, with vmlab's key and the host key pinned when
the Base guest was made, and vmrun guest operations as the fallback. On Linux
SSH is multiplexed and runs the command itself; on Windows, where an SSH
session cannot reach the desktop, it hands the call to an interactive
Scheduled Task, as vmrun -interactive does (vmlab.providers.windows). vmrun takes the
Guest user's password, and an encrypted VM's own password, from vmlab's home
on its command line, so they show in the Host's process list for the length of
a call; they guard a throwaway Guest on Fusion's private NAT network.
Screenshots are Host-side (vmcli MKS captureScreenshot): no Guest credentials,
no Wayland consent dialog. Guests have no sound device: nothing they play
reaches the Host, and they cannot take its Bluetooth headset.

The Linux desktop session is GNOME on Wayland, or Xfce on X11 for Labs that
ask for it: a new clone of such a Lab boots once to switch its autologin
session before its Clean state is taken. Commands run with the session's
environment (DISPLAY, WAYLAND_DISPLAY, ...), as a terminal on its desktop would.

Options, under [labs.<name>.fusion]:

    base = "ubuntu-26.04"     # Base guest to clone (vmlab base list); windows-11 for Windows Labs
    cpu = 4
    channels = ["ssh", "vmrun"]
    session = "wayland"       # or "x11"; Linux only
"""

import base64
import hashlib
import io
import json
import os
import pkgutil
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from vmlab import bases, hostpower, hostproc
from vmlab.config import ConfigError, host_arch
from vmlab.home import vmlab_home
from vmlab.providers import windows
from vmlab.providers.base import BOOT_POLL_SECONDS, FAIL, INFO, OK, WARN, BootState, Channel, ChannelError, ExecResult, GuestError, GuestTimeout, Provider
from vmlab.providers.ssh import SshChannel, is_pinned, pin_host_key, public_key

DEFAULTS = {"base": "ubuntu-26.04", "cpu": 4, "channels": ["ssh", "vmrun"], "session": "wayland"}
# language: the Windows display language the Lab's Scenarios expect; element names are in it.
WINDOWS_DEFAULTS = {"base": "windows-11", "cpu": 4, "channels": ["ssh", "vmrun"], "language": "en-US"}
LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
CHANNELS = ("ssh", "vmrun")
# The autologin session (name of its .desktop file) for each session type. Base guests boot into Wayland.
SESSIONS = {"wayland": "ubuntu", "x11": "xfce"}
SESSION_NAMES = {"wayland": "Wayland", "x11": "X11"}
# Prints (true,) when the session's panel hosts Tray icons: GNOME's AppIndicator extension, Xfce's Status Tray
TRAY_WATCHER = "gdbus call --session -d org.freedesktop.DBus -o /org/freedesktop/DBus -m org.freedesktop.DBus.NameHasOwner org.kde.StatusNotifierWatcher"
FUSION_APP = "/Applications/VMware Fusion.app"
INSTALL_FIX = (
    "install VMware Fusion Pro (free; ask before installing anything on the Host): download it from "
    "Broadcom's support portal (a free Broadcom account; Homebrew has no cask for it) then open the .dmg and double-click its installer"
)
CALL_TIMEOUT = 60  # s for quick vmrun commands
DHCP_LEASES = "/var/db/vmware/vmnet-dhcpd-vmnet8.leases"  # Fusion's NAT network
PROBE_TIMEOUT = 15  # s per reachability probe; vmrun may hang while the Guest boots
INSTALL_TIMEOUT = 2 * 3600  # s for the unattended install, which downloads updates
BASE_BOOT_TIMEOUT = 600
PROVISION_TIMEOUT = 1800
PROVISION_VERSION = 6  # bump when provision.sh, the Shell extension or the recorder changes; `base create` then re-provisions
BASE_CPU, BASE_MEMORY_MB, BASE_DISK = 4, 4096, "64GB"
GUEST_USER = "vmlab"
STOP_GRACE = 60  # s a Guest gets to shut down before it is powered off
RUNNING_TTL = 1.0  # s a VM found running counts as running without asking vmrun again
DISK_OP_TIMEOUT = 600  # s for clone, snapshot, revert and delete while creating a Base guest
CLEAN_SNAPSHOT = "vmlab-clean"
MERGE_TIMEOUT = 3600  # s to delete a snapshot, whose changes Fusion merges into the next one's disk (GBs on Windows)
PROVISIONED_PREFIX = "vmlab-provisioned-"  # + the provisioning's id: the Base guest's snapshot Labs are cloned from
# Set to 1 to let `base create` delete earlier provisioned snapshots no Lab needs without asking.
DELETE_OLD_SNAPSHOTS = "VMLAB_DELETE_OLD_SNAPSHOTS"
# The graphical session is up (autologin done), has published its environment and holds the
# screen: a Run can drive the desktop. Prints the session type. logind knows the type; the user
# manager's environment may still be the previous session's until the new one imports its own.
# plymouth quits at the end of the boot, seconds after autologin, and the console it hands back
# can take the screen from the session: GDM then puts its login screen there, while the session
# lives on behind it. So the boot must be past that, and a session off the screen is put back.
DESKTOP_PROBE = ["/bin/sh", "-c", """
uid=$(id -u); XDG_RUNTIME_DIR=/run/user/$uid; export XDG_RUNTIME_DIR
u=plymouth-quit-wait.service
[ "$(systemctl show -p LoadState --value $u 2>/dev/null)" != loaded ] || [ "$(systemctl show -p ActiveState --value $u)" = active ] || exit 1
s=$(loginctl show-user "$uid" -p Display --value 2>/dev/null); [ -n "$s" ] || exit 1
t=$(loginctl show-session "$s" -p Type --value) || exit 1
env=$(systemctl --user show-environment 2>/dev/null) || exit 1
printf '%s\\n' "$env" | grep -qx "XDG_SESSION_TYPE=$t" || exit 1
case $t in
wayland) systemctl --user is-active --quiet graphical-session.target ;;
x11) DISPLAY=$(printf '%s\\n' "$env" | sed -n 's/^DISPLAY=//p') XAUTHORITY=$(printf '%s\\n' "$env" | sed -n 's/^XAUTHORITY=//p') wmctrl -m >/dev/null 2>&1 ;;
*) false ;;
esac || exit 1
if [ "$(loginctl show-session "$s" -p Active --value)" != yes ]; then
  loginctl activate "$s" >/dev/null 2>&1 || sudo -n loginctl activate "$s" >/dev/null 2>&1
  exit 1
fi
echo "$t"
"""]
# Runs a command with the desktop session's environment, as the user manager holds it, over what
# the Channel's login set (XDG_SESSION_TYPE=tty, ...). Only plain values are taken, so the eval
# sees no shell syntax.
SESSION_ENV = r"""
XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}; : "${DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus}"
export XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
eval "$(systemctl --user show-environment 2>/dev/null | sed -n -E 's/^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|XDG_SESSION_TYPE|XDG_CURRENT_DESKTOP|XDG_SESSION_DESKTOP|DESKTOP_SESSION|GTK_MODULES|QT_ACCESSIBILITY|QT_LINUX_ACCESSIBILITY_ALWAYS_ON|WEBKIT_DISABLE_DMABUF_RENDERER)=([A-Za-z0-9_:/.,@+-]*)$/\1=\2; export \1/p')"
exec "$@"
"""
# Sets the user's autologin session ($1: name, $2: type) in AccountsService, where GDM looks.
SWITCH_SESSION = r"""
dir=/usr/share/wayland-sessions; [ "$2" = x11 ] && dir=/usr/share/xsessions
[ -f "$dir/$1.desktop" ] || { echo "the Guest has no $2 session $1 ($dir/$1.desktop is missing)" >&2; exit 3; }
sudo -n python3 -c '
import configparser, sys
path, name, kind = sys.argv[1:]
config = configparser.ConfigParser()
config.optionxform = str
config.read(path)
if not config.has_section("User"):
    config.add_section("User")
config["User"].update(Session=name, XSession=name, SessionType=kind)
with open(path, "w") as f:
    config.write(f, space_around_delimiters=False)
' "/var/lib/AccountsService/users/$(id -un)" "$1" "$2" && sync
"""
EXTENSION_DIR = "/tmp/vmlab-shell-extension"  # where provisioning finds the Shell extension's files and RECORDER
RECORDER = "notification-recorder.py"  # vmlab's Notification recorder, run by the systemd user unit RECORDER_UNIT
RECORDER_UNIT = "vmlab-notifications.service"
# $VMLAB_HOME/fusion/<clone>.json: which project, Lab and Base guest a clone serves, and which
# provisioning of the Base guest it was made from. `vmlab clean` uses it to find orphans (vmlab.clean).
CLONE_RECORD = ".json"
CREDENTIALS = ".credentials.json"  # a Base guest's user and password, and its VM's encryption password


def fusion_dir():
    path = vmlab_home() / "fusion"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def vmx_path(name):
    """Where vmlab keeps the VM called name."""
    return fusion_dir() / ("%s.vmwarevm" % name) / ("%s.vmx" % name)


def vmrun_binary():
    return os.environ.get("VMLAB_VMRUN") or _existing(FUSION_APP + "/Contents/Public/vmrun") or shutil.which("vmrun") or "vmrun"


def vmcli_binary():
    return os.environ.get("VMLAB_VMCLI") or _existing(FUSION_APP + "/Contents/Library/vmcli") or shutil.which("vmcli") or "vmcli"


def _existing(path):
    return path if os.path.exists(path) else None


def vmrun(args, timeout, auth=()):
    """(code, output) of a vmrun command; GuestError if Fusion is missing, GuestTimeout if it hangs.

    vmrun reports its own errors on stdout ("Error: ..."), so both streams are returned together."""
    argv = [vmrun_binary(), "-T", "fusion"] + list(auth) + [str(a) for a in args]
    try:
        code, out, err = hostproc.run(argv, timeout)
    except FileNotFoundError:
        raise GuestError("VMware Fusion is not installed (%s not found)" % argv[0], INSTALL_FIX)
    except subprocess.TimeoutExpired:
        raise GuestTimeout("`vmrun %s` did not finish within %ss and was killed" % (args[0], timeout))
    return code, _without_noise(out + err)


def vmrun_ok(args, timeout, auth=()):
    code, out = vmrun(args, timeout, auth)
    if code:
        raise GuestError("`vmrun %s` failed: %s" % (args[0], out.strip() or "exit %s" % code), "run it by hand to see why")
    return out


def _without_noise(text):
    """vmrun and vmcli print harmless warnings about Fusion never having been opened."""
    noise = ("LocationGetRoot", "Cannot determine message file", "ServiceImpl_Opener")
    return "\n".join(line for line in text.splitlines() if not any(n in line for n in noise))


def hypervisor():
    """(found, detail, fix): can this Host run VMware Fusion?"""
    if platform.system() != "Darwin":
        return False, "VMware Fusion runs on macOS Hosts only", "run Linux and Windows Labs on a Mac"
    try:
        running_vmx()
    except GuestError as exc:
        return False, exc.message, exc.fix
    try:
        with open(FUSION_APP + "/Contents/Info.plist", "rb") as f:
            return True, "VMware Fusion %s" % plistlib.load(f)["CFBundleShortVersionString"], None
    except (OSError, ValueError, KeyError):
        return True, "VMware Fusion", None  # vmrun from elsewhere (VMLAB_VMRUN, PATH)


def running_vmx():
    """Real paths of the .vmx files of running VMs."""
    out = vmrun_ok(["list"], CALL_TIMEOUT)
    return {os.path.realpath(line.strip()) for line in out.splitlines()[1:] if line.strip()}


class FusionVM:
    """One Fusion VM by .vmx path: the Host-side lifecycle shared by Base guests and Lab clones."""

    def __init__(self, vmx, secrets=None, password=None):
        """secrets: the Base guest VM whose credentials hold this VM's encryption password, if
        it has one; password: that password itself, for a VM vmlab keeps no credentials for."""
        self.vmx = Path(vmx)
        self.secrets = secrets
        self.password = password
        self._ip = None
        self._seen_running = None  # when is_running last found it running

    def _password(self):
        return self.password or (vm_password(self.secrets) if self.secrets else None)

    def password_known(self):
        return bool(self._password())

    @property
    def auth(self):
        """vmrun's arguments that open an encrypted VM: every command but `list` needs them."""
        return ["-vp", self._password()] if self._password() else []

    @property
    def name(self):
        return self.vmx.stem

    def exists(self):
        return self.vmx.is_file()

    def is_running(self):
        # A Run asks several times in a row, and `vmrun list` takes ~0.2 s. Only a yes is
        # remembered, briefly, and only until vmlab itself stops or reverts the VM: a loop
        # waiting for a stop sees it at most that much later.
        if self._seen_running is not None and time.time() - self._seen_running < RUNNING_TTL:
            return True
        running = self.exists() and os.path.realpath(str(self.vmx)) in running_vmx()
        self._seen_running = time.time() if running else None
        return running

    def start(self, timeout=CALL_TIMEOUT, gui=False):
        """Power on, without a window unless gui (for steps a person does in the Guest);
        returns once the VM runs, not once it has booted."""
        self._ip = None
        self._seen_running = None
        # Not through pipes: vmrun leaves a process behind that holds them open.
        log = self.vmx.parent / "vmlab-start.log"
        try:
            with log.open("w") as out:
                code = subprocess.run(
                    [vmrun_binary(), "-T", "fusion"] + self.auth + ["start", str(self.vmx), "gui" if gui else "nogui"],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, timeout=timeout, start_new_session=True,
                ).returncode  # fmt: skip
        except FileNotFoundError:
            raise GuestError("VMware Fusion is not installed", INSTALL_FIX)
        except subprocess.TimeoutExpired:
            raise GuestTimeout("`vmrun start %s` did not finish within %ss" % (self.name, timeout))
        if code:
            raise GuestError("Fusion could not start %s: %s" % (self.name, _without_noise(log.read_text()).strip()), "open %s in Fusion to see why" % self.vmx)

    def stop(self, timeout=STOP_GRACE):
        """Shut down through VMware Tools, or power off when that fails or hangs."""
        self._ip = None
        self._seen_running = None
        try:
            if vmrun(["stop", self.vmx, "soft"], timeout, self.auth)[0] == 0:
                return
        except GuestTimeout:
            pass
        if self.is_running():
            self._seen_running = None
            vmrun_ok(["stop", self.vmx, "hard"], CALL_TIMEOUT, self.auth)

    def ip(self):
        """The Guest's IP, or None. May be stale: reachability needs a probe.

        VMware Tools publish it as guestinfo.ip; `vmrun getGuestIPAddress` can claim Tools
        are not running for minutes after they started, so it is only the second source,
        and Fusion's DHCP lease for the VM's MAC the third."""
        if self._ip is None:
            for args in (["readVariable", self.vmx, "guestVar", "ip"], ["getGuestIPAddress", self.vmx]):
                code, out = vmrun(args, CALL_TIMEOUT, self.auth)
                if code == 0 and re.match(r"^\d+\.\d+\.\d+\.\d+$", out.strip()):
                    self._ip = out.strip()
                    return self._ip
            self._ip = self._leased_ip()
        return self._ip

    def _leased_ip(self):
        """The address Fusion's NAT DHCP server last leased to this VM's MAC, or None."""
        mac = self.config("ethernet0.generatedAddress") if self.exists() else None
        if not mac:
            return None
        try:
            leases = Path(DHCP_LEASES).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        # By its start time: the DHCP server rewrites the file, and an old lease can come after a newer one.
        found = []
        for ip, body in re.findall(r"lease (\S+) \{([^}]*)\}", leases):
            if re.search(r"hardware ethernet %s;" % re.escape(mac), body, re.I):
                starts = re.search(r"starts \d+ ([\d/]+ [\d:]+);", body)
                found.append((starts.group(1) if starts else "", ip))
        return max(found)[1] if found else None

    def forget_ip(self):
        self._ip = None

    def snapshots(self):
        out = vmrun_ok(["listSnapshots", self.vmx], CALL_TIMEOUT, self.auth)
        return [line.strip() for line in out.splitlines()[1:] if line.strip()]

    def snapshot(self, name, timeout=DISK_OP_TIMEOUT):
        vmrun_ok(["snapshot", self.vmx, name], timeout, self.auth)

    def delete_snapshot(self, name, timeout=MERGE_TIMEOUT):
        """Fusion merges its changes into the next snapshot's disk: minutes for a large one."""
        vmrun_ok(["deleteSnapshot", self.vmx, name], timeout, self.auth)

    def revert(self, name, timeout):
        self._seen_running = None
        vmrun_ok(["revertToSnapshot", self.vmx, name], timeout, self.auth)

    def clone_linked(self, dest, snapshot, timeout):
        """Make dest (a FusionVM that does not exist yet) a linked clone of this VM's snapshot."""
        dest.vmx.parent.parent.mkdir(parents=True, exist_ok=True)
        vmrun_ok(["clone", self.vmx, dest.vmx, "linked", "-snapshot=%s" % snapshot, "-cloneName=%s" % dest.name], timeout, self.auth)

    def clone_copy(self, dest):
        """Make dest (a FusionVM that does not exist yet) a copy of this stopped VM, as it is now.

        An APFS clone of its files: instant, and it shares their blocks until either changes
        them. It works for encrypted VMs, which vmrun cannot clone ("Cannot read the virtual
        machine configuration file"). Its files are renamed after dest, the snapshot list
        (.vmsd, found by the .vmx's name) with them; it gets its own UUID and MAC address."""
        dest.vmx.parent.parent.mkdir(parents=True, exist_ok=True)
        code, out, err = hostproc.run(["cp", "-c", "-R", str(self.vmx.parent), str(dest.vmx.parent)], DISK_OP_TIMEOUT)
        if code:
            shutil.rmtree(str(dest.vmx.parent), ignore_errors=True)
            raise GuestError(
                "copying %s failed: %s" % (self.vmx.parent, (err or out).strip()),
                "vmlab's home (%s) must be on the same APFS volume as its Base guests; free some disk space" % vmlab_home(),
            )
        for suffix in (".vmx", ".vmsd"):
            copied = dest.vmx.parent / (self.vmx.stem + suffix)
            if copied.exists() and copied.name != dest.name + suffix:
                copied.rename(dest.vmx.parent / (dest.name + suffix))
        # A copy that says so: Fusion would otherwise ask "moved or copied?" and wait for an answer.
        dest.set_config({"uuid.action": "create", "msg.autoAnswer": "TRUE", "displayName": dest.name,
                         "ethernet0.generatedAddress": None, "ethernet0.generatedAddressOffset": None})  # fmt: skip

    def delete(self, timeout=DISK_OP_TIMEOUT):
        if self.is_running():
            self.stop()
        code, out = vmrun(["deleteVM", self.vmx], timeout, self.auth)
        if code and self.exists():
            fix = "delete it in Fusion, then retry"
            if "insufficient permissions" in out.lower():  # Fusion's window or its library holds it open
                fix = ("VMware Fusion has it open: close its window, or right-click it in Fusion's Virtual Machine Library, "
                       "choose Delete and then Remove from Library (Keep File: vmlab deletes the files), then retry")
            raise GuestError("`vmrun deleteVM %s` failed: %s" % (self.vmx, out.strip()), fix)
        if self.vmx.parent.exists():  # vmrun leaves logs behind, and knows nothing of a VM that never started
            shutil.rmtree(str(self.vmx.parent), ignore_errors=True)

    def config(self, key):
        """A .vmx key's value, or None."""
        match = re.search(r'^%s\s*=\s*"([^"]*)"' % re.escape(key), self.vmx.read_text(encoding="utf-8", errors="replace"), re.M)
        return match.group(1) if match else None

    def has_sound(self):
        return (self.config("sound.present") or "").lower() == "true"

    def set_config(self, settings):
        """Set .vmx keys, e.g. {"memsize": 4096} (the VM must be off); None removes a key."""
        lines = self.vmx.read_text(encoding="utf-8").splitlines()
        for key, value in settings.items():
            lines = [line for line in lines if line.split("=", 1)[0].strip() != key]
            if value is not None:
                lines.append('%s = "%s"' % (key, value))
        self.vmx.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def screenshot(self, dest, timeout):
        try:
            with tempfile.TemporaryFile() as stdin:  # vmcli reads an encrypted VM's password on stdin
                stdin.write(((self._password() or "") + "\n").encode("utf-8"))
                stdin.seek(0)
                code, out, err = hostproc.run([vmcli_binary(), str(self.vmx), "MKS", "captureScreenshot", str(dest)], timeout, stdin=stdin)
        except FileNotFoundError:
            raise GuestError("vmcli is missing from VMware Fusion", INSTALL_FIX)
        except subprocess.TimeoutExpired:
            raise GuestTimeout("screenshot of %s did not finish within %ss" % (self.name, timeout))
        if code or not dest.exists() or not dest.read_bytes()[:4] == b"\x89PNG":
            raise GuestError("screenshot of %s failed: %s" % (self.name, _without_noise(out + err).strip() or "no PNG written"), "check that the Guest is running: vmlab status")


def sound_off(vm, out):
    """Take a Base guest's sound device away (Fusion's Get Windows VM has one), so it plays
    nothing through the Host's speakers and takes no Bluetooth headset while it runs. A running
    one is shut down first: Fusion rewrites a running VM's .vmx."""
    if vm.exists() and vm.has_sound():
        if vm.is_running():
            out("  shutting %s down to take its sound device away" % vm.name)
            vm.stop()
        vm.set_config({"sound.present": "FALSE"})
        out("  turned %s's sound device off: Guests play nothing on the Host" % vm.name)


def sound_findings():
    """doctor's Host check: vmlab's Fusion VMs (every Base guest and Lab clone, of any project)
    with a sound device. [(check, status, detail, fix)]"""
    try:
        vms = HostVMs().vms()
        base_names = {vm: name for vm, (name, _) in fusion_bases().items()}
    except GuestError:
        return []  # Fusion or the registry cannot be asked: the Labs' own checks say so
    effect = "it plays through the Host's speakers or headset while it runs, and can take a Bluetooth headset"
    findings = []
    for vm, running in sorted(vms.items()):
        if not FusionVM(vmx_path(vm)).has_sound():
            continue
        clone = _clone_record(vm)
        if vm in base_names:
            what, fix = "Base guest %s" % base_names[vm], "vmlab base create %s   (shuts it down if it runs, then takes the device away)" % base_names[vm]
        elif clone.get("lab") and clone.get("project"):
            lab = clone["lab"]
            what = "Lab %s of %s" % (lab, clone["project"])
            fix = "in %s: %s   (vmlab takes the device away at every start)" % (clone["project"], "vmlab down %s && vmlab up %s" % (lab, lab) if running else "vmlab up %s" % lab)
        else:
            what, fix = "VM %s" % vm, "vmlab clean   (no known Lab needs it)"
        findings.append(("Guest sound", WARN, "%s has a sound device: %s" % (what, effect), fix))
    return findings


def provisioned_snapshot(provisioned_id):
    """A Base guest's snapshot for one provisioning (bases.provisioning)."""
    return PROVISIONED_PREFIX + provisioned_id[:12]


def fusion_bases():
    """The registered Fusion Base guests, as {VM name: (Base guest name, registry record)}."""
    return {r.get("vm") or bases.vm_name(name): (name, r) for name, r in bases.Registry().all().items() if r.get("provider") == "fusion"}


def stop_hint(name):
    """How the person stops Base guest name. It runs without a window, and an encrypted (Windows)
    VM opens in vmrun only with the password vmlab keeps: `base create` shuts it down."""
    return "vmlab base create %s   (shuts it down)" % name


def shut_down_for_labs(vm, out):
    """`base create` on a ready Base guest: a running one is shut down, since Labs cannot clone it."""
    if vm.is_running():
        out("  shutting %s down: Labs cannot clone a running Base guest" % vm.name)
        vm.stop()


class OldSnapshot:
    """A provisioned snapshot of a Base guest that is not the one Labs are cloned from now."""

    def __init__(self, vm, base, name, running, held_by):
        self.vm, self.base, self.name, self.running = vm, base, name, running
        self.owner = "Base guest %s" % base
        self.held_by = held_by  # [(who, how to let it go)]: the linked clones that still need it
        # Fusion 26's vmrun and vmcli only drop an encrypted VM's snapshot from its list: neither can
        # merge its disks (offline they never do; a running VM refuses them the key), Fusion's window can.
        self.by_hand = _encrypted(vm.vmx)

    @property
    def stop_hint(self):
        return stop_hint(self.base)

    def why_kept(self):
        """Why vmlab does not delete it, or "" when it does."""
        if self.held_by:
            return "; ".join("%s still needs it (%s)" % pair for pair in self.held_by)
        return "vmlab cannot delete an encrypted VM's snapshots; %s" % in_fusion(self.vm, self.name) if self.by_hand else ""

    def delete(self):
        self.vm.delete_snapshot(self.name)


def in_fusion(vm, names):
    """How the person deletes an encrypted VM's snapshots (names) in Fusion's window, which merges their disks."""
    return ("in VMware Fusion, while %s is shut down: File > Open (Cmd+Shift+G: %s), Virtual Machine > Snapshots, delete %s; close its window, "
            "then right-click it in the Virtual Machine Library > Delete > Remove from Library (Keep File). Lab copies made before keep "
            "their own copy of these disks until they are copied again" % (vm.name, vm.vmx.parent, names))


def old_snapshots(vms=None, only=None):
    """The earlier provisioned snapshots of vmlab's Base guests on Fusion (vms: {name: running}, default
    all; only: that one's), other than the current one. A Lab's clone has none: a linked clone's snapshots
    are its own, and a Windows Lab's copy is made again after the next provisioning. A VM whose snapshots
    cannot be listed is skipped: its Lab's own checks say what is wrong."""
    vms = HostVMs().vms() if vms is None else vms
    registry = fusion_bases()
    found = []
    for vm_name, running in sorted(vms.items()):
        if vm_name not in registry or (only and vm_name != only):
            continue
        name, record = registry[vm_name]
        if not record.get("snapshot"):
            continue  # never provisioned yet
        vm = FusionVM(vmx_path(vm_name), secrets=vm_name)
        try:
            names = [s for s in vm.snapshots() if s.startswith(PROVISIONED_PREFIX) and s != record["snapshot"]]
        except GuestError:
            continue
        holders = _linked_clones(name, vm, vms) if names else {}
        found += [OldSnapshot(vm, name, s, running, holders.get(s, [])) for s in names]
    return found


def _linked_clones(name, vm, vms):
    """{snapshot: [(who, how to let it go)]}: the linked clones made from the Base guest's snapshots,
    which need them until they are cloned again. A Lab's clone says which in its record (made_from).
    Fusion lists each snapshot's linked clones in the Base guest's .vmsd, but never forgets one, and a
    Lab is cloned again at the same path: that list only counts for VMs vmlab has no record of, and
    for them the parent disk the clone's own disk names decides, when it can be read."""
    holders, recorded = {}, set()
    for clone_name in sorted(vms):
        clone = _clone_record(clone_name)
        if clone.get("base") != name or not clone.get("made_from"):
            continue
        recorded.add(clone_name)
        if (Path(clone["project"]) / ".vmlab" / "vmlab.toml").is_file():
            again = "vmlab down %s && vmlab up %s" % (clone["lab"], clone["lab"]) if vms[clone_name] else "vmlab up %s" % clone["lab"]
            how = "in %s, `%s` clones it again from the new snapshot" % (clone["project"], again)
        else:
            how = "its project is gone: `vmlab clean` deletes it"
        holders.setdefault(provisioned_snapshot(clone["made_from"]), {})[clone_name] = ("Lab %s of %s" % (clone["lab"], clone["project"]), how)
    vmsd = _vmsd(vm.vmx)
    by_disk = {Path(value).name: vmsd.get(key.split(".")[0] + ".displayName") for key, value in vmsd.items() if re.match(r"snapshot\d+\.disk\d+\.fileName$", key)}
    for key, value in vmsd.items():
        clone_vmx = vm.vmx.parent / value  # Fusion writes absolute paths; a relative one would be the Base guest's
        if not re.match(r"snapshot\d+\.clone\d+$", key) or clone_vmx.stem in recorded or not clone_vmx.is_file():
            continue
        parents = [Path(hint) for hint in _parent_disks(clone_vmx.parent)]
        snapshots = {by_disk.get(p.name) for p in parents if p.parent == vm.vmx.parent} if parents else {vmsd.get(key.split(".")[0] + ".displayName")}
        for snapshot in snapshots - {None}:
            holders.setdefault(snapshot, {})[clone_vmx.stem] = ("VM %s" % clone_vmx.stem, "`vmlab clean` deletes it if no Lab needs it")
    return {snapshot: list(clones.values()) for snapshot, clones in holders.items()}


def _vmsd(vmx):
    """The VM's .vmsd, Fusion's list of its snapshots, as {key: value}, e.g. snapshot0.displayName."""
    try:
        text = vmx.with_suffix(".vmsd").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return dict(re.findall(r'^([\w.]+)\s*=\s*"([^"]*)"', text, re.M))


def _parent_disks(folder):
    """The parent disks the disks in a VM's folder name (parentFileNameHint): a linked clone's
    point into its Base guest's folder. A sparse disk starts with its text descriptor."""
    hints = set()
    for disk in folder.glob("*.vmdk"):
        try:
            with disk.open("rb") as f:
                head = f.read(4096).decode("utf-8", "replace")
        except OSError:
            continue
        hints.update(re.findall(r'^parentFileNameHint="([^"]+)"', head, re.M))
    return hints


def delete_old_snapshots(name, vm, confirm, out):
    """`base create`'s last step: delete the Base guest's earlier provisioned snapshots that no Lab
    clone needs, once the person agrees (--yes, or DELETE_OLD_SNAPSHOTS set, agrees for them), and
    say which Labs keep the others. A running Base guest is left alone."""
    old = old_snapshots(only=vm.name)
    for snapshot in old:
        if snapshot.held_by:
            out("  kept snapshot %s: %s" % (snapshot.name, snapshot.why_kept()))
    unneeded = [snapshot for snapshot in old if not snapshot.held_by]
    if not unneeded:
        return
    names = ", ".join(snapshot.name for snapshot in unneeded)
    if unneeded[0].by_hand:
        out("  kept %s, which no Lab needs: vmlab cannot delete an encrypted VM's snapshots; to free their disk space, %s" % (names, in_fusion(vm, names)))
        return
    if unneeded[0].running:
        out("  kept %s, which no Lab needs: %s is running; stop it (%s), then run `vmlab clean`" % (names, vm.name, stop_hint(name)))
        return
    if not (os.environ.get(DELETE_OLD_SNAPSHOTS) == "1" or confirm("Delete %s's earlier snapshots that no Lab needs (%s)?" % (vm.name, names))):
        out("  kept %s, which no Lab needs: `vmlab clean` or `vmlab base create %s --yes` deletes them; "
            "with %s=1 in your environment `base create` deletes them without asking" % (names, name, DELETE_OLD_SNAPSHOTS))
        return
    free = shutil.disk_usage(str(vm.vmx.parent)).free
    for snapshot in unneeded:
        out("  deleting snapshot %s (Fusion merges it into the next one: minutes for a large one)" % snapshot.name)
        snapshot.delete()
        out("  deleted snapshot %s" % snapshot.name)
    out("  %.1f GB free on the disk (%.1f GB before)" % (shutil.disk_usage(str(vm.vmx.parent)).free / 1e9, free / 1e9))


def snapshot_findings():
    """doctor's Host check: Base guests and Lab copies that keep provisioned snapshots nothing needs.
    [(check, status, detail, fix)]"""
    try:
        old = old_snapshots()
    except GuestError:
        return []  # Fusion or the registry cannot be asked: the Labs' own checks say so
    unneeded = {}
    for snapshot in old:
        if not snapshot.held_by:
            unneeded.setdefault(snapshot.owner, []).append(snapshot)
    findings = []
    for owner, snapshots in sorted(unneeded.items()):
        names = ", ".join(snapshot.name for snapshot in snapshots)
        fix = in_fusion(snapshots[0].vm, names) if snapshots[0].by_hand else "vmlab clean   (asks first; a running VM is left alone)"
        findings.append(("Old snapshots", WARN, "%s keeps %d earlier snapshot%s no Lab needs: %s; they take disk space"
                         % (owner, len(snapshots), "" if len(snapshots) == 1 else "s", names), fix))  # fmt: skip
    return findings


def _credentials_path(base_vm):
    return fusion_dir() / (base_vm + CREDENTIALS)


def credential_files():
    """Every Base guest's credentials file on this Host."""
    return sorted(fusion_dir().glob("*" + CREDENTIALS))


def credentials(base_vm):
    """{"user", "password"} of a Base guest's user, shared by its clones."""
    path = _credentials_path(base_vm)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GuestError("the Guest credentials %s are missing" % path, "re-create the Base guest: vmlab base create NAME")


def save_credentials(base_vm, user, password, vm_password=None):
    """vm_password: the VM's encryption password (Windows Base guests), which its clones share."""
    path = _credentials_path(base_vm)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(dict({"user": user, "password": password}, **({"vm_password": vm_password} if vm_password else {})), f)


def vm_password(base_vm):
    """The encryption password a Base guest's VM (and so its clones) needs, or None."""
    try:
        return json.loads(_credentials_path(base_vm).read_text(encoding="utf-8")).get("vm_password")
    except (OSError, ValueError):
        return None


class VmrunChannel(Channel):
    """vmrun guest operations through VMware Tools: slow (several vmrun calls per command) but
    independent of the Guest's network and sshd. vmrun returns no output, so each call's
    script writes stdout, stderr and the exit code to files named for the call alone."""

    name = "vmrun"

    def __init__(self, vm, base_vm):
        self.vm = vm
        self.base_vm = base_vm

    def _auth(self):
        try:
            creds = credentials(self.base_vm)
        except GuestError as exc:  # the Channel cannot carry the call; SSH may
            raise ChannelError(exc.message, exc.fix)
        return self.vm.auth + ["-gu", creds["user"], "-gp", creds["password"]]

    def exec(self, argv, timeout, env, stdin=None):
        deadline = hostpower.awake_time() + timeout
        timed_out = GuestTimeout("%s timed out after %ss on Channel vmrun and was killed" % (list(argv), timeout))
        auth = self._auth()
        call = "/tmp/vmlab-call-%s" % uuid.uuid4().hex
        command = " ".join(shlex.quote(a) for a in (["env"] + ["%s=%s" % kv for kv in sorted(env.items())] if env else []) + list(argv))
        script = "\n".join([
            "find /tmp -maxdepth 1 -name 'vmlab-call-*' -mmin +60 -delete 2>/dev/null",
            # What an SSH login has: the user's HOME and PATH, and the user's systemd and D-Bus.
            'HOME=$(getent passwd "$(id -un)" | cut -d: -f6); cd "$HOME" 2>/dev/null || cd /',
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin",
            'XDG_RUNTIME_DIR=/run/user/$(id -u); DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus',
            "export HOME PATH XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS",
            # Killed in the Guest too, so a call that timed out does not run on there.
            "timeout -s KILL %d %s < %s > %s.out 2> %s.err" % (int(timeout) + 1, command, (call + ".in") if stdin is not None else "/dev/null", call, call),
            "echo $? > %s.code" % call,
        ])  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            (local / "script").write_text(script + "\n", encoding="utf-8")
            self._vmrun(["copyFileFromHostToGuest", self.vm.vmx, local / "script", call + ".sh"], deadline, auth, timed_out)
            if stdin is not None:
                with (local / "stdin").open("wb") as f:
                    shutil.copyfileobj(stdin, f)
                self._vmrun(["copyFileFromHostToGuest", self.vm.vmx, local / "stdin", call + ".in"], deadline, auth, timed_out)
            self._vmrun(["runProgramInGuest", self.vm.vmx, "/bin/sh", call + ".sh"], deadline, auth, timed_out)
            for suffix in ("out", "err", "code"):
                self._vmrun(["copyFileFromGuestToHost", self.vm.vmx, "%s.%s" % (call, suffix), local / suffix], deadline, auth, timed_out)
            read = lambda name: (local / name).read_text(encoding="utf-8", errors="replace")  # noqa: E731
            return ExecResult(list(argv), int(read("code").strip() or 255), read("out"), read("err"))

    def _made(self, directory, auth):
        """Is directory there in the Guest (made now or before)?"""
        code, out = vmrun(["createDirectoryInGuest", self.vm.vmx, directory], CALL_TIMEOUT, auth)
        return code == 0 or "exist" in out.lower()

    def send_file(self, local, guest_path, timeout):
        auth = self._auth()
        self._vmrun(["copyFileFromHostToGuest", self.vm.vmx, local, guest_path], hostpower.awake_time() + timeout, auth,
                    GuestTimeout("copying %s into %s did not finish within %ss and was killed" % (local, self.vm.name, timeout)))  # fmt: skip

    def _vmrun(self, args, deadline, auth, timed_out):
        """One vmrun step of a call that must end by deadline; timed_out is what to raise if it does not."""
        remaining = deadline - hostpower.awake_time()
        if remaining <= 0:
            raise timed_out
        try:
            code, out = vmrun(args, remaining, auth)
        except GuestTimeout:
            raise timed_out
        except GuestError as exc:
            raise ChannelError(exc.message, exc.fix)
        if code:
            raise ChannelError(
                "vmrun %s failed: %s" % (args[0], out.strip().splitlines()[-1] if out.strip() else "exit %s" % code),
                "check that VMware Tools run in the Guest (open-vm-tools) and the Guest credentials are right",
            )


class WindowsVmrunChannel(VmrunChannel):
    """vmrun -interactive into a Windows Guest's desktop session. The call's script
    (vmlab.providers.windows) goes in, runs, and its one result file comes back:
    three vmrun calls, four with stdin. vmrun refuses until the user is logged in."""

    def __init__(self, vm, base_vm):
        super().__init__(vm, base_vm)
        self._call_dir = False

    def exec(self, argv, timeout, env, stdin=None):
        deadline = hostpower.awake_time() + timeout
        timed_out = GuestTimeout("%s timed out after %ss on Channel vmrun and was killed" % (list(argv), timeout))
        auth = self._auth()
        call = windows.new_call()
        self._make_call_dir(auth)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            (local / "script").write_text(windows.call_script(call, argv, env, timeout, stdin is not None), encoding="ascii")
            self._vmrun(["copyFileFromHostToGuest", self.vm.vmx, local / "script", call + ".ps1"], deadline, auth, timed_out)
            if stdin is not None:
                with (local / "stdin").open("wb") as f:
                    shutil.copyfileobj(stdin, f)
                self._vmrun(["copyFileFromHostToGuest", self.vm.vmx, local / "stdin", call + ".in"], deadline, auth, timed_out)
            self._vmrun(["runProgramInGuest", self.vm.vmx, "-interactive"] + windows.runner_argv(call), deadline, auth, timed_out)
            self._vmrun(["copyFileFromGuestToHost", self.vm.vmx, call + ".result", local / "result"], deadline, auth, timed_out)
            return windows.parse_result(argv, (local / "result").read_text(encoding="utf-8", errors="replace"), self.name)

    def send_file(self, local, guest_path, timeout):
        # Files go to the call folder (copy_in's archive, the call server's script), which may not
        # be there yet: ssh can fail before this Channel's first call.
        self._make_call_dir(self._auth())
        super().send_file(local, guest_path, timeout)

    def _make_call_dir(self, auth):
        if not self._call_dir:
            # vmrun makes them, and says so when they are already there. Remembered only once it
            # worked: while the Guest boots these fail, and a Channel that gave up on them would
            # then fail every later call.
            made = [self._made(directory, auth) for directory in windows.CALL_DIRS]
            self._call_dir = all(made)


def defaults(os_name):
    """A Lab's [labs.<name>.fusion] options when it sets none."""
    return WINDOWS_DEFAULTS if os_name == "windows" else DEFAULTS


class FusionProvider(Provider):
    HOST_SCREENSHOTS = True
    SUPPORTED_OS = ("linux", "windows")
    HYPERVISOR = "VMware Fusion"

    def __init__(self, project, lab):
        super().__init__(project, lab)
        self.options = dict(defaults(lab.os), **lab.options)
        self.vm = FusionVM(vmx_path(self.guest_id), secrets=bases.vm_name(self.base_name))
        self._channels = None
        self._seen_session = None  # the session type the desktop probe last found
        self._booting = False  # started by this process and not reachable yet

    @classmethod
    def validate_options(cls, config_path, key, options, os_name):
        allowed = defaults(os_name)
        for k in sorted(set(options) - set(allowed)):
            raise ConfigError(
                config_path, "%s.%s" % (key, k), "unknown key" + (" for a Windows Lab" if k in DEFAULTS else ""), "remove it; allowed keys: %s" % ", ".join(allowed)
            )
        base = options.get("base", allowed["base"])
        if not (isinstance(base, str) and bases.NAME.match(base)):
            raise ConfigError(config_path, key + ".base", "must be a Base guest name", 'e.g. base = "%s" (see `vmlab base list`)' % allowed["base"])
        cpu = options.get("cpu", allowed["cpu"])
        if isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 1:
            raise ConfigError(config_path, key + ".cpu", "must be a whole number of CPUs >= 1", "e.g. cpu = 4")
        channels = options.get("channels", allowed["channels"])
        if not (isinstance(channels, list) and channels and all(c in CHANNELS for c in channels) and len(set(channels)) == len(channels)):
            raise ConfigError(
                config_path, key + ".channels", "must list Channels from: %s" % ", ".join(CHANNELS), "e.g. channels = %s" % json.dumps(allowed["channels"])
            )
        if options.get("session", DEFAULTS["session"]) not in SESSIONS:
            raise ConfigError(config_path, key + ".session", "must be one of: %s" % ", ".join(sorted(SESSIONS)), 'e.g. session = "x11"')
        language = options.get("language", allowed.get("language"))
        if os_name == "windows" and not (isinstance(language, str) and LANGUAGE.match(language)):
            raise ConfigError(config_path, key + ".language", "must be a Windows language tag", 'e.g. language = "en-US" (the default)')

    @property
    def windows(self):
        return self.lab.os == "windows"

    @property
    def base_name(self):
        return self.options["base"]

    def _base(self):
        """The Base guest's registry record; GuestError when it has not been created."""
        record = bases.Registry().get(self.base_name)
        if not record or record.get("provider") != "fusion" or not record.get("provisioned") or not FusionVM(record["vmx"]).exists():
            raise GuestError(
                "Base guest %s has not been created on this Host" % self.base_name,
                self._create_fix(),
            )
        return record

    def _create_fix(self):
        if self.windows:
            return ('make a Windows 11 VM with Fusion\'s "Get Windows from Microsoft", then run `vmlab base create %s` in a terminal window: '
                    "a wizard that adopts a copy of it" % self.base_name)
        return "vmlab base create %s   (downloads its installer the first time, after asking)" % self.base_name

    @classmethod
    def hypervisor(cls):
        return hypervisor()

    def detect(self):
        return hypervisor()

    def diagnose(self):
        from vmlab.providers import fusion_windows

        name = self.base_name
        check = "Base guest %s" % name
        try:
            record = bases.Registry().get(name)
        except GuestError as exc:
            return [(check, FAIL, exc.message, exc.fix)]
        if not record or record.get("provider") != "fusion":
            return [(check, FAIL, "not created on this Host", self._create_fix())]
        if not record.get("provisioned"):
            return [(check, FAIL, "created, but its install or provisioning did not finish", "vmlab base create %s   (it resumes where it failed)" % name)]
        base_vm = record.get("vm", bases.vm_name(name))
        base = FusionVM(record["vmx"], secrets=base_vm)
        if not base.exists():
            return [(check, FAIL, "registered, but its VM %s is gone" % record["vmx"], "vmlab base create %s" % name)]
        findings = []
        try:
            credentials(base_vm)
        except GuestError as exc:
            if self.windows:  # its VM is encrypted: vmrun cannot even open it
                return [("Guest credentials", FAIL, "%s: vmrun cannot open the encrypted VM without them" % exc.message, "vmlab base create %s --reprovision" % name)]
            if "vmrun" in self.options["channels"]:
                findings.append(("Guest credentials", WARN, "%s: the vmrun Channel needs them" % exc.message, "vmlab base create %s --reprovision" % name))
        version = fusion_windows.PROVISION_VERSION if self.windows else PROVISION_VERSION
        problems = []
        if record["provisioned"] != version:
            again = "vmlab base create %s   (provisions it again%s; Lab clones are then made again)" % (name, ", after one click on Windows' UAC prompt" if self.windows else "")
            problems.append((check, WARN, "provisioned by an older vmlab (v%s; this one provisions v%s)" % (record["provisioned"], version), again))
        try:
            snapshots, running = base.snapshots(), base.is_running()
        except GuestError as exc:
            return findings + problems + [(check, FAIL, exc.message, exc.fix)]
        if record["snapshot"] not in snapshots:
            problems.append((check, FAIL, "its snapshot %s is missing: Labs are cloned from it" % record["snapshot"], "vmlab base create %s --reprovision" % name))
        if running:
            problems.append((check, WARN, "running: Labs cannot clone it while it runs", stop_hint(name)))
        findings += problems or [(check, OK, "provisioned (v%s), VM %s" % (record["provisioned"], base.vmx), None)]
        if self.windows and not record.get("elevated"):
            findings.append(("Elevation", WARN, "the Guest asks before elevating (UAC), which nothing can answer unattended",
                             "vmlab base create %s   (asks for one click on the UAC prompt)" % name))  # fmt: skip
        if "ssh" in self.options["channels"] and not is_pinned(base_vm):
            findings.append(("SSH host key", WARN, "none pinned for %s: the ssh Channel refuses its Guests" % base_vm, "vmlab base create %s --reprovision" % name))
        if self.windows and not self.vm.is_running():  # a running Guest is asked instead: diagnose_guest
            language = record.get("language")
            if language:
                findings.append(self._language_finding(language, "Base guest %s" % name, WARN))
            else:
                findings.append(("Display language", INFO, "not recorded for Base guest %s (provisioned by an older vmlab); checked while the Guest runs" % name,
                                 "vmlab base create %s --reprovision   (records it)" % name))  # fmt: skip
        return findings + self._diagnose_clone(record)

    def _language_finding(self, language, where, status):
        """Does where (the Base guest as recorded, or the running Guest) show Windows in the Lab's language?
        A mismatch is status: a Base guest's does not stop its Guest from starting."""
        wanted = self.options["language"]
        if language.lower() == wanted.lower():
            return ("Display language", OK, language, None)
        return ("Display language", status, "%s shows Windows in %s, but the Lab expects %s: element names (buttons, menus, windows) come in the display language" % (where, language, wanted),
                "make a Windows VM in %s with Fusion's \"Get Windows from Microsoft\" (it asks for the language), then adopt it in place of the current one: "
                "vmlab base create %s --image PATH/TO/ITS.vmx (Labs copy the new one at their next start); or, if the Lab's Scenarios are written for %s, "
                'set language = "%s" under [labs.%s.fusion]' % (wanted, self.base_name, language, language, self.lab.name))  # fmt: skip

    def _diagnose_clone(self, record):
        up = "`vmlab up %s` makes it again" % self.lab.name
        clone = _clone_record(self.guest_id)
        if not self.vm.exists():
            return [("Clone", INFO, "none yet; `vmlab up %s` clones Base guest %s" % (self.lab.name, self.base_name), None)]
        if clone.get("made_from") != bases.provisioning(record):
            return [("Clone", INFO, "made from an earlier provisioning of %s; %s" % (self.base_name, up), None)]
        made_for = clone.get("session", DEFAULTS["session"])
        if made_for != self.session:
            return [("Clone", INFO, "made for the %s session; %s for %s" % (SESSION_NAMES.get(made_for, made_for), up, SESSION_NAMES[self.session]), None)]
        try:
            clean = CLEAN_SNAPSHOT in self.vm.snapshots()
        except GuestError as exc:
            return [("Clone", WARN, exc.message, exc.fix)]
        if not clean:
            return [("Clone", INFO, "has no Clean state: making it did not finish; %s" % up, None)]
        return [("Clone", OK, str(self.vm.vmx), None)]

    def diagnose_guest(self):
        if self.windows:
            return [self._diagnose_language(), self._diagnose_clock()]
        if not self.session:
            return []
        if self._seen_session != self.session:
            seen = SESSION_NAMES.get(self._seen_session, self._seen_session or "no known session")
            return [("Desktop session", FAIL, "the Lab asks for %s, but the Guest logged into %s" % (SESSION_NAMES[self.session], seen),
                     "look at its screen (vmlab ui screenshot --lab %s); `vmlab down %s && vmlab up %s` boots it again" % ((self.lab.name,) * 3))]  # fmt: skip
        extensions = self._diagnose_extensions() if self.session == "wayland" else []
        return [("Desktop session", OK, SESSION_NAMES[self.session], None)] + extensions + [self._diagnose_tray(), self._diagnose_recorder()]

    def _diagnose_recorder(self):
        """`ui notifications` reads what vmlab's Notification recorder, a user unit, writes down."""
        try:
            result = self.exec(["systemctl", "--user", "show", "-p", "LoadState", "-p", "ActiveState", "--value", RECORDER_UNIT], CALL_TIMEOUT)
        except GuestError as exc:
            return ("Notifications", WARN, "cannot ask systemd about the recorder: %s" % exc.message, exc.fix)
        load, active = (result.stdout.split() + ["", ""])[:2]
        if active == "active":
            return ("Notifications", OK, "recorded (%s is running)" % RECORDER_UNIT, None)
        if load != "loaded":
            return ("Notifications", WARN, "no Notification recorder: Base guest %s was provisioned by an older vmlab, so `ui notifications` fails" % self.base_name,
                    "vmlab base create %s --reprovision   (the Lab is cloned again at its next start)" % self.base_name)  # fmt: skip
        return ("Notifications", WARN, "the Notification recorder is %s, so Notifications go unrecorded" % (active or "not running"),
                "look at `vmlab exec --lab %s -- journalctl --user -u %s`; restarting the Guest (vmlab down %s && vmlab up %s) starts it again"
                % (self.lab.name, RECORDER_UNIT, self.lab.name, self.lab.name))  # fmt: skip

    def _diagnose_tray(self):
        """Tray icons (and so `ui tray`) need a StatusNotifierWatcher on the session bus: the panel's."""
        try:
            result = self.exec(["sh", "-c", TRAY_WATCHER], CALL_TIMEOUT)
        except GuestError as exc:
            return ("Tray icons", WARN, "cannot ask the session bus: %s" % exc.message, exc.fix)
        if result.stdout.strip() == "(true,)":
            return ("Tray icons", OK, "the panel shows them (a StatusNotifierWatcher is on the session bus)", None)
        return ("Tray icons", WARN, "no StatusNotifierWatcher on the session bus, so the panel shows no Tray icons and `ui tray` finds none",
                "wait for the desktop and run doctor again; if it stays missing, restart the Guest (vmlab down %s && vmlab up %s)" % (self.lab.name, self.lab.name))  # fmt: skip

    def _diagnose_extensions(self):
        """vmlab's Shell extension drives Wayland sessions; GNOME may have turned all extensions off."""
        try:
            result = self.exec(["gsettings", "get", "org.gnome.shell", "disable-user-extensions"], CALL_TIMEOUT)
        except GuestError as exc:
            return [("Shell extensions", WARN, "cannot read GNOME's settings: %s" % exc.message, exc.fix)]
        if result.stdout.strip() != "true":
            return [("Shell extensions", OK, "on", None)]
        return [("Shell extensions", FAIL, "GNOME Shell turned user extensions off (it does when it stops within its first minute), so vmlab's extension is off",
                 "vmlab base create %s   (provisioning v%s locks them on; the Lab is cloned again at its next start)" % (self.base_name, PROVISION_VERSION))]  # fmt: skip

    def _diagnose_clock(self):
        from vmlab.providers import fusion_windows

        cause, detail, fix = fusion_windows.guest_clock(self)
        if cause is None:
            return ("Clock", OK, detail, None)
        if cause == fusion_windows.WRONG_ZONE:  # the Lab boots with its Base guest's time zone: set it there
            fix = ("%s. Do it in Base guest %s, which the Lab starts from: vmlab base create %s --reprovision (in a terminal window: "
                   "it waits while you do it; the Lab is copied again at its next start)" % (fix, self.base_name, self.base_name))  # fmt: skip
        elif cause == fusion_windows.CLOCK_OFF:  # it reads the Mac's time again when it starts: vmlab-clean is taken powered off
            fix = "restart the Guest: vmlab down %s && vmlab up %s" % (self.lab.name, self.lab.name)
        return ("Clock", WARN, detail, fix)

    def _diagnose_language(self):
        from vmlab.providers import fusion_windows

        try:
            result = self.exec(fusion_windows.DISPLAY_LANGUAGE, CALL_TIMEOUT)
        except GuestError as exc:
            return ("Display language", WARN, "cannot read it: %s" % exc.message, exc.fix)
        if not (result.ok and result.stdout.strip()):
            return ("Display language", WARN, "cannot read it: %s" % (result.stderr.strip() or "no answer"), "check the Guest's PowerShell")
        return self._language_finding(result.stdout.strip(), "the Guest", FAIL)

    def is_running(self):
        return self.vm.is_running()

    @property
    def session(self):
        """The Linux desktop session type; None on Windows."""
        return self.options.get("session")

    def start(self):
        record = self._base()
        self._close_channels()
        made_from = _clone_record(self.guest_id).get("made_from")
        if self.vm.exists() and not self._clone_is_current(record):
            with self.progress.step("deleting the old clone"):
                self.vm.delete(self.lab.boot_timeout)
        if not self.vm.exists() and self.vm.vmx.parent.exists():
            with self.progress.step("deleting the old clone"):
                self.vm.delete(self.lab.boot_timeout)  # a copy or clone that did not finish: start over
        if not self.vm.exists():
            base_vm = FusionVM(record["vmx"], secrets=self.vm.secrets)
            if base_vm.is_running():
                raise GuestError("Base guest %s is running; it must be stopped to be cloned" % self.base_name, stop_hint(self.base_name))
            with self.progress.step("cloning"):
                if self.windows:
                    base_vm.clone_copy(self.vm)
                    self.vm.revert(record["snapshot"], self.lab.boot_timeout)  # the copy's snapshots came along
                else:
                    base_vm.clone_linked(self.vm, record["snapshot"], self.lab.boot_timeout)
                made_from = bases.provisioning(record)
                self._write_clone_record(made_from)
                if self.session not in (None, DEFAULTS["session"]):
                    self._switch_session()
                self.vm.snapshot(CLEAN_SNAPSHOT, self.lab.boot_timeout)
        self._write_clone_record(made_from)
        self._configure()
        self._booting = True
        with self.progress.step("booting"):
            self.vm.start(self.lab.boot_timeout)

    def _clone_is_current(self, record):
        """Whether the existing clone is one start() keeps: made from the Base guest's current
        provisioning, for the Lab's session, with its Clean state taken."""
        clone = _clone_record(self.guest_id)
        return (
            clone.get("made_from") == bases.provisioning(record)  # else the Base guest was provisioned again: the clone lacks what changed
            and clone.get("session", DEFAULTS["session"]) == self.session
            and CLEAN_SNAPSHOT in self.vm.snapshots()  # else its session switch did not finish
        )

    def _write_clone_record(self, made_from):
        _write_clone_record(self.guest_id, {
            "project": str(self.project.root), "lab": self.lab.name, "base": self.base_name, "made_from": made_from,
            "session": self.session,
        })  # fmt: skip

    def _configure(self):
        # At every start: reverting to vmlab-clean brings back the settings of the moment it was taken.
        # No sound device: a Guest must not play through the Host's speakers nor take its Bluetooth headset.
        self.vm.set_config({"numvcpus": self.options["cpu"], "memsize": int(self.lab.memory_gb * 1024), "sound.present": "FALSE"})

    def _switch_session(self):
        """Boot a new clone once to make the Lab's session its autologin session, then stop it."""
        name = SESSION_NAMES[self.session]
        self._configure()
        self.vm.start(self.lab.boot_timeout)
        try:
            deadline = hostpower.awake_time() + self.lab.boot_timeout  # the Host's awake time, as up()'s
            while not self.is_reachable():
                if hostpower.awake_time() >= deadline:
                    late = self._boot_timeout()
                    raise GuestError("the new clone of Lab %s was not switched to the %s session: %s" % (self.lab.name, name, late.message), late.fix)
                time.sleep(BOOT_POLL_SECONDS)
            result = self.exec(["/bin/sh", "-c", SWITCH_SESSION, "sh", SESSIONS[self.session], self.session], CALL_TIMEOUT)
            if not result.ok:
                raise GuestError(
                    "switching Lab %s to the %s session failed: %s" % (self.lab.name, name, result.stderr.strip()),
                    "re-provision its Base guest, which then installs it: vmlab base create %s --reprovision" % self.base_name,
                )
        finally:
            self.stop()

    def up(self):
        super().up()
        self._booting = False
        if self.session and self._seen_session != self.session:
            raise GuestError(
                "Lab %s asks for the %s session, but its Guest logged into %s" % (self.lab.name, SESSION_NAMES[self.session], SESSION_NAMES.get(self._seen_session, self._seen_session or "no known session")),
                "look at its screen (vmlab ui screenshot --lab %s); `vmlab down %s && vmlab up %s` boots it again"
                % (self.lab.name, self.lab.name, self.lab.name),
            )

    def wrap_argv(self, argv, env):
        if self.lab.os != "linux":
            return argv
        # The caller's env again after the session's: what the caller set wins.
        return ["/bin/sh", "-c", SESSION_ENV, "sh"] + (["env"] + ["%s=%s" % kv for kv in sorted(env.items())] if env else []) + argv

    def stop(self):
        self._close_channels()
        self.vm.stop(self.lab.boot_timeout)

    def is_reachable(self):
        if not self.is_running():
            return False
        self.vm.forget_ip()  # a stale address must not stick while the Guest boots
        # A probe, not a Run's call: trying the next Channel after a timeout is safe here.
        if self.windows:
            probes = (self._probes(channel, self.probe_argv()) for channel in self.channels())
            # While it boots, every Channel, not the first that answers: sshd and vmlab's call server
            # are up before vmrun agrees that someone is logged in ("The specified guest user must
            # be logged in interactively"), and a Run must not start while a Channel still refuses.
            # A Guest that was already running has passed that: probing vmrun again costs seconds.
            return all(probes) if self._booting else any(probes)
        # While it boots, every Channel, as for Windows: until VMware Tools publish the new address, ssh may
        # be pointed at an old one (the snapshot's, an old DHCP lease), and a Run must not start with it.
        results = (self._probes(channel, DESKTOP_PROBE) for channel in self.channels())  # it also puts a session off the screen back
        answers = list(results) if self._booting else [next((r for r in results if r), None)]
        if not all(answers):
            return False
        self._seen_session = answers[0].stdout.strip()
        return True

    def boot_state(self):
        """VMware Tools start with the OS: while they do not answer, the boot never got that far."""
        try:
            code, tools = vmrun(["checkToolsState", self.vm.vmx], CALL_TIMEOUT, self.vm.auth)
        except GuestError:
            return None
        if code:
            return None
        if tools.strip() != "running":
            return BootState(False, "VMware Tools never answered")
        try:
            code, ip = vmrun(["readVariable", self.vm.vmx, "guestVar", "ip"], CALL_TIMEOUT, self.vm.auth)
        except GuestError:
            code, ip = 1, ""
        ip = ip.strip() if code == 0 else ""
        return BootState(True, "VMware Tools answer, " + ("IP %s" % ip if ip else "no IP address yet"))

    def _probes(self, channel, probe):
        """The probe's result over channel when it worked, else None."""
        try:
            result = channel.exec(probe, PROBE_TIMEOUT, {})
        except GuestError:
            return None
        return result if result.ok else None

    def channels(self):
        if self._channels is None:
            record = bases.Registry().get(self.base_name) or {}
            base_vm = record.get("vm", bases.vm_name(self.base_name))
            ssh, vmrun_channel = (windows.WindowsSshChannel, WindowsVmrunChannel) if self.windows else (SshChannel, VmrunChannel)
            by_name = {
                "ssh": ssh(record.get("user", GUEST_USER), self.vm.ip, base_vm, self.guest_id),
                "vmrun": vmrun_channel(self.vm, base_vm),
            }
            self._channels = [by_name[name] for name in self.options["channels"]]
        return self._channels

    def _close_channels(self):
        for channel in self.channels():
            if isinstance(channel, SshChannel):
                channel.close()

    def restore(self):
        """Clean state is the clone's vmlab-clean snapshot. A clone start() would make again is
        not reverted first: its new clone is Clean state already."""
        if self.is_running():
            self.stop()
        if self.vm.exists() and self._clone_is_current(self._base()):
            self.vm.revert(CLEAN_SNAPSHOT, self.lab.boot_timeout)
        self.up()

    def copy_in(self, src, guest_dir, timeout=None):
        return windows.copy_in(self, src, guest_dir, timeout) if self.windows else self.copy_in_by_tar(src, guest_dir, timeout)

    def screenshot(self, dest):
        self.vm.screenshot(dest, self.lab.step_timeout)


class HostVMs:
    """vmlab's Fusion VMs on this Host (all under $VMLAB_HOME/fusion), for `vmlab clean` (vmlab.clean)."""

    provider = "fusion"
    service_suffixes = (CLONE_RECORD, CREDENTIALS)

    @property
    def service_dir(self):
        return fusion_dir()

    @staticmethod
    def default_base(lab):
        return defaults(lab.os)["base"]

    def vms(self):
        names = [p.name[: -len(".vmwarevm")] for p in fusion_dir().glob("*.vmwarevm") if vmx_path(p.name[: -len(".vmwarevm")]).is_file()]
        running = running_vmx() if names else set()
        return {name: os.path.realpath(str(vmx_path(name))) in running for name in names}

    def delete_vm(self, name):
        """Delete the VM name, encrypted or not, stopping it first if it runs.

        An encrypted VM opens only with the password vmlab keeps for a Base guest: its own, the
        one of the Base guest its clone record names, or — when that record is what went missing,
        which is why `vmlab clean` calls it a leftover — any Base guest's."""
        vmx = vmx_path(name)
        if not _encrypted(vmx):
            FusionVM(vmx).delete()
            return
        base = _clone_record(name).get("base")
        others = sorted(path.name[: -len(CREDENTIALS)] for path in fusion_dir().glob("*" + CREDENTIALS))
        candidates = ([bases.vm_name(base)] if base else []) + others
        failed = None
        for secrets in dict.fromkeys(candidates):  # in order, without repeats
            vm = FusionVM(vmx, secrets=secrets)
            if not vm.password_known():
                continue
            try:
                vm.delete()
                return
            except GuestError as exc:
                if not vm.exists():
                    return
                failed = failed or exc
        raise failed or GuestError(
            "%s is encrypted and vmlab has no password for it" % vmx, "delete it in Fusion, where its password is in your Keychain"
        )

    def stop_hint(self, name):
        """How the person stops the Base guest whose VM is name (`vmlab clean` stops a Lab's clone itself)."""
        return stop_hint(fusion_bases()[name][0])

    def old_snapshots(self, vms):
        return old_snapshots(vms)


def _encrypted(vmx):
    """Does this VM need a password to open (Fusion encrypts every VM with a TPM)?"""
    try:
        return "encryption.keySafe" in vmx.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _clone_record(vm):
    try:
        return json.loads((fusion_dir() / (vm + CLONE_RECORD)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_clone_record(vm, record):
    (fusion_dir() / (vm + CLONE_RECORD)).write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")



def create_base(name, image, confirm, reprovision, out):
    """Install Ubuntu unattended into the Base guest's VM (asking before a download),
    provision it over SSH, reboot into the desktop, prove both Channels work and
    snapshot it for Lab clones. Idempotent: each finished stage is skipped on a re-run."""
    if platform.system() != "Darwin":
        raise GuestError("VMware Fusion Base guests need a Mac Host", "run this on a Mac")
    running_vmx()  # Fusion is installed and answers
    registry = bases.Registry()
    vm = FusionVM(vmx_path(bases.vm_name(name)))
    record = registry.get(name) or {}
    sound_off(vm, out)
    if (
        record.get("provisioned") == PROVISION_VERSION and vm.exists() and record.get("snapshot") in vm.snapshots()
        and not reprovision
    ):  # fmt: skip
        shut_down_for_labs(vm, out)
        out("Base guest %s is ready (Fusion VM %s)" % (name, vm.vmx))
    else:
        if not (record.get("installed") and vm.exists()):
            iso = _installer_iso(name, image, confirm, out)
            _install(name, vm, iso, out)
        _provision(name, vm, out)
    delete_old_snapshots(name, vm, confirm, out)


def _installer_iso(name, image, confirm, out):
    """The local installer ISO for image, a path or a URL; a URL is downloaded once, after asking."""
    if not re.match(r"^https?://", image):
        path = Path(image).expanduser()
        if not path.is_file():
            raise GuestError("installer ISO %s does not exist" % path, "pass --image with the path of an Ubuntu desktop ISO, or leave --image out to download one")
        return path.resolve()
    cache = vmlab_home() / "images" / image.rsplit("/", 1)[-1]
    if cache.is_file():
        return cache
    if not confirm("Download %s (about 4 GB) for Base guest %s?" % (image, name)):
        raise GuestError(
            "not downloading %s without confirmation" % image,
            "re-run with --yes to allow the download, or run it in a terminal to be asked",
        )
    cache.parent.mkdir(mode=0o700, exist_ok=True)
    expected = _published_sha256(image)
    partial = cache.with_name(cache.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(image, timeout=60) as response, partial.open("wb") as f:
            total = int(response.headers.get("Content-Length") or 0)
            done, shown = 0, -1
            for chunk in iter(lambda: response.read(1 << 20), b""):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total and done * 10 // total > shown:
                    shown = done * 10 // total
                    out("  downloaded %d%% of %.1f GB" % (shown * 10, total / 1e9))
    except OSError as exc:
        raise GuestError("downloading %s failed: %s" % (image, exc), "check your network and re-run")
    if digest.hexdigest() != expected:
        partial.unlink()
        raise GuestError("%s does not match its published SHA-256" % image, "re-run to download it again")
    partial.replace(cache)
    return cache


def _published_sha256(url):
    sums = url.rsplit("/", 1)[0] + "/SHA256SUMS"
    try:
        with urllib.request.urlopen(sums, timeout=60) as response:
            text = response.read().decode("utf-8", "replace")
    except OSError as exc:
        raise GuestError("cannot fetch %s to verify the download: %s" % (sums, exc), "check your network and re-run")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == url.rsplit("/", 1)[-1]:
            return parts[0]
    raise GuestError("%s lists no checksum for %s" % (sums, url), "pass a local ISO with --image")


def _install(name, vm, iso, out):
    """A fresh VM, installed unattended from iso with vmlab's user, key and pinned host key."""
    if vm.exists() or vm.vmx.parent.exists():  # an install that did not finish: start over
        vm.delete()
    folder = vm.vmx.parent
    folder.mkdir(parents=True)
    password = secrets.token_urlsafe(18)
    save_credentials(vm.name, GUEST_USER, password)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _host_tool(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", vm.name, "-f", str(tmp / "host_key")])
        host_key, host_key_pub = (tmp / "host_key").read_text(), (tmp / "host_key.pub").read_text().strip()
        seed = tmp / "seed"
        seed.mkdir()
        hostname = re.sub(r"[^a-z0-9-]", "-", vm.name)
        (seed / "user-data").write_text(_user_data(hostname, _sha512_crypt(password, secrets.token_hex(8)), host_key, host_key_pub))
        (seed / "meta-data").write_text("instance-id: %s\nlocal-hostname: %s\n" % (uuid.uuid4().hex, hostname))
        # cloud-init in the installer's live session finds this disc by its label.
        _host_tool(["hdiutil", "makehybrid", "-quiet", "-o", str(folder / "seed.iso"), str(seed),
                    "-iso", "-joliet", "-iso-volume-name", "CIDATA", "-joliet-volume-name", "CIDATA"])  # fmt: skip
    _host_tool([str(Path(vmrun_binary()).parent.parent / "Library" / "vmware-vdiskmanager"), "-q", "-c", "-s", BASE_DISK, "-a", "lsilogic", "-t", "0", str(folder / "disk.vmdk")])
    serial = folder / "install-serial.log"
    vm.vmx.write_text(_vmx(vm.name, iso, folder / "seed.iso", serial), encoding="utf-8")
    pin_host_key(vm.name, host_key_pub)
    bases.Registry().put(name, {
        "provider": "fusion", "os": "linux", "arch": host_arch(), "vm": vm.name, "vmx": str(vm.vmx), "image": str(iso),
        "user": GUEST_USER, "installed": False, "provisioned": None,
    })  # fmt: skip

    out("installing Ubuntu into %s, unattended (about 15 minutes; progress: %s)" % (vm.name, serial))
    vm.start()
    deadline, started, state, minute = hostpower.awake_time() + INSTALL_TIMEOUT, hostpower.awake_time(), None, 0
    while vm.is_running():
        now = _install_state(serial)
        if now != state or int((hostpower.awake_time() - started) // 300) > minute:
            state, minute = now, int((hostpower.awake_time() - started) // 300)
            out("  %d min: installer %s" % ((hostpower.awake_time() - started) // 60, (state or "starting").lower()))
        if hostpower.awake_time() >= deadline:
            shot = folder / "install-timeout.png"
            try:
                vm.screenshot(shot, CALL_TIMEOUT)
            except GuestError:
                pass
            raise GuestError(
                "the Ubuntu install in %s did not finish within %ss (installer state: %s)" % (vm.name, INSTALL_TIMEOUT, state),
                "look at %s and %s, then re-run `vmlab base create %s` to install again" % (shot, serial, name),
            )
        time.sleep(5)
    # The installer powers off only once DONE; it may do so before confirm-install.py reports it.
    if _install_state(serial) not in ("RUNNING", "UU_RUNNING", "LATE_COMMANDS", "DONE"):
        raise GuestError(
            "the Ubuntu install in %s stopped in state %s" % (vm.name, _install_state(serial)),
            "see %s (the installer's log is /var/log/installer in the Guest); re-run `vmlab base create %s`" % (serial, name),
        )
    # Detach the installer discs and the serial log: the Base guest boots from its disk from now on.
    vm.set_config({"sata0:1.present": "FALSE", "sata0:2.present": "FALSE", "serial0.present": "FALSE"})
    (folder / "seed.iso").unlink()
    record = bases.Registry().get(name)
    record["installed"] = True
    bases.Registry().put(name, record)
    out("  Ubuntu installed")


def _install_state(serial):
    """The installer's last state as confirm-install.py reported it on the serial port, or None."""
    try:
        text = serial.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    states = re.findall(r"vmlab-install: ([A-Z_]+)\s", text)
    return states[-1] if states else None


def _provision(name, vm, out):
    """Run provision.sh over SSH, reboot into the autologin desktop, check both Channels, snapshot."""
    record = bases.Registry().get(name)
    user = record["user"]
    ssh = SshChannel(user, vm.ip, vm.name, vm.name)
    try:
        if not vm.is_running():
            out("starting %s" % vm.name)
            vm.start()
        _wait(vm, ssh, ["true"], BASE_BOOT_TIMEOUT, "SSH with vmlab's key")
        out("provisioning %s" % vm.name)
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for source in ("shell-extension/metadata.json", "shell-extension/extension.js", RECORDER):
                    data = pkgutil.get_data("vmlab", "guest/linux/" + source)
                    info = tarfile.TarInfo(source.split("/")[-1])
                    info.size, info.mode = len(data), 0o644
                    tar.addfile(info, io.BytesIO(data))
            archive.seek(0)
            result = ssh.exec(["/bin/sh", "-c", 'rm -rf "$1" && mkdir -p "$1" && tar -xf - -C "$1"', "sh", EXTENSION_DIR], CALL_TIMEOUT, {}, stdin=archive)
        if not result.ok:
            raise GuestError("copying the Shell extension and the Notification recorder into %s failed: %s" % (vm.name, result.stderr.strip()), "re-run `vmlab base create %s`" % name)
        with tempfile.TemporaryFile() as stdin:
            stdin.write(pkgutil.get_data("vmlab", "guest/linux/provision.sh"))
            stdin.seek(0)
            result = ssh.exec(["/bin/bash", "-s", "--", EXTENSION_DIR], PROVISION_TIMEOUT, {}, stdin=stdin)
        for line in result.stdout.splitlines():
            out(line)
        if not result.ok:
            tail = "\n".join(result.stderr.strip().splitlines()[-15:])
            raise GuestError("provisioning %s failed:\n%s" % (vm.name, tail), "fix the cause, then re-run `vmlab base create %s`: provisioning is idempotent" % name)
        out("rebooting %s into the desktop session" % vm.name)
        ssh.close()
        vm.stop()
        vm.start()
        _wait(vm, ssh, DESKTOP_PROBE, BASE_BOOT_TIMEOUT, "the desktop session (autologin)")
        out("  Channel ssh reaches the desktop session")
        vmrun_channel = VmrunChannel(vm, vm.name)
        result = vmrun_channel.exec(DESKTOP_PROBE, CALL_TIMEOUT, {})
        if not result.ok:
            raise GuestError("the vmrun Channel cannot see the desktop session: %s" % result.stderr.strip(), "re-run `vmlab base create %s --reprovision`" % name)
        out("  Channel vmrun reaches the desktop session")
        out("  the UI helper reaches the desktop: %s" % _ui_helper_detail(ssh, name))
    except GuestError:
        try:
            vm.stop()  # a running Base guest cannot be cloned; a re-run starts it again
        except GuestError:
            pass
        raise
    finally:
        ssh.close()
    vm.stop()
    provisioned_id = uuid.uuid4().hex
    snapshot = provisioned_snapshot(provisioned_id)
    vm.snapshot(snapshot)  # earlier ones go once no Lab clone needs them: delete_old_snapshots
    record.update(provisioned=PROVISION_VERSION, provisioned_id=provisioned_id, snapshot=snapshot)
    bases.Registry().put(name, record)
    out('Base guest %s is ready (Fusion VM %s). Labs use it with: [labs.<name>.fusion] base = "%s"' % (name, vm.vmx, name))


def _ui_helper_detail(ssh, name):
    """What the UI helper reports once the desktop, and in it vmlab's Shell extension, answers it."""
    deadline = hostpower.awake_time() + BASE_BOOT_TIMEOUT
    while True:
        with tempfile.TemporaryFile() as stdin:
            stdin.write(pkgutil.get_data("vmlab", "guest/linux/vmlab-ui.py"))
            stdin.seek(0)
            result = ssh.exec(["python3", "-", "version"], CALL_TIMEOUT, {}, stdin=stdin)
        if result.ok:
            info = json.loads(result.stdout)
            return "%s session; input through %s" % (info["session"], info["input"])
        if hostpower.awake_time() >= deadline:
            raise GuestError("the UI helper cannot drive the desktop of %s: %s" % (name, result.stderr.strip()), "re-run `vmlab base create %s --reprovision`" % name)
        time.sleep(2)


def _wait(vm, channel, probe, timeout, what):
    deadline = hostpower.awake_time() + timeout
    while True:
        vm.forget_ip()
        try:
            if channel.exec(probe, CALL_TIMEOUT, {}).ok:
                return
        except GuestError:
            pass
        if hostpower.awake_time() >= deadline:
            raise GuestError("timed out after %ss waiting for %s in %s" % (timeout, what, vm.name), "look at its screen: vmrun start '%s' gui" % vm.vmx)
        time.sleep(2)


def _host_tool(argv):
    code, out, err = hostproc.run(argv, 300)
    if code:
        raise GuestError("`%s` failed: %s" % (" ".join(argv[:2]), (err or out).strip()), "run it by hand to see why")


def _user_data(hostname, password_hash, host_key, host_key_pub):
    """cloud-init user-data for the installer's live session: the autoinstall answers, and
    a job that confirms the install (confirm-install.py)."""
    confirm = base64.b64encode(pkgutil.get_data("vmlab", "guest/linux/confirm-install.py")).decode("ascii")
    sudoers = "/target/etc/sudoers.d/90-vmlab"
    key = "/target/etc/ssh/ssh_host_ed25519_key"
    data = {
        "autoinstall": {
            "version": 1,
            "interactive-sections": [],
            "locale": "en_US.UTF-8",
            "keyboard": {"layout": "us"},
            "timezone": "Etc/UTC",
            "identity": {"hostname": hostname, "username": GUEST_USER, "realname": "vmlab", "password": password_hash},
            "ssh": {"install-server": True, "allow-pw": False, "authorized-keys": [public_key()]},
            "storage": {"layout": {"name": "direct"}},
            "packages": ["open-vm-tools-desktop"],
            "late-commands": [
                "echo '%s ALL=(ALL) NOPASSWD:ALL' > %s && chmod 440 %s" % (GUEST_USER, sudoers, sudoers),
                # The host key vmlab pinned before the install; cloud-init must not replace it at first boot.
                "echo %s | base64 -d > %s && chmod 600 %s" % (base64.b64encode(host_key.encode()).decode(), key, key),
                "echo '%s' > %s.pub" % (host_key_pub, key),
                "rm -f /target/etc/ssh/ssh_host_rsa_key* /target/etc/ssh/ssh_host_ecdsa_key*",
                # An idle GNOME suspends the Guest, even at the login screen before provisioning.
                "for t in sleep suspend hibernate hybrid-sleep; do ln -sf /dev/null /target/etc/systemd/system/$t.target; done",
                "mkdir -p /target/etc/cloud/cloud.cfg.d && printf 'ssh_deletekeys: false\\nssh_genkeytypes: []\\n' > /target/etc/cloud/cloud.cfg.d/90-vmlab.cfg",
            ],
            "shutdown": "poweroff",
        },
        "runcmd": [
            "echo %s | base64 -d > /run/vmlab-confirm-install.py" % confirm,
            "systemd-run --unit=vmlab-confirm-install python3 /run/vmlab-confirm-install.py",
        ],
    }
    # JSON is YAML: no YAML writer needed.
    return "#cloud-config\n" + json.dumps(data, indent=2) + "\n"


def _vmx(name, iso, seed, serial):
    return "\n".join('%s = "%s"' % kv for kv in [
        (".encoding", "UTF-8"), ("config.version", "8"), ("virtualHW.version", "22"),
        ("displayName", name), ("guestOS", "arm-ubuntu-64" if host_arch() == "arm64" else "ubuntu-64"), ("firmware", "efi"),
        ("numvcpus", BASE_CPU), ("memsize", BASE_MEMORY_MB),
        ("pciBridge0.present", "TRUE"), ("pciBridge4.present", "TRUE"), ("pciBridge4.virtualDev", "pcieRootPort"),
        ("pciBridge4.functions", "8"), ("vmci0.present", "TRUE"), ("hpet0.present", "TRUE"),
        ("nvme0.present", "TRUE"), ("nvme0:0.present", "TRUE"), ("nvme0:0.fileName", "disk.vmdk"),
        ("sata0.present", "TRUE"),
        ("sata0:1.present", "TRUE"), ("sata0:1.deviceType", "cdrom-image"), ("sata0:1.fileName", iso),
        ("sata0:2.present", "TRUE"), ("sata0:2.deviceType", "cdrom-image"), ("sata0:2.fileName", seed),
        ("serial0.present", "TRUE"), ("serial0.fileType", "file"), ("serial0.fileName", serial),
        ("ethernet0.present", "TRUE"), ("ethernet0.connectionType", "nat"), ("ethernet0.addressType", "generated"),
        ("ethernet0.virtualDev", "e1000e"),
        ("usb.present", "TRUE"), ("usb_xhci.present", "TRUE"),
        # No sound: a Guest's audio device can grab the Host's Bluetooth headset.
        ("sound.present", "FALSE"), ("floppy0.present", "FALSE"),
        ("tools.syncTime", "TRUE"), ("tools.upgrade.policy", "manual"), ("mks.enable3d", "TRUE"),
    ]) + "\n"  # fmt: skip


def _sha512_crypt(password, salt, rounds=5000):
    """crypt(3)'s SHA-512 ($6$) hash of password: what the installer wants, and what neither
    macOS's crypt nor its LibreSSL can produce."""
    p, s = password.encode("utf-8"), salt.encode("ascii")[:16]
    sha = hashlib.sha512
    b = sha(p + s + p).digest()
    a = sha(p + s)
    a.update(b * (len(p) // 64) + b[: len(p) % 64])
    i = len(p)
    while i:
        a.update(b if i & 1 else p)
        i >>= 1
    c = a.digest()
    dp = sha(p * len(p)).digest()
    pp = dp * (len(p) // 64) + dp[: len(p) % 64]
    ds = sha(s * (16 + c[0])).digest()
    ss = ds * (len(s) // 64) + ds[: len(s) % 64]
    for r in range(rounds):
        h = sha(pp if r & 1 else c)
        if r % 3:
            h.update(ss)
        if r % 7:
            h.update(pp)
        h.update(c if r & 1 else pp)
        c = h.digest()
    alphabet = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    encoded = []
    groups = [(k, k + 21, k + 42) for k in range(21)]
    for k, group in enumerate(groups):
        x, y, z = group[k % 3:] + group[: k % 3]
        w = (c[x] << 16) | (c[y] << 8) | c[z]
        encoded += [alphabet[(w >> (6 * n)) & 63] for n in range(4)]
    encoded += [alphabet[(c[63] >> (6 * n)) & 63] for n in range(2)]
    return "$6$%s$%s" % (s.decode("ascii"), "".join(encoded))
