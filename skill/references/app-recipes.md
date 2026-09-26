# App recipes

A Lab's `[labs.NAME.app]` tells vmlab how to get the app under test into its Guest. Every key is optional; `vmlab init`'s template comments each one.

| Key | Runs | When |
| --- | --- | --- |
| `build` (+ `inputs`, `build_timeout`) | Host shell, project root, with `VMLAB_LAB`, `VMLAB_OS`, `VMLAB_ARCH` | the `artifact` is missing or older than any of `inputs` |
| `artifact` | Host path or glob (newest match) | copied into a fresh Guest folder; its Guest path is `$VMLAB_ARTIFACT` |
| `install` (+ `install_timeout`) | Guest shell | once per suite, and again after every restore |
| `quit`, then `state` removed, then `launch` | Guest shell | before every Run |
| `quit` (+ `process`, `quit_timeout`) | Guest shell, then a wait until the app's process has gone | before every Run, and in `g.quit()` |
| `ready` (+ `ready_timeout`) | one `wait_for` condition on the Guest | after every `launch` |

The Guest shell is `sh` on macOS and Linux, and PowerShell on Windows (`$env:VMLAB_ARTIFACT`; `%VARS%` and a leading `~` work in `state`). TOML literal strings (`'...'`) hold shell quotes without escaping.

`ready` (optional) says when the launched app can be driven, so no Scenario has to wait for it: one condition with `wait_for`'s keywords, e.g. `ready = { process = "MyApp" }`, a tray app's Tray icon `ready = { tray = "MyApp" }` (the app as `g.tray` takes it: on Windows its process name; on every OS, without opening its menu), a log line `ready = { log = "~/.myapp/app.log", pattern = "listening" }`, or a command `ready = { exec = ["curl", "-fsS", "http://127.0.0.1:8080/health"] }`. `ready_timeout` defaults to the Lab's `step_timeout`. Unmet, the Run errors and `vmlab deploy` exits 1, naming the condition and its last answer.

`process` (optional) is the app's process name, as `wait_for(process=...)` takes it. After every `quit` recipe, vmlab waits until that process has gone, so `state` is removed only once the app stopped writing it (an app may save its settings on the way out), and `g.quit()` returns only then. Set it when `ready` is not `{ process = "X" }`: without it, `ready`'s process is the one waited for. `quit_timeout` (needs one of the two) defaults to the Lab's `step_timeout`. A process still there after it errors the Run, naming it; with a process to wait for, the quit recipe's exit code is not checked (before a Run it never is).

