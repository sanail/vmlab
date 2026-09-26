# Adding a Provider

A **Provider** adapts one hypervisor to vmlab. Scenarios never see it. They talk to the Scenario API, which reaches the Provider through the Runner and Deploy. So a new Provider needs no changes to Scenarios, reports or the CLI. Vocabulary: `CONTEXT.md`. Channel design: ADR 0003.

The Tart Provider (`src/vmlab/providers/tart.py`) is a complete example: Base guest creation and provisioning, clones for Clean state, and SSH with a `tart exec` fallback. The Fusion Provider (`src/vmlab/providers/fusion.py`) shows the other shapes: a Base guest installed unattended from an ISO, linked clones restored by snapshot, and a guest-exec Channel (`vmrun`) that returns no output and so captures it in files named per call. Its Windows half (`fusion_windows.py`, with `providers/windows.py` for what any Windows Provider needs) shows a third: a Base guest a person makes, adopted by a wizard that checks each step, and copies instead of clones. UTM and Parallels are registered as stubs. Selecting one fails at config load and points here. Replacing a stub with a real Provider is the intended path.

## 1. Implement the interface

Subclass `vmlab.providers.base.Provider` in `src/vmlab/providers/<name>.py`. Standard library only; the CLI must run on the macOS system Python 3.9 (ADR 0001).

| Method | Contract |
| --- | --- |
| `validate_options(config_path, key, options, os_name)` (classmethod) | Check the Lab's `[labs.<lab>.<name>]` table (`os_name` is the Lab's `os`). Raise `ConfigError(config_path, "<key>.<option>", problem, fix)` for anything wrong, including unknown keys. |
| `SUPPORTED_OS` (class attribute) | The Lab OSes it runs, e.g. `("macos",)`, or `None` for all. Config load rejects other Labs. |
| `HYPERVISOR` (class attribute) and `hypervisor()` (classmethod) | The hypervisor's name, and `(found, detail, fix)`: is it installed and usable on this Host? Needs no Lab: `doctor`'s Host section lists every registered Provider's. |
| `detect()` | `(ok, detail, fix)` for this Lab's hypervisor, usually `hypervisor()`. `doctor` shows it first. Only the hypervisor: Base guests belong in `diagnose()`. |
| `is_running()` | Is this Lab's Guest powered on? Must be cheap; the Runner calls it often. |
| `start()` | Power the Guest on and return **without** waiting for boot. `deploy` and `run` print its steps (`vmlab.progress`): wrap making the Guest's clone, when it makes one, in `self.progress.step("cloning")`, and powering it on in `self.progress.step("booting")`. |
| `stop()` | Power it off. The base `down()` calls it only when the Guest is running. |
| `is_reachable()` | Has it booted far enough for its Channels to work (e.g. it has an IP and SSH answers)? The base `up()` polls it until the Lab's `boot_timeout`. |
| `boot_state()` (optional) | Once `up()` has waited out `boot_timeout`: how far the boot got, a `BootState(reached_os, evidence)`, e.g. `BootState(False, "VMware Tools never answered")`, or `None` (the default) when the hypervisor cannot tell. `up()`'s error then says the OS never came up instead of suggesting a longer `boot_timeout`. |
| `channels()` | The Guest's Channels, preferred first (see below). The base `exec()` falls back through them. |
| `restore()` | Return the Guest to its Clean state (snapshot revert, or re-clone from the Base guest), whether it runs or not. It must be running and reachable when this returns; a stopped Guest boots once, into its Clean state (a Suite run that starts on a stopped Guest does not start it first). |
| `copy_in(src, guest_dir, timeout=None)` | Copy a Host file or folder into `guest_dir` (created; `~` is the Guest user's home) within `timeout` seconds, all of it (default: the Lab's `app.install_timeout`, for Build artifacts), and raise `GuestTimeout` when it runs out. Return the absolute Guest path of the copy. `g.put` and `vmlab put` are built on it (`put_file`), with the call's timeout. `copy_in_by_tar` does it over `exec`'s stdin for POSIX Guests; `vmlab.providers.windows.copy_in` does it for Windows ones by `send_file` and `tar.exe`, since Windows Channels hold a call's stdin whole. A file already at the copy's path is replaced, a symlink too (not written through), as `tar -x` does. |
| `screenshot(dest)` | Write a PNG of the Guest's screen to `dest`, Host-side where the hypervisor can; then set `HOST_SCREENSHOTS = True`, and a Run whose Guest did not come up keeps its screen as `screenshots/boot-timeout.png`. |

