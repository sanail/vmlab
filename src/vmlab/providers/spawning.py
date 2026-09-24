"""Background processes in a Guest (g.spawn): start one detached, ask if it runs, stop it.

A spawned process writes its stdout and stderr, together, to a log file in the
Guest. Stopping ends what it started too:

- POSIX: it leads its own process group (setsid, or perl's where there is no
  setsid(1), as on macOS); stop sends the group SIGTERM, then SIGKILL.
- Windows: cmd.exe starts it, holding the redirect of both streams to the log;
  stop ends that process tree (taskkill /T). Its pid is that cmd.exe's, and its
  start time guards against a reused pid.

Every call rides on Provider.exec. A process is described by the dict start()
returns: {"pid", "log"[, "started"]}.
"""

import base64
import subprocess
import uuid

from vmlab.config import UsageError, is_argv
from vmlab.providers.base import GuestError, ps_quote, sh_expand_tilde

NO_COMMAND = 127  # the start script's exit code when the Guest has no such command
GONE = 4  # the stop script's exit code when nothing of the process was left to stop
STOPPED, EXITED = "stopped", "exited"  # what stop() found
TERM_SECONDS = 5  # how long a process gets to end on SIGTERM before SIGKILL (POSIX)


def validate_argv(argv):
    """argv as a list; UsageError unless it is a command (config.is_argv)."""
    if not is_argv(argv):
        raise UsageError("spawn needs a command: a non-empty argv list of strings, e.g. [\"python3\", \"-m\", \"http.server\"]")
    return list(argv)


class PosixSpawner:
    def __init__(self, provider, log_dir="/tmp"):
        self.provider = provider
        self.log_dir = log_dir  # ~ is the Guest user's home

    def start(self, argv, env, timeout):
        """Start argv detached; returns {"pid", "log"}: its log is in log_dir."""
        log = "%s/vmlab-spawn-%s.log" % (self.log_dir, uuid.uuid4().hex[:12])
        script = "log=$1; shift; " + sh_expand_tilde("log") + (
            'command -v "$1" >/dev/null 2>&1 || { echo "no such command: $1" >&2; exit %d; }; '
            "if command -v setsid >/dev/null 2>&1; then "
            'setsid "$@" </dev/null >"$log" 2>&1 & '
            "else "
            "perl -e 'use POSIX (); POSIX::setsid(); exec { $ARGV[0] } @ARGV or die \"cannot run $ARGV[0]: $!\\n\"' \"$@\" </dev/null >\"$log\" 2>&1 & "
            "fi; "
            'echo "$!"' % NO_COMMAND
        )
        result = self.provider.exec(["sh", "-c", script, "sh", log] + argv, timeout, env=env)
        [pid] = _parse(argv, result, 1)
        return {"pid": int(pid), "log": log}

    def running(self, process, timeout):
        return self.provider.exec(["kill", "-0", str(process["pid"])], timeout).ok

    def stop(self, process, timeout):
        """End the process and its group: STOPPED, or EXITED if nothing of it was left. GuestError if it lives on."""
        polls = TERM_SECONDS * 10
        script = (
            'g=-$1; kill -0 "$g" 2>/dev/null || exit %d; kill -TERM "$g" 2>/dev/null; i=0; '
            'while kill -0 "$g" 2>/dev/null; do '
            '[ $i -eq %d ] && kill -KILL "$g" 2>/dev/null; [ $i -ge %d ] && exit 1; i=$((i+1)); sleep 0.1; '
            "done; exit 0" % (GONE, polls, polls + 20)
        )
        return _stopped(process, self.provider.exec(["sh", "-c", script, "sh", str(process["pid"])], timeout))


