# Each Guest picks its fastest reliable Channel, with a fallback

We do not force one **Channel** everywhere. macOS and Linux **Guests** use SSH (multiplexed, ~tens of ms per command, native stdout). Windows **Guests** on VMware Fusion use `vmrun runProgramInGuest -interactive`, with a unique output file per call, and fall back to SSH plus an interactive Scheduled Task.

The reason Windows differs: an SSH session on Windows cannot touch the logged-in user's desktop, so UI automation over SSH needs a Scheduled Task hop, while `vmrun -interactive` reaches the desktop directly and is proven in practice. On providers without such a native exec (Hyper-V, UTM, Parallels without tools), SSH plus Scheduled Task becomes primary. `vmlab doctor --bench` measures both Channels so defaults can be revisited with data.

**Verified on Tart (macOS 26 Guest, Tart 2.37, guest agent 0.14).** `tart exec` runs in the logged-in GUI (Aqua) session but has no TCC grants of its own: its first Apple Event to System Events opens a consent dialog nobody can click, and the call hangs. Once provisioning writes TCC grants for the agent binary, `tart exec` drives System Events like SSH does. It stays the fallback, not the primary, because it is slower: a no-op takes about 60 ms over `tart exec` and about 14 ms over multiplexed SSH. Provisioning grants both Channels, so a fallback also carries UI calls. The grants name the agent by path, so upgrading the agent inside a Base guest means re-provisioning it.

Fallback happens only when a Channel reports that it could not carry the call (cannot connect, session lost, helper missing). A command that exits non-zero is a result, not a Channel failure. A call that times out is killed and fails the Run without falling back, because the command may already have run in the Guest and repeating it over another Channel could run it twice. A Channel that hangs on every call therefore shows up as a timeout, and `vmlab doctor` names it.

## Considered options

- **SSH everywhere** — one quoting model and provider-independent, but the Windows desktop is only reachable through an extra hop.
- **Provider-native exec everywhere** — `vmrun` loses stdout and costs seconds per call; `tart exec` works with TCC grants but is about 4× slower than multiplexed SSH.