What depends on the Guest OS (its shell, its paths, how it reads and removes files, spawns processes, checks conditions for `wait_for` and drives the UI) is not the Provider's: `provider.guest_os` holds it, one object per Guest OS in `src/vmlab/guestos.py`, by default the one for the Lab's `os`. The methods below delegate to it, so a Provider never compares `lab.os` for them. The Fake Provider sets `guest_os` to `guestos.FakeGuestOS`, since its commands run on the Host whatever its Lab's `os`.

These are built on the methods above; override them only when the Guest needs something else:

- `up()`, `down()` and `exec()` (Channel fallback). `up()` prints its wait for the Channels as the step `waiting for Channels`, through `self.progress`, which the runner sets as it sets `on_exec`.
- `shell_argv(command)`: `sh -c`, or PowerShell on Windows.
- `remove_paths(paths, timeout)`: app state reset.
- `spawner()`: how `g.spawn` starts, checks and stops background processes (`vmlab.providers.spawning`): `PosixSpawner` (process groups, logs in `/tmp`), or `WindowsSpawner` (Job Objects, logs in `%TEMP%`) for Windows Labs. Both ride on `exec`; the Fake Provider's `guestos.FakeGuestOS` puts logs where `read_file` looks.
- `read_file(guest_path, timeout)`: `g.get` and `vmlab get`, the bytes of a Guest file (`GuestError` naming it when it is not there). The default reads it over `exec` as base64 with the Guest's shell (`sh` or PowerShell), with `put_file`'s path rules; the Fake Provider overrides it to map the path into its Host folder.
- `probe_argv()`: the command `doctor` sends down each Channel (and `doctor --bench` times).
- `diagnose()`: `doctor`'s checks of what the Guest is made from, answerable while it is stopped (its Base guest, its clone), as `[(check, status, detail, fix)]` with a status from `vmlab.providers.base` (`OK`, `INFO`, `WARN`, `FAIL`). A `FAIL` means the Guest cannot start, and doctor checks nothing further for the Lab. The default has none.
- `diagnose_guest()`: the same for a running Guest whose Channels work, for what only this Provider's Guests can get wrong (Tart: a TCC consent dialog blocking Apple Events, or a Screen Recording alert raised by a screenshot over each Channel; Fusion: a Linux Guest logged into the wrong desktop session). Channels, one screenshot and the UI helper are checked for every Provider.
- `wrap_argv(argv, env)`: how every command is run; the default runs it as is. The Fusion Provider wraps Linux commands so they get the desktop session's environment.
- `ui_call(command, params, timeout)`: the UI contract. It runs the Guest OS's UI helper (`vmlab.uihelpers`) over the Guest's Channels, so a Provider gets the UI contract for free once exec works. Only a Provider whose Guests need no helper overrides it, as the Fake Provider does.

