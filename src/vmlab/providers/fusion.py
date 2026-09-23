"""VMware Fusion Provider: Linux and Windows Guests.

A Lab's Guest is a clone of its Base guest's provisioned snapshot (`vmlab
base create ubuntu-26.04` or `windows-11`), so it costs little disk and no
project ever runs in the Base guest: a linked clone for Linux, an APFS copy
for Windows, whose VM is encrypted for its TPM and which vmrun cannot clone
(vmlab.providers.fusion_windows). Clean state is the clone's own snapshot,
vmlab-clean, taken when the clone is made: restoring reverts to it. VMs live
under $VMLAB_HOME/fusion, never in Fusion's own Virtual Machines folder.

Channels (ADR 0003): SSH first, with vmlab's key and the host key pinned when
the Base guest was made, and vmrun guest operations as the fallback. On Linux
SSH is multiplexed and runs the command itself; on Windows, where an SSH
session cannot reach the desktop, it hands the call to an interactive
Scheduled Task, as vmrun -interactive does (vmlab.providers.windows). vmrun takes the
Guest user's password, and an encrypted VM's own password, from vmlab's home
on its command line, so they show in the Host's process list for the length of
a call; they guard a throwaway Guest on Fusion's private NAT network.
Screenshots are Host-side (vmcli MKS captureScreenshot): no Guest credentials,
no Wayland consent dialog.

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

from vmlab import bases, hostproc
from vmlab.config import ConfigError, host_arch
from vmlab.home import vmlab_home
from vmlab.providers import windows
from vmlab.providers.base import BOOT_POLL_SECONDS, FAIL, INFO, OK, WARN, Channel, ChannelError, ExecResult, GuestError, GuestTimeout, Provider
from vmlab.providers.ssh import SshChannel, is_pinned, pin_host_key, public_key

DEFAULTS = {"base": "ubuntu-26.04", "cpu": 4, "channels": ["ssh", "vmrun"], "session": "wayland"}
WINDOWS_DEFAULTS = {"base": "windows-11", "cpu": 4, "channels": ["ssh", "vmrun"]}
CHANNELS = ("ssh", "vmrun")
# The autologin session (name of its .desktop file) for each session type. Base guests boot into Wayland.
SESSIONS = {"wayland": "ubuntu", "x11": "xfce"}
SESSION_NAMES = {"wayland": "Wayland", "x11": "X11"}
FUSION_APP = "/Applications/VMware Fusion.app"
INSTALL_FIX = (
    "install VMware Fusion Pro (free; ask before installing anything on the Host): download it from "
    "Broadcom's support portal (a free Broadcom account; Homebrew has no cask for it) and drag it to /Applications"
)
CALL_TIMEOUT = 60  # s for quick vmrun commands
SEND_FILE_TIMEOUT = 600  # s for one file (a Build artifact) into a Guest
DHCP_LEASES = "/var/db/vmware/vmnet-dhcpd-vmnet8.leases"  # Fusion's NAT network
PROBE_TIMEOUT = 15  # s per reachability probe; vmrun may hang while the Guest boots
INSTALL_TIMEOUT = 2 * 3600  # s for the unattended install, which downloads updates
BASE_BOOT_TIMEOUT = 600
PROVISION_TIMEOUT = 1800
PROVISION_VERSION = 2  # bump when provision.sh or the Shell extension changes; `base create` then re-provisions
BASE_CPU, BASE_MEMORY_MB, BASE_DISK = 4, 4096, "64GB"
GUEST_USER = "vmlab"
STOP_GRACE = 60  # s a Guest gets to shut down before it is powered off
DISK_OP_TIMEOUT = 600  # s for clone, snapshot, revert and delete while creating a Base guest
CLEAN_SNAPSHOT = "vmlab-clean"
# The graphical session is up (autologin done) and has published its environment: a Run
# can drive the desktop. Prints the session type. logind knows the type; the user manager's
# environment may still be the previous session's until the new one imports its own.
DESKTOP_PROBE = ["/bin/sh", "-c", """
uid=$(id -u); XDG_RUNTIME_DIR=/run/user/$uid; export XDG_RUNTIME_DIR
s=$(loginctl show-user "$uid" -p Display --value 2>/dev/null); [ -n "$s" ] || exit 1
t=$(loginctl show-session "$s" -p Type --value) || exit 1
env=$(systemctl --user show-environment 2>/dev/null) || exit 1
printf '%s\\n' "$env" | grep -qx "XDG_SESSION_TYPE=$t" || exit 1
case $t in
wayland) systemctl --user is-active --quiet graphical-session.target ;;
x11) DISPLAY=$(printf '%s\\n' "$env" | sed -n 's/^DISPLAY=//p') XAUTHORITY=$(printf '%s\\n' "$env" | sed -n 's/^XAUTHORITY=//p') wmctrl -m >/dev/null 2>&1 ;;
*) false ;;
esac && echo "$t"
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
EXTENSION_DIR = "/tmp/vmlab-shell-extension"  # where provisioning finds the Shell extension's files
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
        return self.exists() and os.path.realpath(str(self.vmx)) in running_vmx()

    def start(self, timeout=CALL_TIMEOUT, gui=False):
        """Power on, without a window unless gui (for steps a person does in the Guest);
        returns once the VM runs, not once it has booted."""
        self._ip = None
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
        try:
            if vmrun(["stop", self.vmx, "soft"], timeout, self.auth)[0] == 0:
                return
        except GuestTimeout:
            pass
        if self.is_running():
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
        match = re.search(r'^ethernet0\.generatedAddress = "([0-9a-f:]+)"', self.vmx.read_text(encoding="utf-8"), re.M) if self.exists() else None
        try:
            leases = Path(DHCP_LEASES).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        found = re.findall(r"lease (\S+) \{[^}]*hardware ethernet %s;" % re.escape(match.group(1)), leases) if match else []
        return found[-1] if found else None  # the file only grows: the last lease is the newest

    def forget_ip(self):
        self._ip = None

    def snapshots(self):
        out = vmrun_ok(["listSnapshots", self.vmx], CALL_TIMEOUT, self.auth)
        return [line.strip() for line in out.splitlines()[1:] if line.strip()]

    def snapshot(self, name, timeout=DISK_OP_TIMEOUT):
        vmrun_ok(["snapshot", self.vmx, name], timeout, self.auth)

    def revert(self, name, timeout):
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
            raise GuestError("`vmrun deleteVM %s` failed: %s" % (self.vmx, out.strip()), "delete it in Fusion, then retry")
        if self.vmx.parent.exists():  # vmrun leaves logs behind, and knows nothing of a VM that never started
            shutil.rmtree(str(self.vmx.parent), ignore_errors=True)

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
        deadline = time.time() + timeout
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

    def send_file(self, local, guest_path):
        auth = self._auth()
        deadline = time.time() + SEND_FILE_TIMEOUT
        # A ChannelError, not a timeout that fails the Run: copying a file again over another
        # Channel is safe, unlike running a command twice.
        self._vmrun(["copyFileFromHostToGuest", self.vm.vmx, local, guest_path], deadline, auth,
                    ChannelError("copying %s into %s did not finish within %ss" % (local, self.vm.name, SEND_FILE_TIMEOUT),
                                 "check the Guest's disk space and that VMware Tools run"))  # fmt: skip

    def _vmrun(self, args, deadline, auth, timed_out):
        """One vmrun step of a call that must end by deadline; timed_out is what to raise if it does not."""
        remaining = deadline - time.time()
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
        deadline = time.time() + timeout
        timed_out = GuestTimeout("%s timed out after %ss on Channel vmrun and was killed" % (list(argv), timeout))
        auth = self._auth()
        call = windows.new_call()
        if not self._call_dir:
            # vmrun makes them, and says so when they are already there. Remembered only once it
            # worked: while the Guest boots these fail, and a Channel that gave up on them would
            # then fail every later call.
            made = [self._made(directory, auth) for directory in windows.CALL_DIRS]
            self._call_dir = all(made)
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


