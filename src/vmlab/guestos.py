"""The Guest OS: how vmlab runs commands, names paths and drives the UI inside a Guest.

One object per Guest OS, picked once by the Provider (Provider.guest_os); the
rest of vmlab asks it instead of comparing lab.os. macOS and Linux share a POSIX
base, and each one's quirks are overrides in its own class. A Fake Lab's
commands run on the Host, whatever its os: its Guest OS is FakeGuestOS, which is
POSIX but keeps its Lab's Guest OS for the UI contract.

The error handling around these commands (exit codes, messages) stays on the Provider.
"""

from vmlab import ui, uihelpers
from vmlab.config import UsageError
from vmlab.providers import spawning
from vmlab.providers.base import NO_FILE, ps_path, sh_expand_tilde


class GuestOS:
    separators = "/"  # between a Guest path's folders
    stage_app = None  # the app stage-text opens a Staged document in, unless told otherwise
    native_roles = {}  # native role -> cross-OS role (vmlab.ui)

    def shell_argv(self, command):
        """argv that runs a command line in the Guest's shell."""
        raise NotImplementedError

    def probe_argv(self):
        """A command that succeeds on any healthy Guest, used to test Channels."""
        raise NotImplementedError

    def read_file_argv(self, path):
        """argv that prints the Guest file path base64-encoded, or exits NO_FILE when it is not a file."""
        raise NotImplementedError

    def remove_paths_argv(self, paths):
        """argv that deletes the Guest paths (files or folders) that exist."""
        raise NotImplementedError

    def spawner(self, provider):
        """How g.spawn starts, checks and stops background processes (vmlab.providers.spawning)."""
        raise NotImplementedError

    def probes(self):
        """The commands behind wait_for's process, file and exec conditions (vmlab.ui)."""
        raise NotImplementedError

    def ui_helper(self, provider):
        """The Guest-side helper behind the UI contract (vmlab.uihelpers)."""
        raise NotImplementedError

    def split_path(self, guest_path):
        """(folder, name) of a Guest file path; a bare name is in the Guest user's home."""
        cut = max(guest_path.rfind(sep) for sep in self.separators)
        folder, name = guest_path[:cut], guest_path[cut + 1 :]
        if not name or name in (".", "..", "~"):
            raise UsageError("%r is not a file path; give the file's name, e.g. ~/notes.txt" % guest_path)
        if cut < 0:
            folder = "~"
        elif not folder or folder.endswith(":"):
            folder = guest_path[: cut + 1]  # the root, / or C:\
        return folder, name


class Posix(GuestOS):
    def shell_argv(self, command):
        return ["sh", "-c", command]

    def probe_argv(self):
        return ["true"]

    def read_file_argv(self, path):
        script = 'p=$1; %s[ -f "$p" ] || exit %d; base64 < "$p"' % (sh_expand_tilde("p"), NO_FILE)
        return ["sh", "-c", script, "sh", path]

    def remove_paths_argv(self, paths):
        script = 'for p in "$@"; do %srm -rf -- "$p"; done' % sh_expand_tilde("p")
        return ["sh", "-c", script, "sh"] + list(paths)

    def spawner(self, provider):
        return spawning.PosixSpawner(provider)

    def probes(self):
        return ui.PosixProbes()


class MacOS(Posix):
    stage_app = "TextEdit"
    native_roles = ui.MACOS_ROLES

    def ui_helper(self, provider):
        return uihelpers.MacHelper(provider)


class Linux(Posix):
    stage_app = "gnome-text-editor"
    native_roles = ui.LINUX_ROLES

    def probes(self):
        return ui.LinuxProbes()  # Linux keeps only the first 15 bytes of a process's name

    def ui_helper(self, provider):
        return uihelpers.LinuxHelper(provider)


class Windows(GuestOS):
    separators = "\\/"
    stage_app = "Notepad"
    native_roles = ui.WINDOWS_ROLES

    def shell_argv(self, command):
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]

    def probe_argv(self):
        return ["cmd", "/c", "exit 0"]

    def read_file_argv(self, path):
        # Shared for writing: a process may still be writing the file (a log, a spawned process's
        # output), and ReadAllBytes refuses a file another handle has open for writing.
        return self.shell_argv(
            "$p = %s; if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { exit %d }; "
            "$f = [IO.File]::Open((Get-Item -LiteralPath $p).FullName, 'Open', 'Read', 'ReadWrite, Delete'); "
            "$m = New-Object IO.MemoryStream; $f.CopyTo($m); $f.Close(); [Convert]::ToBase64String($m.ToArray())" % (ps_path(path), NO_FILE)
        )

    def remove_paths_argv(self, paths):
        # exit 0: PowerShell exits 1 when its last command failed, even with the error silenced,
        # and a path that is not there is exactly what this asks for.
        return self.shell_argv(
            "foreach ($p in @(%s)) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }; exit 0"
            % ", ".join(ps_path(p) for p in paths)
        )

    def spawner(self, provider):
        return spawning.WindowsSpawner(provider)

    def probes(self):
        return ui.PowerShellProbes()

    def ui_helper(self, provider):
        return uihelpers.WindowsHelper(provider)


class FakeGuestOS(Posix):
    """A Fake Lab's Guest OS: commands run in the Host's POSIX shell; the UI contract is that of
    the Lab's own Guest OS."""

    def __init__(self, guest_os):
        self.guest_os = guest_os

    @property
    def stage_app(self):
        return self.guest_os.stage_app

    @property
    def native_roles(self):
        return self.guest_os.native_roles

    def spawner(self, provider):
        return spawning.PosixSpawner(provider, log_dir="~")  # in the Guest's home, where read_file finds them

    def probes(self):
        # A POSIX Guest OS's Probes run in the Host's sh as they are, Linux's name limit too;
        # Windows's are PowerShell.
        return self.guest_os.probes() if isinstance(self.guest_os, Posix) else super().probes()

    def ui_helper(self, provider):
        return self.guest_os.ui_helper(provider)


GUEST_OSES = {"macos": MacOS(), "linux": Linux(), "windows": Windows()}


def for_os(os_name):
    """The Guest OS of a Lab's os."""
    return GUEST_OSES[os_name]
