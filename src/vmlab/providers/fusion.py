"""VMware Fusion Provider: Linux Guests (Windows: planned).

A Lab's Guest is a linked clone of its Base guest's provisioned snapshot
(`vmlab base create ubuntu-26.04`), so it costs little disk and no project
ever runs in the Base guest. Clean state is the clone's own snapshot,
vmlab-clean, taken when the clone is made: restoring reverts to it. VMs live
under $VMLAB_HOME/fusion, never in Fusion's own Virtual Machines folder.

Channels (ADR 0003): SSH, multiplexed, with vmlab's key and the host key
pinned when the Base guest was installed; vmrun guest operations as the
fallback, with the Guest user's password from vmlab's home. vmrun needs that
password on its command line, so it shows in the Host's process list for the
length of a call; it guards a throwaway Guest on Fusion's private NAT network.
Screenshots are Host-side (vmcli MKS captureScreenshot): no Guest credentials,
no Wayland consent dialog.

Options, under [labs.<name>.fusion]:

    base = "ubuntu-26.04"     # Base guest to clone (vmlab base list)
    cpu = 4
    channels = ["ssh", "vmrun"]
"""

import base64
import hashlib
import json
import os
import pkgutil
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from vmlab import bases, hostproc
from vmlab.config import ConfigError, host_arch
from vmlab.home import vmlab_home
from vmlab.providers.base import Channel, ChannelError, ExecResult, GuestError, GuestTimeout, Provider
from vmlab.providers.ssh import SshChannel, pin_host_key, public_key

DEFAULTS = {"base": "ubuntu-26.04", "cpu": 4, "channels": ["ssh", "vmrun"]}
CHANNELS = ("ssh", "vmrun")
FUSION_APP = "/Applications/VMware Fusion.app"
INSTALL_FIX = (
    "install VMware Fusion (free; ask before installing anything on the Host): "
    "brew install --cask vmware-fusion, or download it from Broadcom's support portal"
)
CALL_TIMEOUT = 60  # s for quick vmrun commands
DHCP_LEASES = "/var/db/vmware/vmnet-dhcpd-vmnet8.leases"  # Fusion's NAT network
PROBE_TIMEOUT = 15  # s per reachability probe; vmrun may hang while the Guest boots
INSTALL_TIMEOUT = 2 * 3600  # s for the unattended install, which downloads updates
BASE_BOOT_TIMEOUT = 600
PROVISION_TIMEOUT = 1800
PROVISION_VERSION = 1  # bump when provision.sh changes; `base create` then re-provisions
BASE_CPU, BASE_MEMORY_MB, BASE_DISK = 4, 4096, "64GB"
GUEST_USER = "vmlab"
STOP_GRACE = 60  # s a Guest gets to shut down before it is powered off
DISK_OP_TIMEOUT = 600  # s for clone, snapshot, revert and delete while creating a Base guest
CLEAN_SNAPSHOT = "vmlab-clean"
# The graphical session is up (autologin done): a Run can drive the desktop.
DESKTOP_PROBE = ["/bin/sh", "-c", 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user is-active --quiet graphical-session.target']
# $VMLAB_HOME/fusion/<clone>.json: which project, Lab and Base guest a clone serves, and which
# provisioning of the Base guest it was made from. `vmlab clean` uses it to find orphans (vmlab.clean).
CLONE_RECORD = ".json"
CREDENTIALS = ".credentials.json"  # a Base guest's user and password


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


def running_vmx():
    """Real paths of the .vmx files of running VMs."""
    out = vmrun_ok(["list"], CALL_TIMEOUT)
    return {os.path.realpath(line.strip()) for line in out.splitlines()[1:] if line.strip()}


class FusionVM:
    """One Fusion VM by .vmx path: the Host-side lifecycle shared by Base guests and Lab clones."""

    def __init__(self, vmx):
        self.vmx = Path(vmx)
        self._ip = None

    @property
    def name(self):
        return self.vmx.stem

    def exists(self):
        return self.vmx.is_file()

    def is_running(self):
        return self.exists() and os.path.realpath(str(self.vmx)) in running_vmx()

    def start(self, timeout=CALL_TIMEOUT):
        """Power on without a window; returns once the VM runs, not once it has booted."""
        self._ip = None
        # Not through pipes: vmrun leaves a process behind that holds them open.
        log = self.vmx.parent / "vmlab-start.log"
        try:
            with log.open("w") as out:
                code = subprocess.run(
                    [vmrun_binary(), "-T", "fusion", "start", str(self.vmx), "nogui"],
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
            if vmrun(["stop", self.vmx, "soft"], timeout)[0] == 0:
                return
        except GuestTimeout:
            pass
        if self.is_running():
            vmrun_ok(["stop", self.vmx, "hard"], CALL_TIMEOUT)

    def ip(self):
        """The Guest's IP, or None. May be stale: reachability needs a probe.

        VMware Tools publish it as guestinfo.ip; `vmrun getGuestIPAddress` can claim Tools
        are not running for minutes after they started, so it is only the second source,
        and Fusion's DHCP lease for the VM's MAC the third."""
        if self._ip is None:
            for args in (["readVariable", self.vmx, "guestVar", "ip"], ["getGuestIPAddress", self.vmx]):
                code, out = vmrun(args, CALL_TIMEOUT)
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
        out = vmrun_ok(["listSnapshots", self.vmx], CALL_TIMEOUT)
        return [line.strip() for line in out.splitlines()[1:] if line.strip()]

    def snapshot(self, name, timeout=DISK_OP_TIMEOUT):
        vmrun_ok(["snapshot", self.vmx, name], timeout)

    def revert(self, name, timeout):
        vmrun_ok(["revertToSnapshot", self.vmx, name], timeout)

    def clone_linked(self, dest, snapshot, timeout):
        """Make dest (a FusionVM that does not exist yet) a linked clone of this VM's snapshot."""
        dest.vmx.parent.parent.mkdir(parents=True, exist_ok=True)
        vmrun_ok(["clone", self.vmx, dest.vmx, "linked", "-snapshot=%s" % snapshot, "-cloneName=%s" % dest.name], timeout)

    def delete(self, timeout=DISK_OP_TIMEOUT):
        if self.is_running():
            self.stop()
        code, out = vmrun(["deleteVM", self.vmx], timeout)
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
            code, out, err = hostproc.run([vmcli_binary(), str(self.vmx), "MKS", "captureScreenshot", str(dest)], timeout)
        except FileNotFoundError:
            raise GuestError("vmcli is missing from VMware Fusion", INSTALL_FIX)
        except subprocess.TimeoutExpired:
            raise GuestTimeout("screenshot of %s did not finish within %ss" % (self.name, timeout))
        if code or not dest.exists() or not dest.read_bytes()[:4] == b"\x89PNG":
            raise GuestError("screenshot of %s failed: %s" % (self.name, _without_noise(out + err).strip() or "no PNG written"), "check that the Guest is running: vmlab status")


def _credentials_path(base_vm):
    return fusion_dir() / (base_vm + CREDENTIALS)


def credentials(base_vm):
    """{"user", "password"} of a Base guest's user, shared by its clones."""
    path = _credentials_path(base_vm)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GuestError("the Guest credentials %s are missing" % path, "re-create the Base guest: vmlab base create NAME")


def save_credentials(base_vm, user, password):
    path = _credentials_path(base_vm)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"user": user, "password": password}, f)