def defaults(os_name):
    """A Lab's [labs.<name>.fusion] options when it sets none."""
    return WINDOWS_DEFAULTS if os_name == "windows" else DEFAULTS


class FusionProvider(Provider):
    SUPPORTED_OS = ("linux", "windows")
    HYPERVISOR = "VMware Fusion"

    def __init__(self, project, lab):
        super().__init__(project, lab)
        self.options = dict(defaults(lab.os), **lab.options)
        self.vm = FusionVM(vmx_path(self.guest_id), secrets=bases.vm_name(self.base_name))
        self._channels = None
        self._seen_session = None  # the session type the desktop probe last found

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
                "vmlab base create %s   (downloads its installer the first time, after asking)" % self.base_name,
            )
        return record

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
            return [(check, FAIL, "not created on this Host", "vmlab base create %s   (downloads its installer the first time, after asking)" % name)]
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
            stop = "shut Windows down from its Start menu" if self.windows else "'%s' -T fusion stop '%s'" % (vmrun_binary(), base.vmx)
            problems.append((check, WARN, "running: Labs cannot clone it while it runs", stop))
        findings += problems or [(check, OK, "provisioned (v%s), VM %s" % (record["provisioned"], base.vmx), None)]
        if self.windows and not record.get("elevated"):
            findings.append(("Elevation", WARN, "the Guest asks before elevating (UAC), which nothing can answer unattended",
                             "vmlab base create %s   (asks for one click on the UAC prompt)" % name))  # fmt: skip
        if "ssh" in self.options["channels"] and not is_pinned(base_vm):
            findings.append(("SSH host key", WARN, "none pinned for %s: the ssh Channel refuses its Guests" % base_vm, "vmlab base create %s --reprovision" % name))
        return findings + self._diagnose_clone(record)

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
        if not self.session:
            return []
        if self._seen_session != self.session:
            seen = SESSION_NAMES.get(self._seen_session, self._seen_session or "no known session")
            return [("Desktop session", FAIL, "the Lab asks for %s, but the Guest logged into %s" % (SESSION_NAMES[self.session], seen),
                     "look at its screen (vmlab ui screenshot --lab %s); `vmlab down %s && vmlab up %s` boots it again" % ((self.lab.name,) * 3))]  # fmt: skip
        return [("Desktop session", OK, SESSION_NAMES[self.session], None)]

    def is_running(self):
        return self.vm.is_running()

    @property
    def session(self):
        """The Linux desktop session type; None on Windows."""
        return self.options.get("session")

    def start(self):
        record = self._base()
        self._close_channels()
        clone = _clone_record(self.guest_id)
        made_from = clone.get("made_from")
        if self.vm.exists() and (
            made_from != bases.provisioning(record)  # the Base guest was provisioned again: the clone lacks what changed
            or clone.get("session", DEFAULTS["session"]) != self.session
            or CLEAN_SNAPSHOT not in self.vm.snapshots()  # its session switch did not finish
        ):
            self.vm.delete(self.lab.boot_timeout)
        if not self.vm.exists() and self.vm.vmx.parent.exists():
            self.vm.delete(self.lab.boot_timeout)  # a copy or clone that did not finish: start over
        if not self.vm.exists():
            base_vm = FusionVM(record["vmx"], secrets=self.vm.secrets)
            if base_vm.is_running():
                raise GuestError("Base guest %s is running; it must be stopped to be cloned" % self.base_name, "vmrun stop '%s'" % base_vm.vmx)
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
        self.vm.start(self.lab.boot_timeout)

    def _write_clone_record(self, made_from):
        _write_clone_record(self.guest_id, {
            "project": str(self.project.root), "lab": self.lab.name, "base": self.base_name, "made_from": made_from,
            "session": self.session,
        })  # fmt: skip

    def _configure(self):
        # At every start: reverting to vmlab-clean brings back the settings of the moment it was taken.
        self.vm.set_config({"numvcpus": self.options["cpu"], "memsize": int(self.lab.memory_gb * 1024)})

    def _switch_session(self):
        """Boot a new clone once to make the Lab's session its autologin session, then stop it."""
        name = SESSION_NAMES[self.session]
        self._configure()
        self.vm.start(self.lab.boot_timeout)
        try:
            deadline = time.time() + self.lab.boot_timeout
            while not self.is_reachable():
                if time.time() >= deadline:
                    raise GuestError(
                        "the new clone of Lab %s did not reach its desktop within %ss, so it was not switched to the %s session"
                        % (self.lab.name, self.lab.boot_timeout, name),
                        "raise labs.%s.boot_timeout if it is just slow; `vmlab up %s` tries again" % (self.lab.name, self.lab.name),
                    )
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
            # Every Channel, not the first that answers: sshd and its Scheduled Task are up
            # before vmrun agrees that someone is logged in ("The specified guest user must be
            # logged in interactively"), and a Run must not start while a Channel still refuses.
            return all(self._probes(channel, self.probe_argv()) for channel in self.channels())
        for channel in self.channels():
            result = self._probes(channel, DESKTOP_PROBE)
            if result:
                self._seen_session = result.stdout.strip()
                return True
        return False

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
        """Clean state is the clone's vmlab-clean snapshot."""
        if self.is_running():
            self.stop()
        if self.vm.exists():
            self.vm.revert(CLEAN_SNAPSHOT, self.lab.boot_timeout)
        self.up()

    def copy_in(self, src, guest_dir):
        return windows.copy_in(self, src, guest_dir) if self.windows else self.copy_in_by_tar(src, guest_dir)

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
        """Delete the VM name, encrypted or not.

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
        return "vmrun stop '%s'" % vmx_path(name)


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
    if (
        record.get("provisioned") == PROVISION_VERSION and vm.exists() and record.get("snapshot") in vm.snapshots()
        and not reprovision
    ):  # fmt: skip
        out("Base guest %s is ready (Fusion VM %s)" % (name, vm.vmx))
        return
    if not (record.get("installed") and vm.exists()):
        iso = _installer_iso(name, image, confirm, out)
        _install(name, vm, iso, out)
    _provision(name, vm, out)


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
    deadline, started, state, minute = time.time() + INSTALL_TIMEOUT, time.time(), None, 0
    while vm.is_running():
        now = _install_state(serial)
        if now != state or int((time.time() - started) // 300) > minute:
            state, minute = now, int((time.time() - started) // 300)
            out("  %d min: installer %s" % ((time.time() - started) // 60, (state or "starting").lower()))
        if time.time() >= deadline:
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
                for filename in ("metadata.json", "extension.js"):
                    data = pkgutil.get_data("vmlab", "guest/linux/shell-extension/" + filename)
                    info = tarfile.TarInfo(filename)
                    info.size, info.mode = len(data), 0o644
                    tar.addfile(info, io.BytesIO(data))
            archive.seek(0)
            result = ssh.exec(["/bin/sh", "-c", 'rm -rf "$1" && mkdir -p "$1" && tar -xf - -C "$1"', "sh", EXTENSION_DIR], CALL_TIMEOUT, {}, stdin=archive)
        if not result.ok:
            raise GuestError("copying the Shell extension into %s failed: %s" % (vm.name, result.stderr.strip()), "re-run `vmlab base create %s`" % name)
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
    finally:
        ssh.close()
    vm.stop()
    provisioned_id = uuid.uuid4().hex
    snapshot = "vmlab-provisioned-%s" % provisioned_id[:12]
    # Earlier snapshots stay: Lab clones made from them still need them until they are re-cloned.
    vm.snapshot(snapshot)
    record.update(provisioned=PROVISION_VERSION, provisioned_id=provisioned_id, snapshot=snapshot)
    bases.Registry().put(name, record)
    out('Base guest %s is ready (Fusion VM %s). Labs use it with: [labs.<name>.fusion] base = "%s"' % (name, vm.vmx, name))


def _ui_helper_detail(ssh, name):
    """What the UI helper reports once the desktop, and in it vmlab's Shell extension, answers it."""
    deadline = time.time() + BASE_BOOT_TIMEOUT
    while True:
        with tempfile.TemporaryFile() as stdin:
            stdin.write(pkgutil.get_data("vmlab", "guest/linux/vmlab-ui.py"))
            stdin.seek(0)
            result = ssh.exec(["python3", "-", "version"], CALL_TIMEOUT, {}, stdin=stdin)
        if result.ok:
            info = json.loads(result.stdout)
            return "%s session; input through %s" % (info["session"], info["input"])
        if time.time() >= deadline:
            raise GuestError("the UI helper cannot drive the desktop of %s: %s" % (name, result.stderr.strip()), "re-run `vmlab base create %s --reprovision`" % name)
        time.sleep(2)


def _wait(vm, channel, probe, timeout, what):
    deadline = time.time() + timeout
    while True:
        vm.forget_ip()
        try:
            if channel.exec(probe, CALL_TIMEOUT, {}).ok:
                return
        except GuestError:
            pass
        if time.time() >= deadline:
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
