"""Windows Base guests on VMware Fusion: `vmlab base create windows-11`.

Windows comes from Fusion's own "Get Windows from Microsoft" flow, which only a
person can click through, so this is a wizard: it tells the developer what to
do, checks each step before the next, and asks again until it holds. Then vmlab
adopts a copy of that VM (an APFS clone of its files: the original is never
changed) and provisions the copy over vmrun, headless but for one click.

Windows 11 needs a TPM, and Fusion encrypts a VM that has one. Its "only the
files needed to support a TPM" option still leaves vmrun unable to open the VM
without the encryption password (Fusion 26: "A password is required for this
operation", even for listSnapshots), so vmlab keeps that password with the
Guest user's, and vmrun cannot clone such a VM: Labs get APFS copies too.

vmrun runs programs as the Guest user unelevated (Medium integrity), and only
a person can answer Windows' UAC prompt (keys sent through vmcli do not reach
it), so the wizard asks for one click on it; the elevated call then lets
administrators elevate without a prompt, and provisioning runs elevated
from there: guest/windows/provision.ps1.
"""

import io
import json
import os
import pkgutil
import re
import subprocess
import time
import uuid
from pathlib import Path

from vmlab import bases, hostproc, uihelpers
from vmlab.config import host_arch
from vmlab.providers.base import GuestError
from vmlab.providers.fusion import (
    BASE_BOOT_TIMEOUT, CALL_TIMEOUT, PROVISION_TIMEOUT, WINDOWS_DEFAULTS, FusionVM, WindowsVmrunChannel, credentials, delete_old_snapshots, fusion_dir, provisioned_snapshot, running_vmx,
    save_credentials, sound_off, vm_password, vmrun, vmx_path,
)  # fmt: skip
from vmlab.providers.ssh import pin_host_key, public_key
from vmlab.providers.windows import WindowsSshChannel

PROVISION_VERSION = 3  # bump when provision.ps1 changes; `base create` then re-provisions
PROBE = ["cmd", "/c", "exit 0"]
ELEVATION_TIMEOUT = 180  # s: Windows cancels an unanswered UAC prompt after about two minutes
POLICIES = r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
SILENT_ELEVATION = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "$s = Get-ItemProperty '%s'; if ($s.ConsentPromptBehaviorAdmin -eq 0 -and $s.PromptOnSecureDesktop -eq 0) { 'yes' } else { 'no' }" % POLICIES]  # fmt: skip
ELEVATE = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', "
           "\"Set-ItemProperty '%s' ConsentPromptBehaviorAdmin 0; Set-ItemProperty '%s' PromptOnSecureDesktop 0\"" % (POLICIES, POLICIES)]  # fmt: skip
