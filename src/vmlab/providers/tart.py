"""Tart Provider: macOS Guests on Apple Silicon.

A Lab's Guest is an APFS clone of a Base guest (`vmlab base create`), so it
costs almost no disk; restoring Clean state means deleting the clone and
cloning again. Guests run headless, without clipboard or audio sharing: shared
clipboards overwrite what the user copied, and a Guest's audio device can grab
the Host's Bluetooth headset.

Channels (ADR 0003): SSH, multiplexed, with vmlab's key; `tart exec` (the
Tart guest agent over vsock) as the fallback. Both run in the logged-in GUI
session and hold the TCC grants provisioning writes. Screenshots are taken in
the Guest with screencapture: Tart has no Host-side screenshot.

Options, under [labs.<name>.tart]:

    base = "macos-tahoe"      # Base guest to clone (vmlab base list)
    cpu = 4
    display = "1920x1080"     # fixed, so coordinates stay stable between Runs
    channels = ["ssh", "exec"]
"""

import base64
import json
import os
import pkgutil
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

from vmlab import bases, hostproc, uihelpers
from vmlab.config import ConfigError
from vmlab.home import vmlab_home
from vmlab.providers.base import Channel, ChannelError, ExecResult, GuestError, GuestTimeout, Provider
from vmlab.providers.ssh import SshChannel, pin_host_key, public_key

DEFAULTS = {"base": "macos-tahoe", "cpu": 4, "display": "1920x1080", "channels": ["ssh", "exec"]}
CHANNELS = ("ssh", "exec")
INSTALL_FIX = "install Tart (ask before installing anything on the Host): brew install cirruslabs/cli/tart"
RUN_FLAGS = ["--no-graphics", "--no-clipboard", "--no-audio"]
PATH_APPEND = ("/usr/local/bin", "/opt/homebrew/bin")  # what `tart exec` has on PATH, so both Channels agree
CALL_TIMEOUT = 60  # s for quick tart subcommands
STOP_GRACE = 30  # s a Guest gets to shut down before tart forces it off
CLONE_TIMEOUT = 4 * 3600  # s for a first clone, which downloads the image
BASE_BOOT_TIMEOUT = 600
PROVISION_TIMEOUT = 900
PROVISION_VERSION = 3  # bump when provision.sh or the UI helper changes; `base create` then re-provisions
DESKTOP_PROBE = ["/usr/bin/pgrep", "-qx", "Dock"]  # the GUI session is up
SYSTEM_EVENTS_PROBE = ["/usr/bin/osascript", "-e", 'tell application "System Events" to count processes']
SYSTEM_EVENTS_TIMEOUT = 30  # s; longer means a TCC consent dialog is waiting for a click
# `tart exec` passes the command's exit code and stderr through, so only Tart's
# own phrasing (naming the VM, or its agent connection) marks a Channel failure.
TART_EXEC_FAILURE = r'^(the specified VM "{vm}" does not exist|VM "{vm}" is not running|.*(guest agent|gRPC|UNAVAILABLE|vsock))'
CLONE_STATE = "base-provisioned"  # which provisioning version of its Base guest a Lab clone was made from


def tart_binary():
    return os.environ.get("VMLAB_TART") or shutil.which("tart") or "tart"


def tart(args, timeout, stdin=None):
    """(code, stdout, stderr) of a tart subcommand; GuestError if Tart is missing, GuestTimeout if it hangs."""
    argv = [tart_binary()] + [str(a) for a in args]
    try:
        return hostproc.run(argv, timeout, stdin=stdin)
    except FileNotFoundError:
        raise GuestError("Tart is not installed (%s not found)" % argv[0], INSTALL_FIX)
    except subprocess.TimeoutExpired:
        raise GuestTimeout("`tart %s` did not finish within %ss and was killed" % (" ".join(argv[1:3]), timeout))


def tart_ok(args, timeout):
    code, out, err = tart(args, timeout)
    if code:
        raise GuestError("`tart %s` failed: %s" % (" ".join(str(a) for a in args), (err or out).strip()), "run it by hand to see why")
    return out


def list_vms(source=None):
    """{name: row} of Tart's VMs; source is "local", "oci" or None for both."""
    out = tart_ok(["list", "--format", "json"] + (["--source", source] if source else []), CALL_TIMEOUT)
    return {row["Name"]: row for row in json.loads(out or "[]")}


