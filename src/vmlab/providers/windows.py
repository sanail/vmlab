"""Running commands in Windows Guests, for any Provider.

Every Windows Channel runs a call the same way, so falling back from one to
another never changes what a command sees: it starts in the logged-in user's
desktop session, not elevated, in the user's profile folder, and its exit
code and raw output come back as one JSON result (ADR 0003):

- a Provider's native guest exec (Fusion: vmrun -interactive) runs a script,
  guest/windows/call.ps1, that writes the result to a file the Channel fetches;
- SSH, whose sessions cannot reach the desktop, reaches vmlab's call server there
  (guest/windows/call-server.ps1), which an interactive Scheduled Task starts once
  per boot, through a forwarded connection.

Output is decoded as UTF-8, the code page provisioning gives Windows Guests.
"""

import base64
import hashlib
import io
import json
import pkgutil
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

from vmlab import hostpower, hostproc
from vmlab.providers.base import ChannelError, ExecResult, GuestError, GuestTimeout, ps_path, ps_quote
from vmlab.providers.ssh import SshChannel

# Where a call's script, stdin and result live. Not C:\Windows\Temp: a Scheduled Task's
# PowerShell cannot read a script there (that folder gives every user write, but no read)
# and the task then dies with 0xFFFD0000 and no output. Under ProgramData, where users may
# read and write, both Channels reach it; each Channel makes the folder.
CALL_DIR = r"C:\ProgramData\vmlab\calls"
CALL_DIRS = (r"C:\ProgramData\vmlab", CALL_DIR)  # made in order: vmrun creates no parents
SERVER_PORTS = (40000, 49000)  # the server's port is in this range, by its version
SERVER_START_TIMEOUT = 75  # s to start the server; in the Guest, call-server.ps1 -Start gives up after 60
SERVER_FIX = "`vmlab doctor` tests every Channel; restarting the Guest restarts the server (vmlab down, then vmlab up)"
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


# Reads the parameters line from stdin, then runs the rest as a script block. No double
# quotes: the Channel's command line goes through Windows' quoting rules.
HELPER_BOOTSTRAP = (
    "$s = [IO.StreamReader]::new([Console]::OpenStandardInput()).ReadToEnd(); $i = $s.IndexOf([char]10); "
    "& ([ScriptBlock]::Create($s.Substring($i + 1))) '%s' $s.Substring(0, $i).TrimEnd([char]13)"
)


def helper_call(script, command, params):
    """(argv, stdin) that run one command of the Guest script guest/windows/SCRIPT over any Channel:
    a PowerShell script taking (COMMAND, PARAMS JSON), sent on stdin behind a line of parameters,
    since a Windows command line is too short and too hard to quote for either."""
    stdin = io.BytesIO(json.dumps(params).encode("utf-8") + b"\n" + pkgutil.get_data("vmlab", "guest/windows/" + script))
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", HELPER_BOOTSTRAP % command], stdin


def _hashed(data):
    return hashlib.sha1(data).hexdigest()[:12]


def server_version():
    """call-server.ps1's version: a hash of it, which names its file, its compiled copy and its port."""
    return _hashed(pkgutil.get_data("vmlab", "guest/windows/call-server.ps1"))


def server_port(version):
    """The Guest port (127.0.0.1) the server of this version listens on; another version has its own."""
    return SERVER_PORTS[0] + int(version, 16) % (SERVER_PORTS[1] - SERVER_PORTS[0])


def call_request(argv, env, timeout, stdin_length):
    """The lines that ask the server for one call (guest/windows/call-server.ps1); stdin_length bytes follow them."""
    b64 = lambda text: base64.b64encode(text.encode("utf-8")).decode("ascii")  # noqa: E731
    lines = ["file " + b64(argv[0]), "args " + b64(subprocess.list2cmdline(argv[1:])), "timeout %d" % int(timeout)]
    lines += ["env %s %s" % (b64(name), b64(str(value))) for name, value in sorted(env.items())]
    lines += ["stdin %d" % stdin_length, "", ""]
    return "\n".join(lines).encode("ascii")