# Runs provision.ps1 elevated and prints its log. stdin: {"script", "params"}; both go to the
# user's own temp folder, which other users cannot read.
PROVISION = ["powershell", "-NoProfile", "-NonInteractive", "-Command", r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$all = New-Object IO.MemoryStream; [Console]::OpenStandardInput().CopyTo($all)
$in = [Text.Encoding]::UTF8.GetString($all.ToArray()) | ConvertFrom-Json
$dir = Join-Path $env:LOCALAPPDATA 'Temp'
$script = Join-Path $dir 'vmlab-provision.ps1'; $params = Join-Path $dir 'vmlab-provision.json'; $log = Join-Path $dir 'vmlab-provision.log'
[IO.File]::WriteAllText($script, $in.script); [IO.File]::WriteAllText($params, ($in.params | ConvertTo-Json -Compress))
$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$script`"", '-Params', "`"$params`"", '-Log', "`"$log`""
Get-Content -LiteralPath $log
Remove-Item -Force -ErrorAction SilentlyContinue $script, $params
exit $p.ExitCode
"""]  # fmt: skip
LANGUAGE_PS = "(Get-UICulture).Name"  # the signed-in user's display language, e.g. en-US
NO_TOOLS_GRACE = 180  # s after which a Guest whose VMware Tools never answer surely lacks them
WINDOW_START_WAIT = 60  # s for Fusion to power on a VM it opened
SESSION = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "(Get-Process -Id $PID).SessionId; [Console]::OutputEncoding.CodePage; " + LANGUAGE_PS]
DISPLAY_LANGUAGE = ["powershell", "-NoProfile", "-NonInteractive", "-Command", LANGUAGE_PS]
FUSION_FOLDERS = ("Virtual Machines.localized", "Virtual Machines", "Documents/Virtual Machines.localized")

GET_WINDOWS = """\
  1. In VMware Fusion: File > New..., choose "Get Windows from Microsoft", pick Windows 11
     and English (United States), and follow Fusion's steps.
     Windows Labs expect en-US element names; a Lab written for another language sets
     language under [labs.NAME.fusion].
  2. When Fusion asks how to encrypt the VM (Windows 11 needs a TPM), choose "Only the files
     needed to support a TPM are encrypted" and let Fusion keep the password in your Keychain.
  3. In Windows Setup, make a local account with a password: vmlab signs in with it.
     If Setup insists on a Microsoft account, press Shift+F10 and run: start ms-cxh:localonly
  4. Set Windows' time zone to your Mac's (Settings > Time & language > Date & time; it is
     Pacific Time after an English (United States) install). Fusion gives Windows the Mac's
     local time, which Windows reads in its own time zone: another one sets its clock hours off.
  5. Once Windows shows its desktop, install VMware Tools (vmlab runs everything through them;
     Fusion's flow leaves them out): in Fusion, Virtual Machine > Install VMware Tools; in
     Windows, open the DVD drive in File Explorer, run setup (Typical), and restart when it asks.
  6. Come back here. vmlab copies the VM; the original stays yours."""
KEYCHAIN_NOTE = ("  If macOS asks whether %s may use a password in your Keychain, click Always Allow:\n"
                 "  Allow lets it once, and vmlab needs it again on later runs.")
START_IN_FUSION = """\
  Fusion would not start it with a window from vmlab (it could not take the VM's password
  from your Keychain), so vmlab opened it in Fusion: %s
""" + KEYCHAIN_NOTE % "VMware Fusion" + """
  If Fusion asks for the VM's encryption password, it is your original VM's. If the VM
  shows as stopped, click its Start button."""


def create_base(name, image, prompt, reprovision, out):
    """Adopt a copy of a Fusion-made Windows VM as Base guest name and provision it; idempotent."""
    running_vmx()  # Fusion is installed and answers
    vm = FusionVM(vmx_path(bases.vm_name(name)), secrets=bases.vm_name(name))
    record = bases.Registry().get(name) or {}
    # --image naming another VM than the one adopted: adopt it in its place (e.g. Windows in another language)
    other_image = bool(image and record.get("image") and _image_vmx(image) != Path(record["image"]).resolve())
    sound_off(vm, out)
    if record.get("provisioned") == PROVISION_VERSION and vm.exists() and record.get("snapshot") in vm.snapshots() and not (reprovision or other_image):
        out("Base guest %s is ready (Fusion VM %s)" % (name, vm.vmx))
    else:
        wizard = Wizard(name, prompt, out)
        if other_image or not (record.get("installed") and vm.exists()):
            _adopt(wizard, name, vm, image)
        _provision(wizard, name, vm)
    delete_old_snapshots(name, vm, prompt.confirm, out)  # says how to delete them in Fusion: vmlab cannot


class Wizard:
    """Steps a person performs, each checked before the next."""

    def __init__(self, name, prompt, out):
        self.name, self.prompt, self.out = name, prompt, out

    def step(self, title, instructions, check, act=None, ahead=False):
        """check() returns None once the step holds, else what is still wrong. act(), if given,
        runs before every check (e.g. raising a prompt the person answers in the Guest); ahead
        shows the instructions before the first check, for a check that waits minutes."""
        shown = False
        if ahead:
            self._show(title, instructions)
            shown = True
        while True:
            if act and not shown:
                self._show(title, instructions)
                shown = True
            if act:
                act()
            problem = check()
            if problem is None:
                self.out("  ok: %s" % title)
                return
            if not shown:
                self._show(title, instructions)
                shown = True
            self.out("  not yet: %s" % problem)
            if not self.prompt.pause("Press Enter to check again (Ctrl-C stops; re-running continues from here)"):
                raise GuestError("%s: %s" % (title, problem), "do the steps above, then run `vmlab base create %s` in a terminal window of your own: "
                                 "it waits for you and asks for passwords, which Claude Code's `!` cannot answer" % self.name)  # fmt: skip

    def _show(self, title, instructions):
        self.out("\n%s\n%s" % (title, instructions))


def _adopt(wizard, name, vm, image):
    """Find the developer's Windows VM, its passwords and the Guest user; copy it into vmlab's home."""
    found = {}

    def check_vm():
        vmx, problem = _find_vm(image, wizard.prompt)
        found["vmx"] = vmx
        return problem

    wizard.step("Make a Windows 11 VM with Fusion", GET_WINDOWS, check_vm)
    source = found["vmx"]
    wizard.out("  using %s" % source)

    def check_password():
        code, output = vmrun(["listSnapshots", source], CALL_TIMEOUT)
        if code == 0:
            found["vm_password"] = None  # not encrypted
            return None
        if "password" not in output.lower():
            return "vmrun cannot open the VM: %s" % output.strip()
        if "keychain_note" not in found:
            wizard.out(KEYCHAIN_NOTE % "security")
            found["keychain_note"] = True
        password = _keychain_password(source) or wizard.prompt.secret("The VM's encryption password: ")
        if not password:
            return "vmlab needs the VM's encryption password, and it is not in your Keychain"
        code, output = vmrun(["listSnapshots", source], CALL_TIMEOUT, ["-vp", password])
        if code:
            return "that password does not open the VM: %s" % output.strip()
        found["vm_password"] = password
        return None

    wizard.step(
        "The VM's encryption password",
        "  vmrun needs it to open the VM. vmlab looks for it in your Keychain, where Fusion keeps it,\n"
        "  and asks you for it otherwise; it keeps a copy in %s (mode 0600)." % vmx_path(bases.vm_name(name)).parent.parent,
        check_password,
    )
    source_vm = FusionVM(source, password=found["vm_password"])

    def check_account():
        user = wizard.prompt.text("The Windows account's user name: ")
        password = user and wizard.prompt.secret("Its password: ")
        if not (user and password):
            return "vmlab needs the Windows account's user name and password"
        found["user"], found["password"] = user.strip(), password
        return None

    wizard.step(
        "The Windows account",
        "  The local administrator account you made in Windows Setup. vmlab signs in with it\n"
        "  (autologin), runs everything as it, and keeps its password with the VM's.",
        check_account,
    )

    def check_stopped():
        if not source_vm.is_running():
            return None
        if wizard.prompt.confirm("%s is running. Should vmlab shut Windows down for you now?" % source_vm.name):
            wizard.out("  shutting Windows down (up to a minute; then vmlab powers it off)")
            source_vm.stop()
        return "the VM is still running" if source_vm.is_running() else None

    wizard.step("Shut the VM down", "  vmlab copies the VM only while it is stopped: shut Windows down (Start > Power > Shut down),\n"
                "  or answer y above and vmlab does it.", check_stopped)

    if vm.exists() or vm.vmx.parent.exists():  # an adoption that did not finish: start over
        vm.delete()
    save_credentials(vm.name, found["user"], found["password"], found["vm_password"])
    wizard.out("copying %s into %s (an APFS clone: instant, and it shares the original's disk space)" % (source, vm.vmx.parent))
    source_vm.clone_copy(vm)
    sound_off(vm, wizard.out)  # before Windows first runs as a Base guest; Lab copies inherit it
    bases.Registry().put(name, {
        "provider": "fusion", "os": "windows", "arch": host_arch(), "vm": vm.name, "vmx": str(vm.vmx), "image": str(source),
        "user": found["user"], "installed": True, "provisioned": None,
    })  # fmt: skip
    wizard.out("  copied. The original VM is untouched: delete it in Fusion once you no longer need it.")


def _image_vmx(image):
    """The .vmx that --image names (a .vmx file or a .vmwarevm folder), resolved."""
    path = Path(image).expanduser()
    candidates = sorted(path.glob("*.vmx")) if path.is_dir() else [path]
    return candidates[0].resolve() if candidates else path.resolve()


def _find_vm(image, prompt):
    """(the .vmx of the developer's Windows VM, None) or (None, why there is none)."""
    if image:
        path = Path(image).expanduser()
        candidates = sorted(path.glob("*.vmx")) if path.is_dir() else [path]
        if not (candidates and candidates[0].is_file()):
            return None, "no VM at %s (--image takes a .vmx file or .vmwarevm folder)" % path
        vmx = candidates[0]
        guest = _guest_os(vmx)
        if "windows" not in guest:
            return None, "%s is not a Windows VM (guestOS = %r)" % (vmx, guest)
    else:
        found = [vmx for folder in FUSION_FOLDERS for vmx in sorted((Path.home() / folder).glob("*.vmwarevm/*.vmx")) if "windows" in _guest_os(vmx)]
        if not found:
            return None, "no Windows VM in Fusion's folders (%s); if yours is elsewhere, pass --image PATH" % ", ".join("~/" + f for f in FUSION_FOLDERS)
        if len(found) > 1:
            listing = "\n".join("  %d. %s" % (i + 1, vmx) for i, vmx in enumerate(found))
            answer = prompt.text("Several Windows VMs:\n%s\nWhich one? " % listing)
            if not (answer and answer.strip().isdigit() and 1 <= int(answer) <= len(found)):
                return None, "several Windows VMs; pick one, or pass --image PATH:\n%s" % listing
            found = [found[int(answer) - 1]]
        vmx = found[0]
        guest = _guest_os(vmx)
    if host_arch() == "arm64" and not guest.startswith("arm-"):
        return None, "%s is an x64 VM; this Mac runs arm64 Windows only (guestOS = %r)" % (vmx, guest)
    vmx = vmx.resolve()
    if fusion_dir().resolve() in vmx.parents:  # a Base guest or a Lab's clone: copying it onto itself would delete it
        return None, "%s is vmlab's own VM; adopt the VM you made in Fusion, not a copy vmlab keeps" % vmx
    return vmx, None


def _guest_os(vmx):
    try:
        match = re.search(r'^guestOS\s*=\s*"([^"]*)"', vmx.read_text(encoding="utf-8", errors="replace"), re.M)
    except OSError:
        return ""
    return match.group(1).lower() if match else ""


def _keychain_password(vmx):
    """The VM's encryption password as Fusion keeps it in the login Keychain, or None."""
    security = os.environ.get("VMLAB_SECURITY") or "/usr/bin/security"
    try:
        code, out, _ = hostproc.run([security, "find-generic-password", "-s", str(vmx), "-w"], 60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.rstrip("\n") if code == 0 and out.strip() else None


def _keychain_save(vm):
    """Keep vm's encryption password in the login Keychain as Fusion does (service: the .vmx path).

    Fusion shows an encrypted VM in a window only if it finds the password there: without it,
    `vmrun start gui` fails with "The operation is not supported", and a window opened on a
    VM started without one stays black."""
    security = os.environ.get("VMLAB_SECURITY") or "/usr/bin/security"
    password = vm_password(vm.secrets)
    if not password or _keychain_password(vm.vmx) == password:
        return
    # -w on the command line, as Fusion's own helper passes it; the Host is the developer's.
    code, out, err = hostproc.run([security, "add-generic-password", "-U", "-s", str(vm.vmx), "-a", os.environ.get("USER", "vmlab"),
                                   "-D", "Virtual Machine password", "-w", password], 60)  # fmt: skip
    if code:
        raise GuestError("cannot keep %s's password in the Keychain: %s" % (vm.name, (err or out).strip()), "unlock the login Keychain, then re-run")


def _provision(wizard, name, vm):
    """Sign in, get elevation once, provision, reboot and check both Channels, snapshot."""
    registry = bases.Registry()
    record = registry.get(name)
    channel = WindowsVmrunChannel(vm, vm.name)
    if not vm.is_running():
        # Headless, unless the person has a step to do in it: the UAC click below.
        needs_click = not record.get("elevated")
        wizard.out("starting %s%s" % (vm.name, " in a Fusion window: you will click once in it" if needs_click else ""))
        if needs_click:
            _keychain_save(vm)
            _start_with_window(wizard, vm)
        else:
            vm.start()

    def signed_in():
        deadline = time.time() + BASE_BOOT_TIMEOUT
        no_tools_since = None
        while True:
            try:
                channel.exec(PROBE, CALL_TIMEOUT, {})
                return None
            except GuestError as exc:  # ChannelError, and the timeouts of a Guest still booting
                if "user name or password" in exc.message.lower():
                    user = wizard.prompt.text("The Windows account's user name: ")
                    password = user and wizard.prompt.secret("Its password: ")
                    if not (user and password):
                        return "Windows refused the account %s: %s" % (credentials(vm.name)["user"], exc.message)
                    save_credentials(vm.name, user.strip(), password, vm_password(vm.name))
                    record["user"] = user.strip()
                    registry.put(name, record)
                    continue
                if "tools are not running" in exc.message.lower():
                    no_tools_since = no_tools_since or time.time()
                    if time.time() - no_tools_since > NO_TOOLS_GRACE:  # Windows has long booted by now
                        return ("VMware Tools do not run in the Guest. In its Fusion window: Virtual Machine > Install VMware Tools;"
                                " in Windows, open the DVD drive in File Explorer, run setup (Typical), and restart when it asks")
                else:
                    no_tools_since = None
                if time.time() >= deadline:
                    return exc.message
            time.sleep(2)

    wizard.step(
        "Sign in to Windows",
        "  vmrun runs programs only for a user signed in to the desktop. If the VM's window shows\n"
        "  the sign-in screen, sign in as %s; provisioning makes that automatic. This waits for the\n"
        "  Guest to boot, which takes a minute or two." % credentials(vm.name)["user"],
        signed_in,
        ahead=True,
    )

    def silent():
        try:
            result = channel.exec(SILENT_ELEVATION, CALL_TIMEOUT, {})
        except GuestError as exc:
            return exc.message
        return None if result.stdout.strip() == "yes" else "Windows still asks before elevating (%s)" % (result.stderr.strip() or "the prompt was not answered")

    def ask_windows():
        if silent() is not None:
            try:
                channel.exec(ELEVATE, ELEVATION_TIMEOUT, {})  # returns once the prompt is answered or cancelled
            except GuestError:
                pass  # nobody answered, or the Guest is not ready: silent() says so

    wizard.step(
        "Allow vmlab to administer the Guest (one click)",
        "  In the VM's Fusion window, Windows asks \"Do you want to allow this app to make changes to\n"
        "  your device?\" for Windows PowerShell: click Yes. Windows then lets vmlab elevate without\n"
        "  asking again, in this throwaway Guest only. If no window shows the Guest, open\n"
        "  %s in Fusion." % vm.vmx.parent,
        silent,
        act=ask_windows,
    )
    record["elevated"] = True
    registry.put(name, record)

    wizard.out("provisioning %s (OpenSSH, autologin, no updates, sleep or OneDrive, UTF-8, native PowerShell; a few minutes)" % vm.name)
    creds = credentials(vm.name)
    stdin = json.dumps({
        "script": pkgutil.get_data("vmlab", "guest/windows/provision.ps1").decode("ascii"),
        "params": {"user": creds["user"], "password": creds["password"], "key": public_key()},
    }).encode("utf-8")  # fmt: skip
    result = channel.exec(PROVISION, PROVISION_TIMEOUT, {}, stdin=io.BytesIO(stdin))
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        if not line.startswith("vmlab-host-key: "):
            wizard.out("  " + line.lstrip("﻿"))
    host_key = [line.split(": ", 1)[1] for line in lines if line.startswith("vmlab-host-key: ")]
    if not result.ok or not host_key:
        raise GuestError(
            "provisioning %s failed (exit %s):\n%s" % (vm.name, result.code, "\n".join((result.stderr.strip().splitlines() + lines)[-15:])),
            "fix the cause, then re-run `vmlab base create %s`: provisioning is idempotent" % name,
        )
    pin_host_key(vm.name, host_key[0])

    wizard.out("rebooting %s: autologin, UTF-8" % vm.name)
    vm.stop()
    vm.start()
    ssh = WindowsSshChannel(creds["user"], vm.ip, vm.name, vm.name)
    try:
        for each in (channel, ssh):
            session, codepage, language = _desktop_session(vm, each)
            if session == 0 or codepage != 65001:
                raise GuestError(
                    "Channel %s runs its calls in session %s with code page %s, not in the desktop session with UTF-8" % (each.name, session, codepage),
                    "re-run `vmlab base create %s --reprovision`" % name,
                )
            wizard.out("  Channel %s reaches the desktop session (session %s, UTF-8)" % (each.name, session))
        expected = WINDOWS_DEFAULTS["language"]
        if language.lower() != expected.lower():
            wizard.out("  display language %s: Windows Labs expect %s unless they set language under [labs.NAME.fusion]" % (language or "unknown", expected))
        # The UI helper compiles itself on first use (~10 s): done here, it is in the snapshot,
        # and Labs do not pay for it after every restore.
        info = uihelpers.windows_helper_info(ssh, uihelpers.WINDOWS_COMPILE_TIMEOUT)
        wizard.out("  UI helper ready (screen %sx%s)" % (info["screen"]["w"], info["screen"]["h"]))
    finally:
        ssh.close()
    vm.stop()
    provisioned_id = uuid.uuid4().hex
    snapshot = provisioned_snapshot(provisioned_id)
    vm.snapshot(snapshot)  # earlier ones stay: vmlab says how to delete them in Fusion (delete_old_snapshots)
    record.update(provisioned=PROVISION_VERSION, provisioned_id=provisioned_id, snapshot=snapshot, language=language)
    registry.put(name, record)
    wizard.out('Base guest %s is ready (Fusion VM %s). Windows Labs use it with: [labs.<name>.fusion] base = "%s"' % (name, vm.vmx, name))


def _start_with_window(wizard, vm):
    """Start vm in a Fusion window, for the click a person gives in it; open it in Fusion when
    vmrun may not (an encrypted VM whose password Fusion cannot read from the Keychain)."""
    try:
        vm.start(gui=True)
        return
    except GuestError as exc:
        if "not supported" not in exc.message.lower():
            raise
    opener = os.environ.get("VMLAB_OPEN") or "/usr/bin/open"
    try:
        hostproc.run([opener, "-a", "VMware Fusion", str(vm.vmx)], 60)
    except (OSError, subprocess.TimeoutExpired):
        pass  # the step below says how to open it by hand

    def running():
        deadline = time.time() + (WINDOW_START_WAIT if wizard.prompt.interactive else 0)  # nobody to wait for otherwise
        while not vm.is_running():
            if time.time() >= deadline:
                return "%s is not running yet" % vm.name
            time.sleep(2)
        return None

    wizard.step("Start the Guest in a Fusion window", START_IN_FUSION % vm.vmx, running, ahead=True)


def _desktop_session(vm, channel):
    """(session id, code page, display language) of calls over channel, once it answers after a boot."""
    deadline = time.time() + BASE_BOOT_TIMEOUT
    while True:
        vm.forget_ip()
        try:
            result = channel.exec(SESSION, CALL_TIMEOUT, {})
            if result.ok:
                session, codepage, language = (result.stdout.split() + [""])[:3]  # no language: the invariant culture
                return int(session), int(codepage), language
            problem = result.stderr.strip()
        except GuestError as exc:
            problem = exc.message
        if time.time() >= deadline:
            raise GuestError("Channel %s did not reach %s within %ss after a reboot: %s" % (channel.name, vm.name, BASE_BOOT_TIMEOUT, problem),
                             "look at its screen (open -a 'VMware Fusion' '%s'); re-run `vmlab base create`" % vm.vmx)  # fmt: skip
        time.sleep(2)