class WindowsSpawner:
    def __init__(self, provider):
        self.provider = provider

    def start(self, argv, env, timeout):
        """Start argv detached; returns {"pid", "started", "log"}: cmd.exe's pid and start time."""
        if any(c in a for a in argv for c in "\r\n"):
            raise UsageError("spawn on Windows cannot pass a line break in an argument (cmd.exe ends the command there); put the text in a file with g.put")
        if '"' in argv[0]:
            raise UsageError("spawn: %r is not a program name" % argv[0])
        line = base64.b64encode(cmd_line(argv).encode("utf-8")).decode("ascii")
        script = (
            "$log = [Environment]::ExpandEnvironmentVariables('%%TEMP%%\\vmlab-spawn-%s.log'); "
            "if (-not (Get-Command -CommandType Application -Name %s -ErrorAction SilentlyContinue)) "
            "{ [Console]::Error.WriteLine('no such command: ' + %s); exit %d }; "
            "$info = New-Object Diagnostics.ProcessStartInfo; "
            "$info.FileName = Join-Path $env:SystemRoot 'System32\\cmd.exe'; "
            "$q = [char]34; $line = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')); "
            "$info.Arguments = '/d /s /c ' + $q + $line + ' <nul >' + $q + $log + $q + ' 2>&1' + $q; "
            "$info.UseShellExecute = $false; $info.CreateNoWindow = $true; $info.WorkingDirectory = $env:USERPROFILE; "
            "$p = [Diagnostics.Process]::Start($info); $p.Id; $p.StartTime.ToFileTimeUtc(); $log"
            % (uuid.uuid4().hex[:12], ps_quote(argv[0]), ps_quote(argv[0]), NO_COMMAND, line)
        )
        result = self.provider.exec(self.provider.shell_argv(script), timeout, env=env)
        pid, started, path = _parse(argv, result, 3)
        return {"pid": int(pid), "started": int(started), "log": path}

    def _find(self, process):
        """PowerShell setting $p to the process, or $null once it has ended (or its pid is another's)."""
        return (
            "$p = Get-Process -Id %d -ErrorAction SilentlyContinue; "
            "if ($p -and $p.StartTime.ToFileTimeUtc() -ne %d) { $p = $null }; " % (process["pid"], process["started"])
        )

    def running(self, process, timeout):
        return self.provider.exec(self.provider.shell_argv(self._find(process) + "if ($p) { exit 0 } else { exit 1 }"), timeout).ok

    def stop(self, process, timeout):
        script = self._find(process) + (
            "if (-not $p) { exit %d }; & taskkill.exe /T /F /PID %d 2>&1 | Out-Null; "
            "if (-not $p.WaitForExit(%d)) { exit 1 }; exit 0" % (GONE, process["pid"], (TERM_SECONDS + 2) * 1000)
        )
        return _stopped(process, self.provider.exec(self.provider.shell_argv(script), timeout))


def cmd_line(argv):
    """argv as a cmd.exe command line that reaches the program unchanged.

    The program name is quoted plainly (cmd.exe finds the program from it); the arguments get
    the C runtime's quoting, then a ^ before each character cmd.exe would act on, quotes
    included, so none of them opens a quoted stretch where carets stop working."""
    args = "".join("^" + c if c in '()%!^"<>&|' else c for c in subprocess.list2cmdline(argv[1:]))
    return '"%s"%s' % (argv[0], " " + args if args else "")


def _parse(argv, result, fields):
    if result.code == NO_COMMAND:
        raise GuestError("spawn %s: the Guest has no such command" % argv, "check the command's name and that it is installed in the Guest")
    lines = result.stdout.strip().splitlines()
    if not result.ok or len(lines) != fields:
        detail = result.stderr.strip() or "exit %d, output %r" % (result.code, result.stdout[-200:])
        raise GuestError("starting %s in the Guest failed: %s" % (argv, detail))
    return [line.strip() for line in lines]


def _stopped(process, result):
    if result.code == GONE:
        return EXITED
    if not result.ok:
        raise GuestError(
            "spawned process %d (log %s) is still running after vmlab tried to stop it%s"
            % (process["pid"], process["log"], ": " + result.stderr.strip() if result.stderr.strip() else ""),
            "look at it in the Guest; it may ignore signals or belong to another user",
        )
    return STOPPED