class WindowsSshChannel(SshChannel):
    """SSH to a Windows Guest, whose calls vmlab's call server runs in the desktop session (call-server.ps1).

    A call is `ssh -W` to the server's port over the master connection (~40 ms), not a session:
    Windows' sshd stops reading a command's stdin after about 4 KB, while a forwarded
    connection carries any size. The server starts at the first call after a boot, through an
    interactive Scheduled Task, over a plain SSH session; files (send_file) go over scp."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Sessions connect on their own: Windows' sshd may refuse a second session on one
        # connection ("Session open refused by peer"). Forwarded calls share the master.
        self._sessions = None
        self._version = server_version()
        self._port = server_port(self._version)
        # Known to be gone: the Guest stopped since (close). A try would first wait ~2 s for
        # Windows to refuse the connection.
        self._server_gone = False
        self._call_dir = False  # made, and its access granted, since the Guest last stopped

    def close(self):
        super().close()
        self._server_gone = True
        self._call_dir = False

    def _make_call_dir(self):
        """Make the call folder and grant the Guest user Modify on it and all it holds, inherited.
        What scp writes belongs to the elevated ssh session, and a file there gets only read for
        Users from ProgramData: the desktop session's calls, not elevated, could then delete neither
        it (copy_in's archive) nor the folder. Granting it again also reaches the files already there."""
        grant = "('*' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value + ':(OI)(CI)M')"
        result = self.run_command(
            "powershell -NoProfile -NonInteractive -Command \"New-Item -ItemType Directory -Force -Path '%s' | Out-Null; "
            "icacls.exe '%s' /grant %s /Q | Out-Null; exit $LASTEXITCODE\"" % (CALL_DIR, CALL_DIR, grant),
            ["mkdir", CALL_DIR], MKDIR_TIMEOUT, None,
        )  # fmt: skip
        if not result.ok:
            raise ChannelError(
                "the Guest's call folder %s could not be made, or the Guest user given Modify on it: %s" % (CALL_DIR, (result.stderr or result.stdout).strip()),
                "check the Guest's disk, and the folder's permissions (icacls %s)" % CALL_DIR,
            )
        self._call_dir = True

    def send_file(self, local, guest_path, timeout):
        # Files go to the call folder (copy_in's archive, the call server's script).
        if not self._call_dir:
            self._make_call_dir()
        super().send_file(local, guest_path, timeout)

    def _start_server(self):
        """Send call-server.ps1 and have it start its Scheduled Task; ChannelError if it does not listen."""
        script = pkgutil.get_data("vmlab", "guest/windows/call-server.ps1")
        path = r"%s\vmlab-call-server-%s.ps1" % (CALL_DIR, self._version)
        with tempfile.NamedTemporaryFile(suffix=".ps1") as local:
            local.write(script)
            local.flush()
            try:
                self.send_file(local.name, path, SERVER_START_TIMEOUT)
            except GuestTimeout as exc:  # the call has not run yet, so another Channel may take it
                raise ChannelError(exc.message, "check the Guest's disk space and sshd")
        command = 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%s" -Port %d -Start' % (path, self._port)
        result = self.run_command(command, ["vmlab-call-server", "-Start"], SERVER_START_TIMEOUT, None)
        if result.code:
            raise ChannelError(
                "ssh reached the Guest, but vmlab's call server did not start in its desktop session: %s" % (result.stderr.strip().splitlines() or ["exit %s" % result.code])[-1],
                "check that the Guest user is logged in to the desktop (autologin); `vmlab doctor` tests every Channel",
            )

    def _forward(self, request, timeout):
        """(code, stdout, stderr) of one connection to the server, fed the file request."""
        target = self._target()
        self._ensure_master(target)
        command = ["ssh"] + self._master_options() + ["-o", "ControlMaster=no", "-W", "127.0.0.1:%d" % self._port, target]
        request.seek(0)
        return hostproc.run(command, timeout, stdin=request)

    def exec(self, argv, timeout, env, stdin=None):
        with tempfile.TemporaryFile() as data, tempfile.TemporaryFile() as request:
            if stdin is not None:
                shutil.copyfileobj(stdin, data)
            request.write(call_request(argv, env, timeout, data.tell()))
            data.seek(0)
            shutil.copyfileobj(data, request)
            if self._server_gone:
                self._start_server()
                self._server_gone = False
            answer = self._call(argv, request, timeout)
            if answer is None:
                # Nothing answered on the server's port, so the call never ran: start the server, then try again.
                self._start_server()
                answer = self._call(argv, request, timeout)
        if answer is None:
            raise ChannelError("vmlab's call server in the Guest does not answer on port %d although it started" % self._port, SERVER_FIX)
        try:
            return parse_result(argv, answer, self.name)
        except ChannelError:
            # Not a ChannelError: the server took the call, so the command may have run, and another
            # Channel must not run it again (ADR 0003).
            raise GuestError("vmlab's call server in the Guest took %s but gave no result: %r" % (list(argv), answer[:300]), SERVER_FIX)

    def _call(self, argv, request, timeout):
        """The server's answer to one call, or None if no server took it."""
        try:
            code, out, err = self._forward(request, timeout)
        except subprocess.TimeoutExpired:
            raise GuestTimeout("%s timed out after %ss on Channel ssh and was killed" % (list(argv), timeout))
        greeting = "vmlab-call-server %s\n" % self._version
        if out.startswith(greeting):
            return out[len(greeting):]
        self.raise_ssh_failure(code, err)
        if out:
            raise ChannelError("something other than vmlab's call server answered on Guest port %d: %r" % (self._port, out[:300]), SERVER_FIX)
        return None


def copy_in(provider, src, guest_dir, timeout=None):
    """Provider.copy_in for Windows Guests: the Host file or folder src as a tar archive, carried
    in as a file by a Channel's own transfer (send_file), then unpacked by tar.exe, which Windows
    has. Not over exec's stdin: both Windows Channels hold a call's stdin whole (the call server
    in memory, vmrun in a file it copies in first), and a Build artifact may be large. The copy
    and the unpacking share timeout (default: the Lab's app.install_timeout)."""
    timeout = provider.lab.app.install_timeout if timeout is None else timeout
    deadline = hostpower.awake_time() + timeout

    def remaining():
        left = deadline - hostpower.awake_time()
        if left <= 0:
            raise GuestTimeout("copying %s into Guest %s did not finish within %ss" % (src, provider.lab.name, timeout))
        return left

    archive = r"%s\vmlab-copy-%s.tar" % (CALL_DIR, uuid.uuid4().hex)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "copy.tar"
        with tarfile.open(str(local), mode="w") as tar_file:
            tar_file.add(str(src), arcname=src.name)
        provider.send_file(local, archive, remaining())
    # The folder by its name as is: New-Item's -Path would take [ ] as a wildcard. Relative to
    # PowerShell's location, where a call starts, not the process's. Continue for tar: its stderr
    # may reach PowerShell as errors, which Stop would make fatal. Archives earlier copies left
    # (a Host that went away) go first; the Host's time, which a sent file keeps, does not compare
    # with the Guest's clock, so they age against this archive, which came the same way.
    script = (
        "$sent = (Get-Item -LiteralPath %s).LastWriteTime.AddHours(-1); "
        "Get-ChildItem -LiteralPath %s -Filter 'vmlab-copy-*.tar' | Where-Object { $_.LastWriteTime -lt $sent } | "
        "Remove-Item -Force -ErrorAction SilentlyContinue; "
        "$ErrorActionPreference = 'Stop'; try { $d = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(%s); "
        "$d = [IO.Directory]::CreateDirectory($d).FullName; $ErrorActionPreference = 'Continue'; tar.exe -xf %s -C $d; $code = $LASTEXITCODE } "
        "finally { Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath %s }; if ($code) { exit $code }; $d"
        % (ps_quote(archive), ps_quote(CALL_DIR), ps_path(guest_dir), ps_quote(archive), ps_quote(archive))
    )
    result = provider.exec(provider.shell_argv(script), remaining())
    if not result.ok:
        detail = "\n".join(result.stderr.strip().splitlines()[-15:]) or "(no output)"
        raise GuestError("copying %s into the Guest failed: %s" % (src, detail), "check free disk space in the Guest")
    return result.stdout.strip() + "\\" + src.name