The UI helpers are per OS, not per Provider: `src/vmlab/guest/<os>/` holds them, `src/vmlab/uihelpers.py` runs them and each Guest OS (`src/vmlab/guestos.py`) picks its own. A helper takes `COMMAND JSON` and prints one JSON object; `src/vmlab/ui.py` does everything above that once (roles, node shape, matching, chords, waiting). Provisioning a Base guest must install what the helper needs, so no Run compiles or installs anything (the macOS helper is compiled then; the Linux helper is plain Python sent with each call, and provisioning installs the GNOME Shell extension it drives on Wayland; the Windows helper is sent with each call too, and provisioning compiles its C# into the snapshot; the one exception is a Windows Base guest provisioned by an older vmlab, whose Labs compile the new helper on their first UI call after a restore).

Every call into the hypervisor must be bounded: `start`, `stop`, `restore`, `screenshot` and `boot_state` take no timeout argument, so use the Lab's `step_timeout` (or `boot_timeout` for start and restore) and raise `GuestTimeout` when it runs out. `copy_in` and `send_file` take the caller's timeout and must end within it, every step together (a Scenario's `g.put` passes what is left of its clock). A hung hypervisor must never hang a suite.

`guest_id` names the Guest uniquely per project and Lab. Use it for the hypervisor's VM name, so projects never share a Guest by accident.

Guests have no sound device: nothing a Guest plays may reach the Host's speakers or headset, and a Guest must never take the Host's Bluetooth headset. Start every Guest without one, and make sure a snapshot revert cannot bring it back (Tart runs Guests with `--no-audio`; Fusion sets `sound.present = "FALSE"` at every start), and have `doctor` warn about any of the Provider's VMs that still has one (Fusion: `sound_findings()`, a Host check).

A Provider that keeps VMs on the Host describes them for `vmlab clean` with a `HostVMs` inventory (`vmlab.clean`): its VMs and whether they run, how to delete one (stopping it first if it runs), how the person stops a running Base guest, its service files, and `old_snapshots(vms)`, the snapshots of earlier Base guest provisionings its VMs keep (Tart: none, a Base guest is provisioned again in place; Fusion: `fusion.old_snapshots()`, whose items say whether a linked clone still needs them or, for an encrypted VM, how to delete them in Fusion's window, and a `doctor` Host check).

Keep anything machine-specific or secret out of the project: keys, known_hosts, passwords and registries go under `vmlab.home.vmlab_home()` (mode 0700) or the Keychain.

## 2. Implement its Channels

Subclass `vmlab.providers.base.Channel`. Set a short `name` (it appears in reports and in `doctor`) and implement `exec(argv, timeout, env, stdin=None)`:

- Return an `ExecResult(argv, code, stdout, stderr)` whenever the command ran, **whatever its exit code**.
- Raise `ChannelError(message, fix)` when the Channel itself failed: it could not connect, lost the session, or its helper is missing. Only this makes vmlab try the next Channel.
- Raise `GuestTimeout(message)` after **killing** the call when it exceeds `timeout`. Never fall back on a timeout, because the command may have run. To kill Host-side helpers with their children, use `vmlab.hostproc.run`, which kills the whole process group.
- Pass `argv` without re-splitting it. Quote each element for the remote shell, so spaces and quotes arrive intact.
- Apply `env` to the command only, never to the Guest's global environment.
- Use a unique output file per call if the Channel captures output through files. Never use a shared one: concurrent calls would race.
- Implement `send_file(local, guest_path, timeout)` if the Channel can carry a file of any size (scp, the hypervisor's own file copy). Windows Guests need it for `copy_in`: their Channels hold a call's stdin whole (the call server in memory), too much for a Build artifact. Raise `ChannelError` when the Channel cannot carry the file (the next one may), and `GuestTimeout` after killing a copy that outlives `timeout`: the time is spent, so it does not fall back.

Typical Channels: SSH with vmlab's own key and known_hosts (multiplexed; reuse `vmlab.providers.ssh.SshChannel`), the hypervisor's guest-exec (`tart exec`, `vmrun runProgramInGuest`), and on Windows SSH to vmlab's call server in the desktop session (`vmlab.providers.windows.WindowsSshChannel`). ADR 0003 has the defaults per OS.

## 3. Register it

Add it to `PROVIDERS` in `src/vmlab/providers/__init__.py`, replacing the stub if there is one. Its name is what Labs write as `provider = "<name>"`. Mention it in the README's config reference and in the config template in `src/vmlab/vendoring.py`.

## 4. Test it

vmlab has two seams (spec 0001, *Testing Decisions*):

- **Seam 1** (every commit): `python3 -m unittest discover -s tests` drives the built zipapp against the **Fake Provider**. It covers everything that does not depend on a real hypervisor: config, lifecycle, fallback, timeouts, deploy and reports. A new Provider rarely needs Seam 1 tests, except for its own config validation.
- **Seam 2** (manual or scheduled): `tests/contract/test_contract.py` checks the Provider/Channel contract against a **real** Lab through the CLI. It covers up, every Channel healthy, exec (stdout, stderr, exit code, env, quoting), timeouts, deploy (copy in and install), screenshots, every UI contract command (in the OS's stock text editor; on macOS also a cold WebKit page in Safari), restore and down. On macOS, run it a second time with `VMLAB_UI_HELPER=jxa` to cover the JXA fallback. With no settings it runs against a Fake Lab, so it stays green in Seam 1 too.

To run Seam 2 against your Provider, describe one Lab in a TOML file **without** an `[labs.<lab>.app]` table (the suite adds its own):

```toml
# my-lab.toml
[labs.mac]
provider = "<name>"
os = "macos"

[labs.mac.<name>]
# the Provider's options
```

```sh
python3 tools/build.py
VMLAB_CONTRACT_LAB_FILE=my-lab.toml VMLAB_CONTRACT_LAB=mac \
    python3 -m unittest discover -s tests -p 'test_contract.py' -v
```

The Lab gets a project of its own under `target/contract/<lab>` in this repo (ignored by git), so its Guest is reused between runs and never touches your projects; its Base guest comes from `$VMLAB_HOME` as usual. Run the suite once per OS the Provider supports. A Provider is done when:

- Seam 2 is green for each supported OS.
- `vmlab doctor` gives a fix for every failure you met while getting there.
- Seam 1 stays green.
