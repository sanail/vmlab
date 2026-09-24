"""Running commands in Windows Guests, for any Provider.

Every Windows Channel runs a call the same way, so falling back from one to
another never changes what a command sees: guest/windows/call.ps1 starts it
in the logged-in user's desktop session, not elevated, in the user's profile
folder, and writes its exit code and raw output to a result file. A Channel
only delivers the call and fetches that file (ADR 0003):

- a Provider's native guest exec (Fusion: vmrun -interactive), and
- SSH, whose sessions cannot reach the desktop, so its calls hop through an
  interactive Scheduled Task (guest/windows/ssh-call.ps1).

Output is decoded as UTF-8, the code page provisioning gives Windows Guests.
"""

import base64
import hashlib
import json
import pkgutil
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

from vmlab.providers.base import ChannelError, ExecResult, GuestError
from vmlab.providers.ssh import SshChannel

# Where a call's script, stdin and result live. Not C:\Windows\Temp: a Scheduled Task's
# PowerShell cannot read a script there (that folder gives every user write, but no read)
# and the task then dies with 0xFFFD0000 and no output. Under ProgramData, where users may
# read and write, both Channels reach it; each Channel makes the folder.
CALL_DIR = r"C:\ProgramData\vmlab\calls"
CALL_DIRS = (r"C:\ProgramData\vmlab", CALL_DIR)  # made in order: vmrun creates no parents
NO_RESULT = 3  # ssh-call.ps1's exit code when the Scheduled Task wrote no result
MKDIR_TIMEOUT = 30  # s to make the call folder


def new_call():
    """A Guest path prefix unique to one call: its script, stdin and result files."""
    return r"%s\vmlab-call-%s" % (CALL_DIR, uuid.uuid4().hex)


def call_script(call, argv, env, timeout, stdin):
    """The PowerShell script (ASCII) that runs argv for the call prefix call; stdin: is there input?"""
    request = {
        "file": argv[0],
        "args": subprocess.list2cmdline(argv[1:]),
        "env": dict(env),
        "timeout": timeout,
        "stdin": call + ".in" if stdin else None,
        "result": call + ".result",
    }
    encoded = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
    return "$Request = '%s'\n%s" % (encoded, pkgutil.get_data("vmlab", "guest/windows/call.ps1").decode("ascii"))


def runner_argv(call):
    """What runs a call's script: in a console with no window, which would take the focus."""
    return [
        r"C:\Windows\System32\conhost.exe", "--headless",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", call + ".ps1",
    ]  # fmt: skip


def parse_result(argv, text, channel):
    try:
        result = json.loads(text)
        decode = lambda b64: base64.b64decode(result[b64]).decode("utf-8", "replace").replace("\r\n", "\n")  # noqa: E731
        return ExecResult(list(argv), int(result["code"]), decode("stdout"), decode("stderr"))
    except (ValueError, KeyError, TypeError):
        raise ChannelError(
            "the Guest's call runner returned no result over Channel %s: %r" % (channel, text[:200]),
            "re-provision the Base guest: vmlab base create NAME --reprovision",
        )


def _hashed(data):
    return hashlib.sha1(data).hexdigest()[:12]


class WindowsSshChannel(SshChannel):
    """SSH to a Windows Guest; each call runs as an interactive Scheduled Task (ssh-call.ps1).

    Everything but the short command line travels as files over scp: Windows' sshd stops
    reading a command's stdin after about 4 KB and the call then hangs, so nothing rides on
    it. That also gives Build artifacts a way in (send_file)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._helper = None  # the Guest path of ssh-call.ps1, once this Channel has sent it
        # No multiplexing: Windows' sshd refuses a second session on one connection
        # ("Session open refused by peer"), so every call would pay for a failed try first.
        self._control = None

    def _ready(self):
        """The Guest path of ssh-call.ps1, sent once per Channel; its name carries its version."""
        if self._helper is None:
            script = pkgutil.get_data("vmlab", "guest/windows/ssh-call.ps1")
            helper = r"%s\vmlab-ssh-call-%s.ps1" % (CALL_DIR, _hashed(script))
            result = self.run_command(
                "powershell -NoProfile -NonInteractive -Command \"New-Item -ItemType Directory -Force -Path '%s' | Out-Null\"" % CALL_DIR,
                ["mkdir", CALL_DIR], MKDIR_TIMEOUT, None,
            )  # fmt: skip
            if not result.ok:
                raise ChannelError("the Guest's call folder %s could not be made: %s" % (CALL_DIR, result.stderr.strip()), "check the Guest's disk")
            with tempfile.NamedTemporaryFile(suffix=".ps1") as local:
                local.write(script)
                local.flush()
                self.send_file(local.name, helper)
            self._helper = helper
        return self._helper

    def exec(self, argv, timeout, env, stdin=None):
        helper = self._ready()
        call = new_call()
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "call.ps1"
            script.write_text(call_script(call, argv, env, timeout, stdin is not None), encoding="ascii")
            self.send_file(script, call + ".ps1")
            if stdin is not None:
                data = Path(tmp) / "call.in"
                with data.open("wb") as f:
                    shutil.copyfileobj(stdin, f)
                self.send_file(data, call + ".in")
            command = 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%s" -Id %s -Dir "%s" -Timeout %d' % (
                helper, call.rsplit("-", 1)[1], CALL_DIR, int(timeout),
            )  # fmt: skip
            result = self.run_command(command, argv, timeout, None)
        if result.code == NO_RESULT:
            raise ChannelError(
                "ssh reached the Guest, but its Scheduled Task did not run the call: %s" % (result.stderr.strip().splitlines() or ["no output"])[-1],
                "check that the Guest user is logged in to the desktop (autologin); `vmlab doctor` tests every Channel",
            )
        if result.code:
            raise ChannelError(
                "the ssh Channel's PowerShell failed (exit %s): %s" % (result.code, (result.stderr.strip().splitlines() or ["no output"])[-1]),
                "re-provision the Base guest: vmlab base create NAME --reprovision",
            )
        return parse_result(argv, result.stdout, self.name)


def copy_in(provider, src, guest_dir, timeout=None):
    """Provider.copy_in for Windows Guests: the Host file or folder src as a tar archive,
    carried in as a file (a Channel's own transfer, not exec's stdin, which Windows' sshd
    cannot stream), then unpacked by tar.exe, which Windows has."""
    archive = r"%s\vmlab-copy-%s.tar" % (CALL_DIR, uuid.uuid4().hex)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "copy.tar"
        with tarfile.open(str(local), mode="w") as tar_file:
            tar_file.add(str(src), arcname=src.name)
        provider.send_file(local, archive)
    script = (
        "$d = [Environment]::ExpandEnvironmentVariables('%s') -replace '^~', $env:USERPROFILE; "
        "New-Item -ItemType Directory -Force -Path $d | Out-Null; tar.exe -xf '%s' -C $d; $code = $LASTEXITCODE; "
        "Remove-Item -Force -ErrorAction SilentlyContinue '%s'; if ($code) { exit $code }; (Get-Item -LiteralPath $d).FullName"
        % (guest_dir.replace("'", "''"), archive, archive)
    )
    timeout = provider.lab.app.install_timeout if timeout is None else timeout
    result = provider.exec(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)
    if not result.ok:
        detail = "\n".join(result.stderr.strip().splitlines()[-15:]) or "(no output)"
        raise GuestError("copying %s into the Guest failed: %s" % (src, detail), "check free disk space in the Guest")
    return result.stdout.strip() + "\\" + src.name