`notification_id` (optional) is the OS's id for the app as the sender of its Notifications, which `g.notifications` and `wait_for(notification=...)` default to: on macOS its bundle id, on Windows the AppUserModelID its installer gives its Start menu shortcut (a Tauri app's: its identifier), on Linux the app name it gives the notification server (often its binary's or its product name). A Lab has one OS, so each Lab states its own. Not sure of it: send one Notification and read `vmlab ui notifications`, which lists every app's with its `"app"`.

Work out the recipes from what the project already has: its build scripts, its packaging config (which bundle formats it makes, the app's name and bundle id), its CI workflow (the commands it runs per OS), and where the app keeps settings. Done when every agreed Lab has `artifact`, `install`, `quit`, `launch` and `state`, plus `build` unless the user builds by hand, and `ready` when the app has a natural readiness signal (a tray icon, a window, a port answering, a log line), each traced to something in the project or marked to the user as a guess. No such signal: leave `ready` out rather than invent one.

## Per Guest OS

| | macOS | Windows | Linux |
| --- | --- | --- | --- |
| artifact | `.app` (a folder; delivered whole), or `.dmg` | `.exe` (bare, or an installer), `.msi`, `.zip` | `.deb`, `.AppImage`, a folder or `.tar.gz` |
| install | `rm -rf /Applications/X.app && cp -R "$VMLAB_ARTIFACT" /Applications/`; a `.dmg`: `hdiutil attach -nobrowse`, copy, `hdiutil detach` | NSIS: `Start-Process -Wait $env:VMLAB_ARTIFACT /S`; MSI: `Start-Process -Wait msiexec "/i $env:VMLAB_ARTIFACT /qn"`; a bare `.exe` needs none | `sudo apt-get install -y "$VMLAB_ARTIFACT"` (installs the `.deb`'s dependencies too; sudo is passwordless); AppImage: `chmod +x` |
| quit | `pkill -x X` | `Stop-Process -Name X -ErrorAction SilentlyContinue` | `pkill -x x` (process names past 15 characters are cut: `pkill -f /path/x`) |
| launch | `open -a /Applications/X.app` (`open` does not pass the recipe's `env` on: when it matters, start `X.app/Contents/MacOS/X` in the background instead) | `Start-Process 'C:\Program Files\X\X.exe'` | `setsid /usr/bin/x >/dev/null 2>&1 &` |
| state | `~/Library/Application Support/<id>`, `~/Library/Caches/<id>`, `~/Library/Preferences/<id>.plist` (`quit` also runs `defaults delete <id>`: cfprefsd caches plists) | `%APPDATA%\X`, `%LOCALAPPDATA%\X` (a Tauri app's WebView2 data too: `%LOCALAPPDATA%\<identifier>`) | `~/.config/x`, `~/.local/share/x`, `~/.cache/x` |

Windows installers per user put the app under `%LOCALAPPDATA%\Programs\X` rather than `Program Files`: read the installer config. An app that needs a permission grant to work (macOS Accessibility, for one) gets it in `install`: see [traps-macos.md](traps-macos.md).

## Building for another OS on a Mac

The Host is a Mac, so the macOS Build artifact is a native build, and the others are cross-builds, a container build, a build inside the Guest, or CI's artifacts. The Build artifact's `arch` is the Host's unless the Lab says otherwise (x64 Windows builds run under emulation on Apple Silicon).

- **Tauri**: always build through the Tauri CLI (`npm run tauri build -- ...`): bare `cargo build` makes a dev binary that loads its frontend from the dev server, so its window opens blank. macOS: native (`--bundles app`). Windows: `--runner cargo-xwin --target aarch64-pc-windows-msvc` (or `x86_64-pc-windows-msvc`), which needs `cargo install cargo-xwin`, `rustup target add ...`, and LLVM (`brew install llvm`, keg-only: point `RC` at its `llvm-rc`, or the build fails with `NotAttempted("llvm-rc")`); `--no-bundle` gives the bare `.exe`, and an NSIS installer needs `brew install nsis`. Linux: WebKitGTK does not cross-compile from a Mac: build in a Linux container (Docker, an `ubuntu:22.04` image of the Guest's arch with `libwebkit2gtk-4.1-dev` and the Rust toolchain, the project mounted) or in the Guest.
- **Electron** (electron-builder, electron-forge): packages for every OS from a Mac, e.g. `npx electron-builder --win zip --arm64`, `--linux deb --arm64`. Native Node modules need prebuilt binaries for the target; without them, build in the Guest.
- **Qt** (C++): no practical cross-build from a Mac to Windows or Linux: build in the Guest, or use CI's artifacts.
- **.NET** (Avalonia, WinForms, WPF, MAUI): `dotnet publish -r win-arm64 --self-contained -p:PublishSingleFile=true` (or `linux-arm64`, `win-x64`) cross-publishes from a Mac; WinForms and WPF also need `-p:EnableWindowsTargeting=true`. A macOS `.app` needs a bundling step (Avalonia documents one).
- **Flutter**: desktop builds only on their own OS: build in the Guest.
- **Anything, from CI**: when CI already builds each OS, its artifacts can be the Build artifacts. Leave `build` out (source `inputs` cannot tell when CI's build is stale) and fetch before deploying: `gh run download RUN_ID --name NAME --dir DIR`, with `artifact` pointing into `DIR`.

Tell the user which Host tools a cross-build needs before installing any (they are host installs: ask first).

## Building inside the Guest

For stacks that do not cross-build, deliver the source and build in `install`:

```toml
[labs.linux.app]
artifact = ".vmlab/runs/source.tar.gz"
build = "mkdir -p .vmlab/runs && git ls-files -z -co --exclude-standard | tar czf .vmlab/runs/source.tar.gz --null -T -"
inputs = ["src", "src-tauri/src", "package.json"]   # the source that matters
install = '''
set -e
sudo apt-get install -y build-essential curl nodejs npm libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev
command -v cargo >/dev/null || [ -x "$HOME/.cargo/bin/cargo" ] || curl -sSf https://sh.rustup.rs | sh -s -- -y
. "$HOME/.cargo/env"
rm -rf ~/src && mkdir ~/src && tar xzf "$VMLAB_ARTIFACT" -C ~/src && cd ~/src
npm ci && npm run tauri build -- --bundles deb
sudo apt-get install -y ./src-tauri/target/release/bundle/deb/*.deb
'''
install_timeout = 3600
```

- The tarball holds tracked and untracked files but nothing git ignores, so `node_modules` and build output stay on the Host. It lives in `.vmlab/runs/`, which is git-ignored.
- Toolchains install into the Lab's clone, and a restore takes them away again, so the first `install` after a restore pays for them. Keep the toolchain steps idempotent (`apt-get install` of what is there is quick; `command -v ... ||`) so later installs skip them. Base guests are shared by all projects, so toolchains never go into them.
- Windows: the same shape in PowerShell; `tar` ships with Windows (`tar -xzf $env:VMLAB_ARTIFACT -C $HOME\src`), toolchains through `winget install --silent --accept-source-agreements --accept-package-agreements ...`.