class VmrunChannel(Channel):
    """vmrun guest operations through VMware Tools: slow (several vmrun calls per command) but
    independent of the Guest's network and sshd. vmrun returns no output, so each call's
    script writes stdout, stderr and the exit code to files named for the call alone."""

    name = "vmrun"

    def __init__(self, vm, base_vm):
        self.vm = vm
        self.base_vm = base_vm

    def exec(self, argv, timeout, env, stdin=None):
        deadline = time.time() + timeout
        timed_out = GuestTimeout("%s timed out after %ss on Channel vmrun and was killed" % (list(argv), timeout))
        try:
            creds = credentials(self.base_vm)
        except GuestError as exc:  # the Channel cannot carry the call; SSH may
            raise ChannelError(exc.message, exc.fix)
        auth = ["-gu", creds["user"], "-gp", creds["password"]]
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


class FusionProvider(Provider):
    SUPPORTED_OS = ("linux",)

    def __init__(self, project, lab):
        super().__init__(project, lab)
        self.options = dict(DEFAULTS, **lab.options)
        self.vm = FusionVM(vmx_path(self.guest_id))
        self._channels = None

    @classmethod
    def validate_options(cls, config_path, key, options):
        for k in sorted(set(options) - set(DEFAULTS)):
            raise ConfigError(config_path, "%s.%s" % (key, k), "unknown key", "remove it; allowed keys: %s" % ", ".join(DEFAULTS))
        base = options.get("base", DEFAULTS["base"])
        if not (isinstance(base, str) and bases.NAME.match(base)):
            raise ConfigError(config_path, key + ".base", "must be a Base guest name", 'e.g. base = "ubuntu-26.04" (see `vmlab base list`)')
        cpu = options.get("cpu", DEFAULTS["cpu"])
        if isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 1:
            raise ConfigError(config_path, key + ".cpu", "must be a whole number of CPUs >= 1", "e.g. cpu = 4")
        channels = options.get("channels", DEFAULTS["channels"])
        if not (isinstance(channels, list) and channels and all(c in CHANNELS for c in channels) and len(set(channels)) == len(channels)):
            raise ConfigError(
                config_path, key + ".channels", "must list Channels from: %s" % ", ".join(CHANNELS), 'e.g. channels = ["ssh", "vmrun"]'
            )

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

    def detect(self):
        if platform.system() != "Darwin":
            return False, "VMware Fusion runs on macOS Hosts only", "run this Lab on a Mac"
        try:
            running_vmx()
            self._base()
        except GuestError as exc:
            return False, exc.message, exc.fix
        return True, "VMware Fusion; Base guest %s" % self.base_name, None

    def is_running(self):
        return self.vm.is_running()

    def start(self):
        record = self._base()
        self._close_channels()
        made_from = _clone_record(self.guest_id).get("made_from")
        if self.vm.exists() and made_from != bases.provisioning(record):
            # The Base guest was provisioned again since: the clone lacks what changed.
            self.vm.delete(self.lab.boot_timeout)
        if not self.vm.exists():
            base_vm = FusionVM(record["vmx"])
            if base_vm.is_running():
                raise GuestError("Base guest %s is running; it must be stopped to be cloned" % self.base_name, "vmrun stop '%s'" % base_vm.vmx)
            base_vm.clone_linked(self.vm, record["snapshot"], self.lab.boot_timeout)
            self.vm.snapshot(CLEAN_SNAPSHOT, self.lab.boot_timeout)
            made_from = bases.provisioning(record)
        _write_clone_record(self.guest_id, {"project": str(self.project.root), "lab": self.lab.name, "base": self.base_name, "made_from": made_from})
        # At every start: reverting to vmlab-clean brings back the settings of the moment it was taken.
        self.vm.set_config({"numvcpus": self.options["cpu"], "memsize": int(self.lab.memory_gb * 1024)})
        self.vm.start(self.lab.boot_timeout)

    def stop(self):
        self._close_channels()
        self.vm.stop(self.lab.boot_timeout)

    def is_reachable(self):
        if not self.is_running():
            return False
        self.vm.forget_ip()  # a stale address must not stick while the Guest boots
        # A probe, not a Run's call: trying the next Channel after a timeout is safe here.
        for channel in self.channels():
            try:
                if channel.exec(DESKTOP_PROBE, PROBE_TIMEOUT, {}).ok:
                    return True
            except GuestError:
                continue
        return False

    def channels(self):
        if self._channels is None:
            record = bases.Registry().get(self.base_name) or {}
            base_vm = record.get("vm", bases.vm_name(self.base_name))
            by_name = {
                "ssh": SshChannel(record.get("user", "vmlab"), self.vm.ip, base_vm, self.guest_id),
                "vmrun": VmrunChannel(self.vm, base_vm),
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
        return self.copy_in_by_tar(src, guest_dir)

    def screenshot(self, dest):
        self.vm.screenshot(dest, self.lab.step_timeout)


class HostVMs:
    """vmlab's Fusion VMs on this Host (all under $VMLAB_HOME/fusion), for `vmlab clean` (vmlab.clean)."""

    provider = "fusion"
    default_base = DEFAULTS["base"]
    service_suffixes = (CLONE_RECORD, CREDENTIALS)

    @property
    def service_dir(self):
        return fusion_dir()

    def vms(self):
        names = [p.name[: -len(".vmwarevm")] for p in fusion_dir().glob("*.vmwarevm") if vmx_path(p.name[: -len(".vmwarevm")]).is_file()]
        running = running_vmx() if names else set()
        return {name: os.path.realpath(str(vmx_path(name))) in running for name in names}

    def delete_vm(self, name):
        FusionVM(vmx_path(name)).delete()

    def stop_hint(self, name):
        return "vmrun stop '%s'" % vmx_path(name)


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
        with tempfile.TemporaryFile() as stdin:
            stdin.write(pkgutil.get_data("vmlab", "guest/linux/provision.sh"))
            stdin.seek(0)
            result = ssh.exec(["/bin/bash", "-s"], PROVISION_TIMEOUT, {}, stdin=stdin)
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
