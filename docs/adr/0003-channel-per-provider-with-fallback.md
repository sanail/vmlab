# Each Guest picks its fastest reliable Channel, with a fallback

We do not force one **Channel** everywhere. macOS and Linux **Guests** use SSH (multiplexed, ~tens of ms per command, native stdout). Windows **Guests** on VMware Fusion use `vmrun runProgramInGuest -interactive`, with a unique output file per call, and fall back to SSH plus an interactive Scheduled Task.

The reason Windows differs: an SSH session on Windows cannot touch the logged-in user's desktop, so UI automation over SSH needs a Scheduled Task hop, while `vmrun -interactive` reaches the desktop directly and is proven in practice. On providers without such a native exec (Hyper-V, UTM, Parallels without tools), SSH plus Scheduled Task becomes primary. `vmlab doctor --bench` measures both Channels so defaults can be revisited with data.

## Considered options

- **SSH everywhere** — one quoting model and provider-independent, but the Windows desktop is only reachable through an extra hop.
- **Provider-native exec everywhere** — `vmrun` loses stdout and costs seconds per call; `tart exec`'s access to the GUI session and TCC grants is unverified.