def _tail(text, lines=15):
    return "\n".join(text.rstrip().splitlines()[-lines:]) or "(no output)"


class TartVM:
    """One Tart VM by name: the Host-side lifecycle shared by Base guests and Lab clones."""

    def __init__(self, name):
        self.name = name
        self.log_path = vmlab_home() / "tart" / ("%s.log" % name)
        self._run = None  # the `tart run` process this object started
        self._ip = None

    def row(self):
        return list_vms("local").get(self.name)

    def is_running(self):
        row = self.row()
        return bool(row and row.get("Running"))

    def start(self):
        """Power on in the background; `tart run` lives as long as the VM does."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._ip = None
        with self.log_path.open("wb") as log:
            try:
                self._run = subprocess.Popen(
                    [tart_binary(), "run", self.name] + RUN_FLAGS,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError:
                raise GuestError("Tart is not installed", INSTALL_FIX)

    def check_alive(self):
        """Raise if the `tart run` this object started has exited, e.g. refused to start."""
        if self._run is None or self._run.poll() is None or self.is_running():
            return
        log = self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
        raise GuestError(
            "Tart VM %s exited while starting (code %s):\n%s" % (self.name, self._run.returncode, _tail(log)),
            "macOS runs at most two macOS VMs at once; see `tart list` and stop one. Full log: %s" % self.log_path,
        )

    def stop(self, timeout):
        # `tart stop` powers a macOS Guest off at once: writes still in its page
        # cache are lost. Flush them first, best effort.
        try:
            tart(["exec", self.name, "/bin/sync"], CALL_TIMEOUT)
        except GuestError:
            pass
        tart(["stop", self.name, "--timeout", STOP_GRACE], timeout)
        self._ip = None

    def ip(self):
        """The VM's IP from its DHCP lease, or None. The lease may be stale: reachability needs a probe."""
        if self._ip is None:
            code, out, _ = tart(["ip", self.name, "--wait", 0], CALL_TIMEOUT)
            self._ip = (out.strip() or None) if code == 0 else None
        return self._ip

    def forget_ip(self):
        self._ip = None


class TartExecChannel(Channel):
    """`tart exec`: the Tart guest agent, over vsock. Needs no network and no key."""

    name = "exec"

    def __init__(self, vm):
        self.vm = vm

    def exec(self, argv, timeout, env, stdin=None):
        command = ["exec"] + (["-i"] if stdin is not None else []) + [self.vm.name]
        if env:
            command += ["/usr/bin/env"] + ["%s=%s" % kv for kv in sorted(env.items())]
        try:
            code, out, err = tart(command + list(argv), timeout, stdin=stdin)
        except GuestTimeout:
            raise GuestTimeout("%s timed out after %ss on Channel exec and was killed" % (list(argv), timeout))
        except GuestError as exc:
            raise ChannelError(exc.message, exc.fix)
        last = err.strip().splitlines()[-1] if err.strip() else ""
        if code and re.match(TART_EXEC_FAILURE.format(vm=re.escape(self.vm.name)), last):
            raise ChannelError(
                "tart exec failed: %s" % last,
                "check that the Guest booted and runs the Tart guest agent (cirruslabs images do)",
            )
        return ExecResult(list(argv), code, out, err)


