"""The SSH Channel, with vmlab's own key and known_hosts.

vmlab never uses the user's keys, ssh config or known_hosts (spec: Host state).
A Guest's host key is pinned when its Base guest is provisioned and is looked
up by an alias, the Base guest's name, never by IP: DHCP hands Guest IPs out
again and again, so IP-keyed entries either break or trust the wrong Guest.
Clones share their Base guest's host key.

Calls are multiplexed over one master connection per Guest (ADR 0003).
"""

import hashlib
import re
import shlex
import socket
import subprocess

from vmlab import hostproc
from vmlab.home import vmlab_home
from vmlab.providers.base import Channel, ChannelError, ExecResult, GuestError, GuestTimeout

CONNECT_TIMEOUT = 5
MASTER_PERSIST = 600  # seconds an idle master connection stays up
MAX_SOCKET_PATH = 100  # sun_path is 104 bytes on macOS
# ssh exits 255 on its own failures, and so may a remote command. ssh's own
# failures are told apart by what it prints.
SSH_FAILURE = re.compile(
    r"^(ssh: |kex_exchange_identification|Connection (closed|reset|timed out|refused)|.*Permission denied \(|"
    r"Host key verification failed|client_loop: |Received disconnect|mux_client|Control socket|"
    r"@+\s*WARNING: REMOTE HOST IDENTIFICATION)",
    re.M,
)


def ssh_dir():
    path = vmlab_home() / "ssh"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def key_path():
    """vmlab's passwordless key, created on first use. Never the user's own key."""
    key = ssh_dir() / "id_ed25519"
    if not key.exists():
        code, _, err = hostproc.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vmlab", "-f", str(key)], 30)
        if code:
            raise GuestError("cannot create vmlab's SSH key %s: %s" % (key, err.strip()), "check that ssh-keygen works")
    return key


def public_key():
    key_path()
    return (ssh_dir() / "id_ed25519.pub").read_text(encoding="utf-8").strip()


def pin_host_key(alias, host_key):
    """Trust host_key ("<type> <base64> [comment]") for alias, replacing any earlier pin."""
    path = ssh_dir() / "known_hosts"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if line.split(" ", 1)[0] != alias]
    lines.append("%s %s" % (alias, " ".join(host_key.split()[:2])))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_pinned(alias):
    """Is a host key pinned for alias?"""
    path = ssh_dir() / "known_hosts"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return any(line.split(" ", 1)[0] == alias for line in lines)


def unpin_host_key(alias):
    """Forget alias's pinned host key, e.g. when its Base guest is deleted."""
    path = ssh_dir() / "known_hosts"
    if path.exists():
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.split(" ", 1)[0] != alias]
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def remote_command(argv, env, path_append=()):
    """A command line for the Guest's POSIX shell that runs argv with env, every word quoted."""
    words = ["exec"]
    if env:
        words += ["env"] + [shlex.quote("%s=%s" % kv) for kv in sorted(env.items())]
    words += [shlex.quote(a) for a in argv]
    prefix = 'PATH="$PATH:%s"; export PATH; ' % ":".join(path_append) if path_append else ""
    return prefix + " ".join(words)


class SshChannel(Channel):
    name = "ssh"

    def __init__(self, user, host, alias, guest, path_append=()):
        """host() returns the Guest's current IP, or None; alias names its pinned host key;
        guest names the master connection, so there is one per Guest."""
        self.user = user
        self._host = host
        self.alias = alias
        self.path_append = path_append
        digest = hashlib.sha1(guest.encode("utf-8")).hexdigest()[:16]
        control = ssh_dir() / ("cm-%s" % digest)
        self._control = control if len(str(control)) <= MAX_SOCKET_PATH else None
        self._sessions = self._control  # the master connection that sessions (commands, scp) ride on, or None

    def _options(self):
        """ssh's options for sessions (commands, scp), over the master connection where they may use it."""
        return self._common_options() + ["-o", "ControlPath=%s" % (self._sessions or "none")]

    def _master_options(self):
        """ssh's options for the master connection itself, and for what rides on it."""
        return self._common_options() + ["-o", "ControlPath=%s" % (self._control or "none")]

    def _common_options(self):
        return [
            "-F", "/dev/null",
            "-i", str(key_path()),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=%d" % CONNECT_TIMEOUT,
            "-o", "HostKeyAlias=%s" % self.alias,
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=%s" % (ssh_dir() / "known_hosts"),
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
        ]  # fmt: skip

    def _target(self):
        host = self._host()
        if not host:
            raise ChannelError("the Guest has no IP address yet", "wait for it to boot, then `vmlab doctor`")
        return "%s@%s" % (self.user, host)

    def _master_alive(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(self._control))
            return True
        except OSError:
            return False
        finally:
            client.close()

    def _ensure_master(self, target):
        """Start the master connection if there is none. Best effort: without one, calls connect directly."""
        if not self._control or self._master_alive():
            return
        if self._control.exists():
            self._control.unlink()  # left by a master that died
        try:
            # Its own session and no pipes: -f leaves the master running in the background,
            # holding whatever it was given.
            subprocess.run(
                ["ssh"] + self._master_options() + ["-o", "ControlMaster=yes", "-o", "ControlPersist=%d" % MASTER_PERSIST, "-f", "-N", target],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CONNECT_TIMEOUT + 5,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            pass

    def send_file(self, local, guest_path, timeout):
        """Copy a Host file into the Guest with scp, which carries any size. guest_path is a Guest
        path; scp takes it with forward slashes."""
        target = self._target()
        remote = "%s:%s" % (target, str(guest_path).replace("\\", "/"))
        try:
            code, out, err = hostproc.run(["scp"] + self._options() + ["-p", str(local), remote], timeout)
        except subprocess.TimeoutExpired:
            raise GuestTimeout("scp of %s into the Guest did not finish within %ss and was killed" % (local, timeout))
        if code:
            lines = (err or out).strip().splitlines()
            raise ChannelError("scp of %s into the Guest failed: %s" % (local, lines[-1] if lines else "exit %s" % code), "check that the Guest runs sshd")

    def close(self):
        """Stop the master connection, e.g. before the Guest stops or changes."""
        if not self._control or not self._control.exists():
            return
        try:
            hostproc.run(["ssh"] + self._master_options() + ["-O", "exit", "vmlab-guest"], 10)
        except subprocess.TimeoutExpired:
            pass
        if self._control.exists():
            self._control.unlink()

    def exec(self, argv, timeout, env, stdin=None):
        return self.run_command(remote_command(argv, env, self.path_append), argv, timeout, stdin)

    def run_command(self, remote, argv, timeout, stdin):
        """Run the command line remote in the Guest's login shell; argv is what the result reports."""
        target = self._target()
        if self._sessions:
            self._ensure_master(target)
        command = ["ssh"] + self._options() + ["-o", "ControlMaster=no", target, remote]
        try:
            code, out, err = hostproc.run(command, timeout, stdin=stdin)
        except subprocess.TimeoutExpired:
            raise GuestTimeout("%s timed out after %ss on Channel ssh and was killed" % (list(argv), timeout))
        self.raise_ssh_failure(code, err)
        return ExecResult(list(argv), code, out, err)

    def raise_ssh_failure(self, code, err):
        """ChannelError if ssh itself failed (exit 255 and its own message), not the command."""
        if code == 255 and SSH_FAILURE.search(err):
            raise ChannelError(
                "ssh %s failed: %s" % (self._target(), err.strip().splitlines()[-1]),
                "check that the Guest booted and runs sshd; `vmlab doctor` tests every Channel",
            )
