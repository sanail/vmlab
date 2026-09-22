# Adding a Provider

A **Provider** adapts one hypervisor to vmlab. Scenarios never see it. They talk to the Scenario API, which reaches the Provider through the Runner and Deploy. So a new Provider needs no changes to Scenarios, reports or the CLI. Vocabulary: `CONTEXT.md`. Channel design: ADR 0003.

The Tart Provider (`src/vmlab/providers/tart.py`) is a complete example: Base guest creation and provisioning, clones for Clean state, and SSH with a `tart exec` fallback. UTM and Parallels are registered as stubs. Selecting one fails at config load and points here. Replacing a stub with a real Provider is the intended path.

## 1. Implement the interface

Subclass `vmlab.providers.base.Provider` in `src/vmlab/providers/<name>.py`. Standard library only; the CLI must run on the macOS system Python 3.9 (ADR 0001).

| Method | Contract |
| --- | --- |
| `validate_options(config_path, key, options)` (classmethod) | Check the Lab's `[labs.<lab>.<name>]` table. Raise `ConfigError(config_path, "<key>.<option>", problem, fix)` for anything wrong, including unknown keys. |
| `SUPPORTED_OS` (class attribute) | The Lab OSes it runs, e.g. `("macos",)`, or `None` for all. Config load rejects other Labs. |
| `detect()` | `(ok, detail, fix)`: is the hypervisor installed and usable? `doctor` shows this first. |
| `is_running()` | Is this Lab's Guest powered on? Must be cheap; the Runner calls it often. |
| `start()` | Power the Guest on and return **without** waiting for boot. |
| `stop()` | Power it off. The base `down()` calls it only when the Guest is running. |
| `is_reachable()` | Has it booted far enough for its Channels to work (e.g. it has an IP and SSH answers)? The base `up()` polls it until the Lab's `boot_timeout`. |
| `channels()` | The Guest's Channels, preferred first (see below). The base `exec()` falls back through them. |
| `restore()` | Return the running Guest to its Clean state (snapshot revert, or re-clone from the Base guest). It must be reachable again when this returns. |
| `copy_in(src, guest_dir)` | Copy a Host file or folder into `guest_dir` (created; `~` is the Guest user's home). Return the absolute Guest path of the copy. |
| `screenshot(dest)` | Write a PNG of the Guest's screen to `dest`, Host-side where the hypervisor can. |

These are built on the methods above; override them only when the Guest needs something else:

- `up()`, `down()` and `exec()` (Channel fallback).
- `shell_argv(command)`: `sh -c`, or PowerShell on Windows.
- `remove_paths(paths, timeout)`: app state reset.
- `probe_argv()`: the command `doctor` sends down each Channel.
- `ui_call(command, params, timeout)`: the UI contract. It runs the Guest OS's UI helper (`vmlab.uihelpers`) over the Guest's Channels, so a Provider gets the UI contract for free once exec works. Only a Provider whose Guests need no helper overrides it, as the Fake Provider does.

The UI helpers are per OS, not per Provider: `src/vmlab/guest/<os>/` holds them and `src/vmlab/uihelpers.py` runs them. A helper takes `COMMAND JSON` and prints one JSON object; `src/vmlab/ui.py` does everything above that once (roles, node shape, matching, chords, waiting). Provisioning a Base guest must install the helper, so no Run compiles anything.

Every call into the hypervisor must be bounded: `start`, `stop`, `restore`, `copy_in` and `screenshot` take no timeout argument, so use the Lab's `step_timeout` (or `boot_timeout` for start and restore) and raise `GuestTimeout` when it runs out. A hung hypervisor must never hang a suite.

`guest_id` names the Guest uniquely per project and Lab. Use it for the hypervisor's VM name, so projects never share a Guest by accident.

Keep anything machine-specific or secret out of the project: keys, known_hosts, passwords and registries go under `vmlab.home.vmlab_home()` (mode 0700) or the Keychain.

## 2. Implement its Channels

Subclass `vmlab.providers.base.Channel`. Set a short `name` (it appears in reports and in `doctor`) and implement `exec(argv, timeout, env, stdin=None)`:

- Return an `ExecResult(argv, code, stdout, stderr)` whenever the command ran, **whatever its exit code**.
- Raise `ChannelError(message, fix)` when the Channel itself failed: it could not connect, lost the session, or its helper is missing. Only this makes vmlab try the next Channel.
- Raise `GuestTimeout(message)` after **killing** the call when it exceeds `timeout`. Never fall back on a timeout, because the command may have run. To kill Host-side helpers with their children, use `vmlab.hostproc.run`, which kills the whole process group.
- Pass `argv` without re-splitting it. Quote each element for the remote shell, so spaces and quotes arrive intact.
- Apply `env` to the command only, never to the Guest's global environment.
- Use a unique output file per call if the Channel captures output through files. Never use a shared one: concurrent calls would race.

Typical Channels: SSH with vmlab's own key and known_hosts (multiplexed; reuse `vmlab.providers.ssh.SshChannel`), the hypervisor's guest-exec (`tart exec`, `vmrun runProgramInGuest`), and SSH plus an interactive Scheduled Task on Windows. ADR 0003 has the defaults per OS.

## 3. Register it

Add it to `PROVIDERS` in `src/vmlab/providers/__init__.py`, replacing the stub if there is one. Its name is what Labs write as `provider = "<name>"`. Mention it in the README's config reference and in the config template in `src/vmlab/vendoring.py`.

## 4. Test it

vmlab has two seams (spec 0001, *Testing Decisions*):

- **Seam 1** (every commit): `python3 -m unittest discover -s tests` drives the built zipapp against the **Fake Provider**. It covers everything that does not depend on a real hypervisor: config, lifecycle, fallback, timeouts, deploy and reports. A new Provider rarely needs Seam 1 tests, except for its own config validation.
- **Seam 2** (manual or scheduled): `tests/contract/test_contract.py` checks the Provider/Channel contract against a **real** Lab through the CLI. It covers up, every Channel healthy, exec (stdout, stderr, exit code, env, quoting), timeouts, deploy (copy in and install), screenshots, every UI contract command (in the OS's stock text editor), restore and down. On macOS, run it a second time with `VMLAB_UI_HELPER=jxa` to cover the JXA fallback. With no settings it runs against a Fake Lab, so it stays green in Seam 1 too.

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

The Lab runs in a project of its own under `$VMLAB_HOME/contract/<lab>`, so its Guest is reused between runs and never touches your projects. Run the suite once per OS the Provider supports. A Provider is done when:

- Seam 2 is green for each supported OS.
- `vmlab doctor` gives a fix for every failure you met while getting there.
- Seam 1 stays green.