class TartProvider(Provider):
    SUPPORTED_OS = ("macos",)

    def __init__(self, project, lab):
        super().__init__(project, lab)
        self.options = dict(DEFAULTS, **lab.options)
        self.vm = TartVM(self.guest_id)
        self._channels = None

    @classmethod
    def validate_options(cls, config_path, key, options):
        for k in sorted(set(options) - set(DEFAULTS)):
            raise ConfigError(config_path, "%s.%s" % (key, k), "unknown key", "remove it; allowed keys: %s" % ", ".join(DEFAULTS))
        base = options.get("base", DEFAULTS["base"])
        if not (isinstance(base, str) and bases.NAME.match(base)):
            raise ConfigError(config_path, key + ".base", "must be a Base guest name", 'e.g. base = "macos-tahoe" (see `vmlab base list`)')
        cpu = options.get("cpu", DEFAULTS["cpu"])
        if isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 1:
            raise ConfigError(config_path, key + ".cpu", "must be a whole number of CPUs >= 1", "e.g. cpu = 4")
        display = options.get("display", DEFAULTS["display"])
        if not (isinstance(display, str) and re.match(r"^\d+x\d+$", display)):
            raise ConfigError(config_path, key + ".display", "must be WIDTHxHEIGHT", 'e.g. display = "1920x1080"')
        channels = options.get("channels", DEFAULTS["channels"])
        if not (isinstance(channels, list) and channels and all(c in CHANNELS for c in channels) and len(set(channels)) == len(channels)):
            raise ConfigError(
                config_path, key + ".channels", "must list Channels from: %s" % ", ".join(CHANNELS), 'e.g. channels = ["ssh", "exec"]'
            )

    @property
    def base_name(self):
        return self.options["base"]

    def _base(self):
        """The Base guest's registry record; GuestError when it has not been created."""
        record = bases.Registry().get(self.base_name)
        if not record or not record.get("provisioned") or bases.vm_name(self.base_name) not in list_vms("local"):
            raise GuestError(
                "Base guest %s has not been created on this Host" % self.base_name,
                "vmlab base create %s   (downloads its image the first time, after asking)" % self.base_name,
            )
        return record

    def detect(self):
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            return False, "Tart runs macOS Guests on Apple Silicon Macs only", "run this Lab on an Apple Silicon Mac"
        try:
            version = tart_ok(["--version"], CALL_TIMEOUT).strip()
            self._base()
        except GuestError as exc:
            return False, exc.message, exc.fix
        return True, "tart %s; Base guest %s" % (version, self.base_name), None

    def is_running(self):
        return self.vm.is_running()

    @property
    def _clone_state(self):
        return vmlab_home() / "tart" / ("%s.%s" % (self.guest_id, CLONE_STATE))

    def start(self):
        record = self._base()
        self._close_channels()
        vms = list_vms("local")
        base_vm = bases.vm_name(self.base_name)
        made_from = self._clone_state.read_text().strip() if self._clone_state.exists() else None
        if self.guest_id in vms and made_from != str(record["provisioned"]):
            # The Base guest was provisioned again since: the clone lacks its grants.
            tart_ok(["delete", self.guest_id], CALL_TIMEOUT)
            del vms[self.guest_id]
        if self.guest_id not in vms:
            if vms[base_vm].get("Running"):
                raise GuestError(
                    "Base guest %s is running; it must be stopped to be cloned" % self.base_name, "tart stop %s" % base_vm
                )
            tart_ok(["clone", base_vm, self.guest_id], self.lab.boot_timeout)
            tart_ok(["set", self.guest_id, "--random-mac"], CALL_TIMEOUT)  # clones of one Base guest run side by side
            self._clone_state.parent.mkdir(parents=True, exist_ok=True)
            self._clone_state.write_text(str(record["provisioned"]))
        memory_mb = int(self.lab.memory_gb * 1024)
        tart_ok(["set", self.guest_id, "--cpu", self.options["cpu"], "--memory", memory_mb, "--display", self.options["display"]], CALL_TIMEOUT)
        self.vm.start()

    def stop(self):
        self._close_channels()
        self.vm.stop(self.lab.boot_timeout)

    def is_reachable(self):
        self.vm.check_alive()
        if not self.is_running():
            return False
        self.vm.forget_ip()  # a stale lease must not stick while the Guest boots
        # A probe, not a Run's call: a timeout here just means "not yet", so trying
        # the next Channel is safe (ADR 0003 forbids it only for real calls).
        for channel in self.channels():
            try:
                if channel.exec(DESKTOP_PROBE, CALL_TIMEOUT, {}).ok:
                    return True
            except GuestError:
                continue
        return False

    def channels(self):
        if self._channels is None:
            record = bases.Registry().get(self.base_name) or {}
            by_name = {
                "ssh": SshChannel(record.get("user", "admin"), self.vm.ip, bases.vm_name(self.base_name), self.guest_id, PATH_APPEND),
                "exec": TartExecChannel(self.vm),
            }
            self._channels = [by_name[name] for name in self.options["channels"]]
        return self._channels

    def _close_channels(self):
        for channel in self.channels():
            if isinstance(channel, SshChannel):
                channel.close()

    def restore(self):
        """Clean state is a fresh clone of the Base guest."""
        if self.is_running():
            self.stop()
        if self.guest_id in list_vms("local"):
            tart_ok(["delete", self.guest_id], CALL_TIMEOUT)
        self.up()

    def copy_in(self, src, guest_dir):
        # A tar stream keeps bundles intact (symlinks, modes); the Guest-side
        # script expands ~ and prints the absolute folder.
        script = 'd=$1; case $d in "~"|"~/"*) d="$HOME${d#"~"}";; esac; mkdir -p "$d" && tar -xf - -C "$d" && cd "$d" && pwd'
        argv = ["/bin/sh", "-c", script, "sh", guest_dir]
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar_file:
                tar_file.add(str(src), arcname=src.name)
            result = self.exec(argv, self.lab.app.install_timeout, stdin=archive)
        if not result.ok:
            raise GuestError("copying %s into the Guest failed: %s" % (src, _tail(result.stderr)), "check free disk space in the Guest")
        return "%s/%s" % (result.stdout.strip(), src.name)

    def screenshot(self, dest):
        shot = "/tmp/vmlab-shot-%s.png" % uuid.uuid4().hex
        script = 'screencapture -x -t png "$1" && base64 -i "$1"; code=$?; rm -f "$1"; exit $code'
        result = self.exec(["/bin/sh", "-c", script, "sh", shot], self.lab.step_timeout)
        png = base64.b64decode(result.stdout) if result.ok else b""
        if not png.startswith(b"\x89PNG"):
            raise GuestError(
                "screencapture in Guest %s failed: %s" % (self.lab.name, _tail(result.stderr)),
                "the Screen Recording grant may be missing: vmlab base create %s --reprovision" % self.base_name,
            )
        dest.write_bytes(png)


def create_base(name, image, confirm, reprovision, out):
    """Clone image into the Base guest's VM (asking before a download), provision it,
    reboot, and prove both Channels reach System Events. Idempotent."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise GuestError("macOS Base guests need an Apple Silicon Mac", "run this on an Apple Silicon Mac")
    registry = bases.Registry()
    vm = TartVM(bases.vm_name(name))
    local = list_vms("local")
    record = registry.get(name)
    if record and record.get("provisioned") == PROVISION_VERSION and vm.name in local and not reprovision:
        out("Base guest %s is ready (Tart VM %s)" % (name, vm.name))
        return

    if vm.name not in local:
        if image not in list_vms("oci") and not confirm("Download %s (tens of GB) for Base guest %s?" % (image, name)):
            raise GuestError(
                "not downloading %s without confirmation" % image,
                "re-run with --yes to allow the download, or run it in a terminal to be asked",
            )
        out("cloning %s into %s" % (image, vm.name))
        try:
            # Not captured: tart shows the download's progress.
            code = subprocess.run([tart_binary(), "clone", image, vm.name], stdin=subprocess.DEVNULL, timeout=CLONE_TIMEOUT).returncode
        except FileNotFoundError:
            raise GuestError("Tart is not installed", INSTALL_FIX)
        except subprocess.TimeoutExpired:  # run() has killed tart
            raise GuestTimeout("`tart clone %s` did not finish within %ss and was killed" % (image, CLONE_TIMEOUT), "check your network and re-run")
        if code:
            raise GuestError("`tart clone %s %s` failed (exit %s)" % (image, vm.name, code), "check the image name and your network")
    registry.put(name, {"provider": "tart", "os": "macos", "arch": "arm64", "vm": vm.name, "image": image, "user": None, "provisioned": None})

    agent = TartExecChannel(vm)
    if not vm.is_running():
        out("starting %s" % vm.name)
        vm.start()
    _wait(vm, agent, ["/usr/bin/true"], BASE_BOOT_TIMEOUT, "the Tart guest agent to answer")

    out("provisioning %s" % vm.name)
    user = _checked(agent.exec(["/usr/bin/id", "-un"], CALL_TIMEOUT, {}), "id -un").stdout.strip()
    ui_src = "/tmp/vmlab-ui-%s.swift" % uuid.uuid4().hex[:8]
    with tempfile.TemporaryFile() as stdin:
        stdin.write(_read_guest_file("macos/vmlab-ui.swift"))
        stdin.seek(0)
        _checked(agent.exec(["/bin/sh", "-c", 'cat > "$1"', "sh", ui_src], CALL_TIMEOUT, {}, stdin=stdin), "copy the UI helper's source")
    with tempfile.TemporaryFile() as stdin:
        stdin.write(_read_guest_file("macos/provision.sh"))
        stdin.seek(0)
        result = agent.exec(["/bin/bash", "-s", "--", public_key(), ui_src], PROVISION_TIMEOUT, {}, stdin=stdin)
    for line in result.stdout.splitlines():
        out(line)
    if not result.ok:
        raise GuestError(
            "provisioning %s failed:\n%s" % (vm.name, _tail(result.stderr)),
            "fix the cause, then re-run `vmlab base create %s`: provisioning is idempotent" % name,
        )
    host_key = _checked(agent.exec(["/bin/cat", "/etc/ssh/ssh_host_ed25519_key.pub"], CALL_TIMEOUT, {}), "read the SSH host key").stdout
    pin_host_key(vm.name, host_key)

    # tccd reads its database at boot. Reboot from outside: a reboot from inside
    # cuts the Channel and "succeeds" before anything happens.
    booted = _checked(agent.exec(["/usr/sbin/sysctl", "-n", "kern.boottime"], CALL_TIMEOUT, {}), "sysctl").stdout
    out("rebooting %s so the TCC grants take effect" % vm.name)
    vm.stop(BASE_BOOT_TIMEOUT)
    vm.start()
    _wait(vm, agent, DESKTOP_PROBE, BASE_BOOT_TIMEOUT, "the desktop session after the reboot")
    if _checked(agent.exec(["/usr/sbin/sysctl", "-n", "kern.boottime"], CALL_TIMEOUT, {}), "sysctl").stdout == booted:
        raise GuestError("%s did not reboot (same kern.boottime)" % vm.name, "re-run `vmlab base create %s`" % name)

    ssh = SshChannel(user, vm.ip, vm.name, vm.name, PATH_APPEND)
    try:
        _wait(vm, ssh, ["/usr/bin/true"], BASE_BOOT_TIMEOUT, "SSH with vmlab's key", forget_ip=True)
        for channel in (ssh, agent):
            try:
                result = channel.exec(SYSTEM_EVENTS_PROBE, SYSTEM_EVENTS_TIMEOUT, {})
            except GuestTimeout:
                raise GuestError(
                    "System Events did not answer over Channel %s: a TCC consent dialog is probably waiting in the Guest" % channel.name,
                    "look at the Guest's screen (tart run %s) and re-run `vmlab base create %s --reprovision`" % (vm.name, name),
                )
            _checked(result, "System Events over Channel %s" % channel.name, "re-run `vmlab base create %s --reprovision`" % name)
            out("  Channel %s reaches System Events" % channel.name)
            result = channel.exec(["/bin/sh", "-c", uihelpers.MACOS_GUARD, "sh", "version"], CALL_TIMEOUT, {})
            if result.code != uihelpers.MISSING:
                info = json.loads(_checked(result, "vmlab-ui over Channel %s" % channel.name).stdout)
                if not info.get("trusted"):
                    raise GuestError(
                        "vmlab-ui has no Accessibility grant over Channel %s" % channel.name,
                        "re-run `vmlab base create %s --reprovision`" % name,
                    )
                out("  Channel %s: vmlab-ui has the Accessibility grant" % channel.name)
    finally:
        ssh.close()
    vm.stop(BASE_BOOT_TIMEOUT)
    registry.put(
        name,
        {"provider": "tart", "os": "macos", "arch": "arm64", "vm": vm.name, "image": image, "user": user, "provisioned": PROVISION_VERSION},
    )
    out("Base guest %s is ready (Tart VM %s). Labs use it with: [labs.<name>.tart] base = \"%s\"" % (name, vm.name, name))


def _wait(vm, channel, probe, timeout, what, forget_ip=False):
    deadline = time.time() + timeout
    while True:
        vm.check_alive()
        if forget_ip:
            vm.forget_ip()
        try:
            if channel.exec(probe, CALL_TIMEOUT, {}).ok:
                return
        except GuestError:
            pass
        if time.time() >= deadline:
            raise GuestError("timed out after %ss waiting for %s in %s" % (timeout, what, vm.name), "look at %s" % vm.log_path)
        time.sleep(1)


def _checked(result, what, fix="re-run the same `vmlab base create`: it resumes where it failed"):
    if not result.ok:
        raise GuestError("%s failed (exit %s): %s" % (what, result.code, _tail(result.stderr)), fix)
    return result


def _read_guest_file(name):
    return pkgutil.get_data("vmlab", "guest/" + name)
