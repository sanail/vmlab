# vmlab

An agent skill plus a host CLI for testing desktop applications inside macOS, Windows and Linux **Guests**. Vocabulary: `CONTEXT.md`. Design: `docs/spec/0001-vmlab.md` and `docs/adr/`.

Status: CLI core, the Fake Provider, the Tart Provider (macOS Guests), the VMware Fusion Provider (Linux and Windows Guests) and the UI contract on macOS, Linux (X11 and Wayland) and Windows. UTM and Parallels are stubs. To add a hypervisor, see `docs/adding-a-provider.md`.

## Build and test

Python 3.9+ and the standard library only.

```sh
python3 tools/build.py                   # -> dist/vmlab.pyz, and the skill in dist/skill/vmlab/
python3 -m unittest discover -s tests    # Seam 1: drives the built zipapp as a subprocess; a few tests load one part
                                         # directly against stand-ins (the Linux UI helper, the Windows ssh Channel)
# Seam 2, the Provider/Channel contract against a real Lab (docs/adding-a-provider.md):
VMLAB_CONTRACT_LAB_FILE=my-lab.toml VMLAB_CONTRACT_LAB=mac python3 -m unittest discover -s tests -p 'test_contract.py' -v
```

## The skill

`skill/` holds the agent skill: `SKILL.md`, a short router, sends the agent to one workflow in `skill/references/` (setup, Ad-hoc run, regression), and those load on demand the Scenario API, the app recipes (build hooks per stack, cross-building from a Mac, building inside the Guest) and each Guest OS's traps. `tools/build.py` assembles it with the zipapp as `scripts/vmlab.pyz`.

### Install the skill

Build it (`python3 tools/build.py`), then copy `dist/skill/vmlab/` into a skills folder your agent reads: in your home for every project, or in a project for that project alone (commit it to share it). Agents that follow the open SKILL.md standard differ only in which folders they read:

| Agent | project | home |
| --- | --- | --- |
| Claude Code | `.claude/skills/` | `~/.claude/skills/` |
| Cursor | `.cursor/skills/`, `.agents/skills/`, `.claude/skills/` | `~/.cursor/skills/`, `~/.agents/skills/`, `~/.claude/skills/` |
| Codex | `.agents/skills/` (in the working folder and the repository root; never `.claude/skills/`) | `~/.agents/skills/` |
| OpenCode | `.opencode/skills/`, `.agents/skills/`, `.claude/skills/` | `~/.config/opencode/skills/`, `~/.agents/skills/`, `~/.claude/skills/` |
| Kilo Code | `.kilo/skills/` | `~/.kilo/skills/`, `~/.agents/skills/`, `~/.claude/skills/` |

In your home, `~/.agents/skills/` reaches every agent but Claude Code, and `~/.claude/skills/` every agent but Codex, so one copy and a symlink serve them all:

```sh
mkdir -p ~/.agents/skills ~/.claude/skills
cp -R dist/skill/vmlab ~/.agents/skills/
ln -s ../../.agents/skills/vmlab ~/.claude/skills/vmlab
```

In a project the same pair serves all but Kilo Code, which reads only `.kilo/skills/` there; add a third link for it:

```sh
mkdir -p .agents/skills .claude/skills .kilo/skills
cp -R PATH/TO/dist/skill/vmlab .agents/skills/
ln -s ../../.agents/skills/vmlab .claude/skills/vmlab
ln -s ../../.agents/skills/vmlab .kilo/skills/vmlab
```

An agent picks the skill up in a new session and uses it on its own when a request fits ("test my app on Windows"). To ask for it by name: `/vmlab` in Claude Code, Cursor and Kilo Code, `$vmlab` in Codex; in OpenCode, ask for the vmlab skill and its agent loads it with its `skill` tool. The skill is tested in Claude Code and Cursor; the other agents' folders come from their documentation.

To upgrade, copy the new build over the old folder, then run `python3 .vmlab/vmlab.pyz self-update` in each project that uses vmlab: the project keeps its pinned copy until you do.

## Project layout

`vmlab init` creates it (idempotent: an existing config, `run` or vendored copy is never touched, so running it again in an older project adds only what is missing):

```
.vmlab/
  vmlab.toml        # Labs; a commented template to start from
  vmlab.pyz         # the pinned CLI (ADR 0002): run it as `python3 .vmlab/vmlab.pyz ...`
  run               # runs the Regression suite: `.vmlab/run [NAME...] [--lab LAB]...`, the same as `python3 .vmlab/vmlab.pyz run ...`
  scenarios/*.py    # Scenarios
  .gitignore        # ignores runs/
  runs/             # per invocation and Lab: <UTC timestamp>-<lab>/ with report.json, junit.xml, summary.md, screenshots/<scenario>/
```

`vmlab self-update [--from PYZ]` replaces the vendored copy and prints `old -> new`. Run from a skill copy, it vendors itself; run from the vendored copy, it picks the newest `<skill>/scripts/vmlab.pyz` among the `.claude`, `.cursor`, `.agents`, `.codex`, `.opencode` and `.kilo` skill folders in the project and in `~`, and `~/.config/opencode/skills`. It refuses to downgrade.

```toml
[labs.mac]
provider = "fake"        # fake | tart (macOS) | fusion (Linux, Windows) (utm, parallels: stubs)
os = "macos"             # macos | windows | linux
arch = "arm64"           # arm64 | x86_64: the Build artifact's; defaults to the Host's (see "Architecture")
memory_gb = 4            # Host RAM the Guest takes; used by --parallel
boot_timeout = 300       # seconds from power-on until the Guest must be reachable
step_timeout = 60        # default seconds per Guest call
scenario_timeout = 600   # default seconds per Scenario

[labs.mac.app]                                   # all optional; see "Deploy"
artifact = "target/release/bundle/macos/MyApp.app"  # Host path or glob (newest match), relative to the project root
build = "npm run tauri build"                    # Host command, run in the project root when the artifact is stale
inputs = ["src", "src-tauri", "package.json"]    # stale = missing, or older than any of these
build_timeout = 1800
install = "rm -rf /Applications/MyApp.app && cp -R \"$VMLAB_ARTIFACT\" /Applications/"
install_timeout = 600
quit = "pkill -x MyApp"                          # exit code ignored before a Run; g.quit() checks it when there is no process
process = "MyApp"                                # optional; every quit waits for it to go (default: ready's process, if any)
quit_timeout = 30                                # seconds (default: step_timeout); needs a process
launch = "open -a /Applications/MyApp.app"
ready = { process = "MyApp" }                    # optional; one wait_for condition: launched means ready; needs launch (see "Deploy")
ready_timeout = 30                               # seconds (default: step_timeout); needs ready
env = { RUST_LOG = "debug" }                     # for install, quit and launch
state = ["~/Library/Application Support/MyApp"]  # Guest paths removed before every Run
notification_id = "com.example.myapp"           # the app as its Notifications' sender (see "UI contract")

[labs.mac.fake]
ui_tree = "tree.json"        # optional scripted UI tree, relative to .vmlab/
notifications = "notifications.json"  # optional scripted Notifications, read on every call
channels = ["ssh", "exec"]   # Channel names, preferred first
broken_channels = []         # fault injection: these Channels fail every call
hung_channels = []           # fault injection: these Channels hang until the call times out
boot_seconds = 0             # fault injection: a slow boot
```

Guests have no sound device on any Provider (Tart runs them with `--no-audio`, Fusion's have `sound.present = "FALSE"`): nothing a Guest plays reaches the Host's speakers or headset, and a Guest never takes a Bluetooth headset. There is no setting that gives a Guest sound, so a Scenario cannot test what an app sounds like.

## macOS Guests with Tart

Needs an Apple Silicon Mac with [Tart](https://tart.run) (`brew install openai/tools/tart`). A **Base guest** is created once per Host and shared by all projects; each Lab runs in its own APFS clone of it, which costs almost no disk:

```sh
vmlab base create macos-tahoe   # asks before downloading the image (tens of GB); --yes to allow it; idempotent
vmlab base list
```

`base create` clones a cirruslabs `*-base` image and provisions it: vmlab's SSH key, Remote Login, no sleep or screen saver, window and app restore off, the UI helper, TCC grants (Accessibility, Screen Recording, Input Monitoring, Apple Events to System Events and Finder) and Screen Recording approvals for both Channels. It then reboots the Guest from outside, checks `kern.boottime` changed, and proves each Channel reaches System Events and holds the UI helper's Accessibility grant. `--reprovision` runs it again; so does a newer vmlab whose provisioning changed.

**Guest-only security trade-off.** Granting automation permissions without MDM means writing TCC.db directly, which needs SIP off. The cirruslabs `*-base` images ship with SIP off. This affects only the throwaway Guest, never the Host.

```toml
[labs.mac]
provider = "tart"
os = "macos"
memory_gb = 4

[labs.mac.tart]
base = "macos-tahoe"         # the Base guest to clone
cpu = 4
display = "1920x1080"        # fixed, so coordinates stay stable between Runs
channels = ["ssh", "exec"]   # SSH (multiplexed), then `tart exec` (the Tart guest agent)
```

- Guests run headless, with no shared clipboard (it would overwrite what you copied) and no audio (a Guest's audio device can grab your Bluetooth headset).
- A clone lives until a restore or a re-provisioned Base guest replaces it (the old one is deleted first, so a Lab has at most one); `vmlab down` only stops it. It costs only what it changed relative to its Base guest.
- `vmlab clean` finds what no known Lab needs, on Tart and Fusion alike, using the record each clone keeps in `$VMLAB_HOME/tart/<clone>.json` (project, Lab, Base guest): clones whose project or Lab is gone (moved, renamed or deleted), clones of unknown origin, service files whose VM is gone, and Base guests no known Lab uses. It lists them and deletes after you confirm (`--yes` to skip asking). A running orphaned clone is stopped before it is deleted (no Lab's `vmlab down` can reach it, and a Windows Lab's copy stops only with the password vmlab keeps); a running Base guest is left alone, with how to stop it. VMs vmlab did not make are never touched; Base guests are deleted only with `--bases`, since re-creating one downloads its image. Run it inside a project to count that project's Labs as users of their Base guests even before their first clone. A hypervisor that is not installed is skipped (with a warning if vmlab has anything registered there).
- Clean state = delete the clone and clone the Base guest again. A clone is also made again on the next start after its Base guest is provisioned again (`--reprovision`, or a newer vmlab), so no Lab keeps running without what changed.
- Provisioning again changes the Base guest in place: Tart keeps no snapshots and nothing of the earlier provisioning, so there is nothing more to clean.
- SSH uses vmlab's own key and known_hosts in `$VMLAB_HOME/ssh/`. The Guest's host key is pinned at provisioning under the Base guest's name, never its IP, so reused DHCP addresses can't break or confuse it. Your `~/.ssh` is never read or written.
- Screenshots are taken in the Guest with `screencapture`, because Tart has no Host-side screenshot.
- The screen is `display` in points (Retina, 2x pixels). macOS would otherwise keep the display mode it last used whenever the display still offers it (the image pins 1024x768), so vmlab clears the saved mode each time it stops a Guest, and every boot fits the Lab's `display`.
- macOS runs at most two macOS VMs at once, so `--parallel` can't run a third.
- `VMLAB_TART` overrides the `tart` binary.

Provisioning also readies the desktop for UI work: loginwindow no longer relaunches the apps that ran at power-off (the image ships with Terminal), apps don't rewrite typed text (automatic capitalisation, spelling correction and smart punctuation are off), and the UI helper `vmlab-ui` (Swift: Accessibility + CGEvent) is compiled into `/usr/local/vmlab/bin`. Nothing is compiled at first use. Where the helper is missing (a Base guest provisioned by an older vmlab, or no Command Line Tools), UI commands fall back to a JXA script, which is slower, types through System Events (so it follows the Guest's keyboard layout), does not check what lies under a click, and does not wake lazy WebKit or Electron trees; `vmlab doctor` warns about it. Input events are paced a few milliseconds apart so the window server keeps their order; that is pacing, not waiting, which is always condition-plus-timeout.

macOS traps the helper handles, so Scenarios don't have to:

- **WebKit's accessibility tree is lazy** and goes to the *next* client that connects: a cold first read of a WebKit app (Tauri, Safari, WKWebView) shows no page content at all. The first time the helper meets an app process it reads it once to wake it, then reads again from a fresh process; after that even new windows come through on the first read.
- **AXManualAccessibility** turns the tree on in Chromium and Electron apps. WebKit rejects it (error -25205), so it is set best effort and the wake-up read above is what makes WebKit work.
- **Keyboard layouts**: chords are sent by physical key code (`cmd+c` copies on a Russian layout too) and text is typed as Unicode, independent of the layout.
- **Focus stealing**: staging text in another app and pressing the app's hotkey happen in one Guest call (`stage-text --then`), and the result records which app was frontmost when the chord went out.
- **Screen Recording consent**: even with the TCC grant, macOS 15+ alerts ("... is requesting to bypass the system private window picker") when a client captures the screen without the picker, at its first capture and again every 30 days, and the alert covers the app under test. Provisioning records a first use for vmlab's Channel clients and dates their next alert to 2100 (`ScreenCaptureApprovals.plist` in the Guest), part of the same Guest-only trade-off as the TCC grants. `vmlab doctor` takes a screenshot over each Channel and warns if the alert comes back.
- **Covered elements**: an element under the Dock, another window or scrolled out of view still has bounds. `click` asks Accessibility what lies under the point first and refuses with what it found there.

## Linux Guests with VMware Fusion

Needs a Mac with [VMware Fusion](https://www.vmware.com/products/desktop-hypervisor/workstation-and-fusion) (Fusion Pro, free: download it from Broadcom's support portal with a free Broadcom account; Homebrew no longer has a cask for it). As with Tart, a **Base guest** is created once per Host and each Lab runs in its own linked clone of it:

```sh
vmlab base create ubuntu-26.04   # asks before downloading the Ubuntu desktop ISO (about 4 GB); --yes to allow it; idempotent
vmlab base create ubuntu-26.04 --image ~/Downloads/ubuntu-26.04.1-desktop-arm64.iso   # or use an ISO you have
```

`base create` downloads the ISO for the Host's architecture into `$VMLAB_HOME/images/` (checked against Ubuntu's published SHA-256), builds a VM in `$VMLAB_HOME/fusion/` and installs Ubuntu without a single click (about 10 minutes, most of it installing updates). Then it provisions the Guest over SSH, reboots it into the desktop, proves both Channels reach the desktop session and takes the snapshot that Lab clones link to. Each stage is skipped when it already finished, so re-running after a failure resumes; `--reprovision` provisions again.

How the unattended install works, for when it needs debugging:

- A small `CIDATA` disc next to the ISO carries cloud-init user-data: the autoinstall answers (user `vmlab` with a random password, vmlab's SSH key, a host key vmlab generated and pinned beforehand, passwordless sudo) and a job for the installer's live session.
- The Ubuntu installer wants a click before it writes the disk unless `autoinstall` is on the kernel command line, and editing that means typing into GRUB. Instead, the job waits for the installer to ask and confirms over the installer's own local API, as the Install button does.
- The job reports the installer's state on the VM's serial port, which Fusion writes to `install-serial.log` in the VM's folder; `base create` prints the state as it changes. The installer powers the VM off when it is done.

Provisioning (`src/vmlab/guest/linux/provision.sh`, idempotent, about a minute) prepares desktop sessions that automation can drive: GDM logs the user in automatically; screen lock, blanking and suspend are off (an idle GNOME suspends the Guest, VMware Tools included, even at the login screen); the accessibility bus is on (for Qt apps too); the welcome wizard (which also returns as a "what's new" tour after release upgrades), the update notifier, the crash reporter and background apt updates are off, since they pop up over the app under test or hold the package lock during Runs. For the UI contract it installs AT-SPI (`python3-pyatspi`), vmlab's GNOME Shell extension (below), vmlab's Notification recorder (a systemd user unit, `vmlab-notifications.service`), and for X11 an Xfce session with `xdotool`, `python3-xlib`, `xclip` and `wmctrl`: GNOME 50 no longer has an X11 session. The default session is GNOME on Wayland.

```toml
[labs.linux]
provider = "fusion"
os = "linux"
memory_gb = 4

[labs.linux.fusion]
base = "ubuntu-26.04"          # the Base guest to clone
cpu = 4
channels = ["ssh", "vmrun"]    # SSH (multiplexed), then vmrun guest operations through VMware Tools
session = "wayland"            # GNOME on Wayland; "x11": Xfce on X11. Testing both means two Labs
```

- Clean state is the clone's `vmlab-clean` snapshot, taken when the clone is made: restoring reverts to it. A clone is made again on the next start after its Base guest is provisioned again, or after the Lab's `session` changes.
- A Lab with `session = "x11"` boots its new clone once to make Xfce its autologin session (in AccountsService, where GDM looks) before `vmlab-clean` is taken, so restores keep the session. Every start checks that the Guest logged into the session its Lab asks for.
- Commands (`g.exec()`, the app recipes) run with the desktop session's environment (`DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, `XDG_SESSION_TYPE`, ...), taken from the user's systemd manager, into which the session imports it; values the command sets itself win. So `launch = "setsid /opt/myapp/myapp >/dev/null 2>&1 &"` opens the app on the Guest's screen. `systemd-run --user --collect /opt/myapp/myapp` works too, and keeps the app out of the Channel's session.
- Two vmlab processes never share a Guest: `run` and `deploy` hold a lock on it (any Provider), and a second one fails at once, naming the process that holds it.
- The Guest's IP is looked up at every boot, never hardcoded: from what VMware Tools publish (`guestinfo.ip`), else `vmrun getGuestIPAddress` (which can claim for minutes that Tools are not running when they are), else Fusion's DHCP lease for the VM's MAC. SSH works as with Tart: vmlab's key and known_hosts, the host key pinned under the Base guest's name.
- The vmrun Channel needs no network and no sshd, but it takes several vmrun calls per command (about 3 s, against about 12 ms over SSH). Each call writes its output to files named for that call alone, so concurrent calls never mix, and a call that times out is killed in the Guest too. vmrun takes the Guest password on its command line, so it is visible in the Host's process list while a call runs; the password is random, stored in `$VMLAB_HOME/fusion/` (mode 0600), and guards a throwaway Guest on Fusion's private NAT network. Nothing in the Guest ever needs it: sudo is passwordless.
- Screenshots are Host-side (`vmcli MKS captureScreenshot`): no Guest credentials, and no Wayland consent dialog.
- Guests run without a window and without a sound device (`sound.present = "FALSE"`, set again at every start, since reverting to a snapshot brings back the settings it was taken with), so nothing a Guest plays reaches your speakers or headset. `VMLAB_VMRUN` and `VMLAB_VMCLI` override the Fusion binaries.
- A Base guest runs only while `base create` provisions it: Labs cannot clone (or copy) a running one. When one runs, `vmlab doctor` warns and `vmlab up` refuses to clone it, and both give the fix, `vmlab base create NAME`, which shuts a running Base guest down over vmrun, with the VM's password for a Windows one: no window, no Start menu.
- `vmlab clean` treats Fusion leftovers as it treats Tart's (see above), using the clone records in `$VMLAB_HOME/fusion/`; deleting an unused Base guest also deletes its Guest credentials.
- Each provisioning takes a new `vmlab-provisioned-<id>` snapshot of the Base guest. An earlier one is kept only while a Lab's linked clone was made from it (per its clone record; for a VM vmlab has no record of, per the parent disk its own disk names, or else the linked clones Fusion lists in the Base guest's `.vmsd`), until that Lab is cloned again: `vmlab up LAB` (after `vmlab down LAB` if it runs). `base create` then offers to delete the others and names the Labs that keep the rest. Without a terminal it deletes nothing and says so; `--yes` answers yes, and `VMLAB_DELETE_OLD_SNAPSHOTS=1` in your environment turns the question off for good. `base create` shuts a running Base guest down first; `vmlab clean` leaves a running one alone and deletes them too, and `vmlab doctor` warns about them. Deleting a snapshot merges its changes into the next one, which takes minutes for a large one. A Windows Base guest's are deleted by hand: Fusion's command-line tools (vmrun, vmcli) only drop an encrypted VM's snapshot from its list and never merge its disks, so nothing is freed, while Fusion's own Snapshots window does merge them. `base create`, `vmlab clean` and `vmlab doctor` name them and give the steps in Fusion. Each earlier provisioning keeps a few hundred MB there; a Windows Lab's copy (an APFS clone) carries its own copy of them until it is copied again after the next provisioning, so delete them in Fusion before the Labs' next start.

The UI helper (`src/vmlab/guest/linux/vmlab-ui.py`) is plain Python sent with every call, so it never lags behind vmlab. It reads the tree through AT-SPI in both sessions, and asks logind at every call which session is logged in. In X11 it asks the window manager where windows are and sends input through XTEST (`xdotool`, which also types characters the keyboard layout lacks). On Wayland no client may learn where windows are, move the pointer to a point or raise another app, and GNOME Shell's own Introspect interface refuses unknown callers, so vmlab's GNOME Shell extension (`src/vmlab/guest/linux/shell-extension/`, only in the Guest) does it: window geometry and focus, a virtual pointer and keyboard, the clipboard. The Linux traps it handles:

- **Coordinates**: GTK 4 reports every element at (0, 0) in screen coordinates, in both sessions, and GTK 3 on Wayland reports them relative to its window, shadow included. So elements are read relative to their window, and the window's place comes from the window manager: whichever of its rectangles (with or without the client-side shadow) has the size the toolkit reports.
- **Pointer on Wayland**: `ydotool` moves the pointer relatively, through pointer acceleration: asked for (100, 100) it landed at (200, 200). The extension's virtual pointer moves to the exact point.
- **Typing on Wayland**: key events can only type what the keyboard layout has, so text goes to Wayland apps through the input method, as from the on-screen keyboard: any character, on any layout. Apps under Xwayland get key events instead. Chords are always key events, by physical key.
- **Hidden widgets**: a background tab or a hidden window stays in the AT-SPI tree; elements without the visible state are left out.
- **A busy editor** (the stock text editor on a loaded Guest): a select-all can land before a newly opened document has focus or its text, and a close that comes before the save has finished asks about saving. `stage-text` presses select-all again until the document's own view, the one on screen, has the text selected; `close-staged` closes once the file on disk holds what the view shows (or after 10 s, for an editor that changes the text as it saves).
- **Rebooting a Guest with unsaved documents**: an editor's inhibitor blocks `systemctl reboot` in the Guest. vmlab never reboots from inside: it stops the Guest from the Host (powering it off if it does not shut down in time) and starts it again.
- **WebKitGTK** paints a window once and then never again with its DMA-BUF renderer on the Guest's software GL (Fusion passes no 3D to arm64 Linux), so screenshots freeze on the first frame. The session sets `WEBKIT_DISABLE_DMABUF_RENDERER=1`.

## Windows Guests with VMware Fusion

Windows comes from Fusion's own "Get Windows from Microsoft" flow, which only a person can click through, so `vmlab base create windows-11` is a wizard: it says what to do, checks each step before the next, and asks again until it holds. Re-running it continues where it stopped.

```sh
vmlab base create windows-11                       # walks you through Fusion's flow, then adopts your VM
vmlab base create windows-11 --image ~/VMs/Win11.vmwarevm   # or name the VM to adopt
```

What the wizard asks of you: make the VM in Fusion (Windows 11 in English (United States), "only the files needed to support a TPM are encrypted", the password kept in your Keychain, a local administrator account with a password, then the Mac's time zone, since Windows reads Fusion's clock, the Mac's local time, in its own zone, then VMware Tools, which Fusion's flow leaves out), then one click. Before provisioning it compares the Guest's clock (in UTC) with the Mac's, and while they are more than two minutes apart it names the Mac's time zone and where to set it in Windows. What it does itself: find the VM, take its encryption password from your Keychain, copy it into `$VMLAB_HOME/fusion/` (an APFS clone of its files: instant, and it shares the original's disk space, so the original stays yours and untouched), provision the copy, reboot it, prove both Channels reach the desktop and snapshot it for Lab clones.

**The one click.** vmrun runs programs as the Guest user with a filtered token, so nothing vmlab starts can administer Windows, and Windows' consent prompt is drawn on the secure desktop, where keys sent from the Host do not reach. So the wizard raises that prompt once, in a Fusion window it opens for the purpose, and asks you to click Yes; from then on administrators elevate without a prompt in this throwaway Guest, and provisioning runs headless. (Fusion refuses to start an encrypted VM with a window unless the password is in the Keychain, so vmlab puts the copy's there, as Fusion does for VMs you make.)

Provisioning (`src/vmlab/guest/windows/provision.ps1`, idempotent) installs the OpenSSH server and vmlab's key (as `administrators_authorized_keys`, which sshd reads for administrators), turns on autologin (the password as an LSA secret, not the registry value every user can read), turns off Windows Update, sleep, the screen saver, the lock screen, the "finish setting up your device" pages and OneDrive (by policy: it starts at logon and a few minutes later shows "Turn On Windows Backup" over the app under test; Scenarios cannot use OneDrive), and makes UTF-8 the code page of every program, so output arrives as text on the Host. It also compiles PowerShell's .NET assemblies to native code (NGEN). A new Windows has not done that yet, and a Guest restored to its Clean state loses what Windows does in idle time; without it every call, which starts PowerShell two or three times, takes seconds longer.

```toml
[labs.win]
provider = "fusion"
os = "windows"
memory_gb = 4

[labs.win.fusion]
base = "windows-11"            # the Base guest to copy
cpu = 4
channels = ["ssh", "vmrun"]    # SSH to vmlab's call server in the desktop session, then vmrun -interactive
language = "en-US"             # the display language the Lab's Scenarios expect
```

- **Display language**: element names (buttons, menus, window titles) come in the Guest's display language, so a Scenario that finds "Close" works only on an English Windows. Make the Base guest from English (United States) Windows; provisioning records the language it finds, and `vmlab doctor` fails a Lab whose Base guest (or running Guest) shows a language other than its `language`. To replace a Base guest's Windows, make a new VM in Fusion and adopt it with `vmlab base create windows-11 --image PATH`: Labs copy the new one at their next start.

- **Clock**: Windows reads Fusion's hardware clock, the Mac's local time, in its own time zone, so a Guest in another zone than the Mac's (Windows Setup in English (United States) picks Pacific Time) runs hours off. The wizard stops until the Guest's time zone is the Mac's. `vmlab doctor` compares a running Windows Guest's clock with the Host's and warns when they are more than two minutes apart: a wrong time zone is set in the Base guest (`vmlab base create windows-11 --reprovision` waits while you do it; Labs copy it again at their next start), and a Guest in the Mac's zone that is still off reads the Mac's time again when it restarts. Changing the Mac's own time zone later puts every Windows Guest off the same way. Fusion gives Linux Guests UTC as their hardware clock, which Linux reads as UTC, and Tart's macOS Guests boot with the Host's UTC time too, so only Windows Guests' clocks are checked.
- **No sound**: Fusion's Windows VM has a sound device, so Windows would play its sign-in chime and notification sounds, and the app's own, through your speakers, and could take your Bluetooth headset. The wizard turns it off in the copy it adopts, `vmlab base create windows-11` turns it off in a Base guest made by an older vmlab (shutting it down first if it runs), and every Lab starts without one. Windows then shows a crossed-out speaker in the tray and nothing else; `vmlab doctor` warns about any of vmlab's Fusion VMs (Base guest or Lab, of any project) that still has one, with the fix.
- A Lab's Guest is an **APFS copy** of the Base guest, not a linked clone: vmrun cannot clone an encrypted VM ("Cannot read the virtual machine configuration file"). The copy is instant, shares the original's blocks and gets its own UUID and MAC address. Clean state is its `vmlab-clean` snapshot, as on Linux.
- Every vmrun call carries the VM's encryption password (`-vp`) and the Guest user's (`-gu`/`-gp`), so both are visible in the Host's process list while a call runs; they live in `$VMLAB_HOME/fusion/` (mode 0600) and guard a throwaway Guest. Screenshots go through `vmcli`, which takes the password on stdin instead.
- Both Channels run a call the same way: in the logged-in user's desktop session, unelevated, in the user's profile folder, and its exit code and raw output come back whole. So a fallback never changes what a command sees. An SSH session cannot reach the desktop, so the ssh Channel talks to vmlab's call server there (`src/vmlab/guest/windows/call-server.ps1`): an interactive Scheduled Task starts it at the first call after every boot or restore, and each call is a connection forwarded to it over the multiplexed SSH connection (`ssh -W`). A no-op then takes about 40 ms, a PowerShell command about 0.25 s (PowerShell's own start); vmrun, which starts a script per call, about 3 s. vmrun needs neither network nor sshd, which is why provisioning uses it. ADR 0003 has the numbers and the traps behind them.
- Windows' sshd stops reading a command's stdin at about 4 KB and the call hangs, so nothing rides on a command's stdin: a call's stdin travels over the call server's forwarded connection, Build artifacts as files over scp. While a Guest boots, `vmlab up` waits until **every** Channel answers, because sshd is up before Windows finishes signing the user in; a Guest that is already running needs one.

The UI helper (`src/vmlab/guest/windows/vmlab-ui.ps1`) is sent with every call, like Linux's, and runs in the logged-in user's session, as every Windows Channel's calls do: UI Automation for the tree, `SendInput` for the pointer and keys. Its work is C# inside the script, which PowerShell would compile at every call (seconds), so the Guest keeps a compiled copy in `C:\ProgramData\vmlab\ui`, named by a hash of the source; provisioning compiles it into the Base guest's snapshot, and a Guest without a copy for this vmlab (a Base guest provisioned by an older one) compiles one on its first UI call after every start or restore (about 10 s; that call gets a minute for it, whatever the Scenario has left). `vmlab base create windows-11 --reprovision` bakes the new one in. A UI call takes about 0.4 s over SSH. App names are process names, as `Get-Process` shows them (`Notepad`, `explorer`); minimized windows are left out of the tree. The taskbar and the notification area are `explorer`'s: their buttons are in the tree by name (`find --role button --text MyApp --app explorer`), so a Check never needs to crop a screenshot at fixed coordinates. The Windows traps it handles:

- **Focus**: a process that is not in the foreground may not bring a window forward; `SetForegroundWindow` just returns false. The helper joins the foreground window's input queue (`AttachThreadInput`), where the switch is allowed, and checks that it happened. No Alt tap, the usual other trick: in an app with a menu bar Alt enters the menu, and the next chord goes there. Calls run in a console without a window (`conhost --headless`), so a call never takes the focus from the app under test.
- **Scaling**: a Windows 11 Guest in Fusion on a Retina Mac runs at 200 %. The helper is per-monitor DPI aware, so the tree, clicks and screenshots all use physical pixels; an unaware process gets some answers scaled and others not.
- **Keyboard layouts**: chords are sent as virtual keys, which keep their meaning on any layout (checked with Ctrl+A and Ctrl+C under a Russian layout); punctuation keys (`slash`, `semicolon`, ...) are the US layout's. Text is sent as Unicode characters, which need no layout at all.
- **Typing pace**: Notepad (WinUI) dropped characters sent 5 ms apart now and then (one call in three); the helper waits 20 ms between characters.
- Text areas are `Document` elements in WinUI and rich edits, and so are web pages: an editable one gets the role `textarea`, and its text comes through its text pattern.

`g.spawn` in a Windows Guest goes through `src/vmlab/guest/windows/vmlab-spawn.ps1`, sent with every call like the UI helper. It starts the program itself (so `handle.pid` is the program's) in a Job Object that holds everything it starts, and `stop()` ends that job: children whose parent has exited are ended too, which a walk of parent pids (`taskkill /T`) misses. Its C# is compiled into `C:\ProgramData\vmlab\spawn` on the first spawn after a start or restore (about 1 s; no provisioning needed); a spawn then takes about 0.5 s.

A Scenario is a Python file defining `scenario(g)`:

```python
FRESH = True     # optional: restore Clean state before this Scenario
LAUNCH = False   # optional: don't launch the app before this Scenario; call g.launch(env={...}) yourself (it waits for app.ready)
                 # g.quit(env={...}) quits it again and waits for its process to go: g.quit() then g.launch() restarts it
TIMEOUT = 120    # optional: seconds for the whole Scenario (default: the Lab's scenario_timeout)

def scenario(g):
    r = g.exec(["echo", "hello"])          # r.code, r.stdout, r.stderr, r.ok, r.channel; timeout=, env=
    g.check("echo prints hello", r.stdout.strip() == "hello", detail=r.stderr)
    g.put("~/input.txt", "data\n")         # str (UTF-8) or bytes; makes folders; returns the absolute Guest path
    g.check("read back", g.get("~/input.txt") == "data\n")  # binary=True for bytes; a missing file raises
    mock = g.spawn(["python3", "-u", "-m", "http.server", "8080"])  # detached (env= adds to its environment); stopped with the Run
    g.wait_for(log=mock.log, pattern="Serving HTTP")  # mock.log: its stdout+stderr; mock.output(), .running(), .stop(), .pid
    g.screenshot("after echo")             # saved in the Run folder as evidence
    g.check("icon looks right", True, visual=True)  # a judgement from a screenshot: reported "visual, unverified"
    g.skip("tray icon shows", "no tray on this Guest OS")  # a Skipped Check: reason required; neither passes nor fails
    # g.lab, g.os, g.arch (the Build artifact's) and g.guest_arch (the Guest's: the Host's)
```

## UI contract

Scenarios and the agent read and drive the Guest's UI with the same commands and JSON on every OS (macOS, Linux and Windows). `vmlab ui COMMAND [--lab LAB]` prints JSON; each Scenario method returns the same object:

| CLI | Scenario | Result |
| --- | --- | --- |
| `ui tree [--app APP]` | `g.tree(app=None)` | the node tree below |
| `ui find [--text T] [--role R] [--app APP]` | `g.find(text=, role=, app=)` | `{"matches": [node without children, plus "app"]}` |
| `ui click [--text/--role/--app] [--index N] [--timeout S]`, `ui click --at X Y` | `g.click(..., index=0, timeout=None)`, `g.click(at=(x, y))` | `{"x", "y", "element", "under"}` |
| `ui press CHORD` | `g.press("cmd+shift+space")` | `{"chord": "shift+cmd+space"}` |
| `ui type TEXT` | `g.type(text)` | `{"typed": n}` |
| `ui focus --app APP [--window TITLE]` | `g.focus(app, window=None)` | `{"app", "window", "frontmost"}` |
| `ui clipboard [--set TEXT]` | `g.clipboard()`, `g.set_clipboard(text)` | `{"text"}` |
| `ui stage-text TEXT [--app APP] [--then CHORD]` | `g.stage_text(text, app=None, then=None)` | `{"app", "file", "frontmost", "selected", "pressed"}` |
| `ui close-staged --file PATH [--app APP]` | `g.close_staged(staged)` | `{"file", "closed"}` |
| `ui tray --app APP [--choose LABEL]... [--timeout S]` | `g.tray(app, choose=None, timeout=None)` | `{"items": [{"name", "enabled", "checked", "children"}], "chosen"}` |
| `ui notifications [--app ID] [--text PATTERN] [--since TIME]` | `g.notifications(app=None, text=None, since=None)` | `{"notifications": [{"app", "title", "body", "time"}]}` |
| `ui wait-for CONDITION [--gone] [--timeout S]` | `g.wait_for(..., gone=False, timeout=None)` | `{"met", "waited_s", "condition"[, "matches"][, "code", "stdout"][, "notifications"][, "detail"][, "error"]}` |
| `ui screenshot [--out PATH]` | `g.screenshot(name)` | `{"path"}` (Scenarios: the path in the Run folder) |

Every node has `role` (cross-OS: `application`, `window`, `button`, `textfield`, `textarea`, `text`, `checkbox`, `menuitem`, ...), `name`, `value`, `description`, `bounds` (`{"x", "y", "w", "h"}` in screen points, pixels on Windows, or null), `focused`, `enabled`, `native_role` (e.g. `AXButton`) and `children`. The root is the `desktop`, with `truncated` true when a size limit cut the tree short; its children are applications (with `pid`), holding their windows and, on macOS, their Tray icons (`menubaritem`).

- `find` matches `--text` against name, value and description: exact matches win, otherwise substrings. `--role` takes the cross-OS or the native role.
- `click` clicks the middle of the first match (or the `--index`th) with a real mouse event, after checking the element is what lies under that point. Without `--timeout` it fails at once when nothing matches or something covers the element; with it, it tries again until the element is there and uncovered, then fails with the last reason. `--at` ignores `--timeout`.
- Chords are `+`-joined modifiers (`ctrl`, `alt`/`option`, `shift`, `cmd`/`command`/`win`/`super`) and one key: `a`-`z`, `0`-`9`, `f1`-`f12`, `space`, `enter`, `tab`, `escape`, `backspace`, `delete`, arrows, `home`, `end`, `pageup`, `pagedown` and punctuation names (`minus`, `comma`, `slash`, ...). An unknown key is a usage error (exit 2).
- `focus` brings a running app to the front and waits until it is frontmost; `--window` first raises its first window whose title contains TITLE. An app that is not running, or a window that is not there, is an error naming what is.
- `stage-text` opens the text in a third-party editor (default: TextEdit, Notepad or GNOME Text Editor, `gnome-text-editor`), selects it all, and presses `--then` in the same Guest call, so nothing can steal focus in between. It closes nothing: its `"file"` is the Staged document, which `close-staged` saves and closes (`g.close_staged` takes the `stage_text` result or that path), leaving the editor's other documents, and the one in front, as they were; `"closed"` is false when it was no longer open. A path that is not a file `stage-text` wrote to the Guest's temp folder is refused. In a Scenario, what it leaves open is closed at the end of its Run.
- `tray` reads an app's Tray menu: its items in order, without separators, each with `children` (a submenu's items, on every OS; `[]` for an item without a submenu). `--choose` chooses an item, one label per menu level (`--choose Settings --choose Advanced`; `g.tray(app, choose=["Settings", "Advanced"])`, or `choose="Quit"`), matched exactly without mnemonics, and `"chosen"` echoes the path; without it nothing is chosen and nothing is left open. The app has no Tray icon, no item has a label at its level, the item is disabled, or it opens a submenu (`"Settings" opens a submenu; choose one of its items: Advanced, Dark mode`): it exits 1 (a Scenario gets an error) naming what, and the CLI still prints the items with `"error"`. `--timeout` waits for the Tray icon to appear, as tray apps put it up after their launch; `wait-for --tray` waits for it alone. `--app` is the app as the OS names it for the other commands (on Linux also its process's name or its StatusNotifierItem's Id). macOS presses the item in the Tray menu without opening it; Windows opens the menu with a right click on the Tray icon, first moving an icon waiting among the hidden ones onto the taskbar (it stays there until the next restore); Linux chooses over D-Bus, from the menu the panel draws.
- `notifications` lists the Notifications the Guest's OS recorded, oldest first, shown on screen or not: macOS's from the Notification Center's store (usernoted's SQLite database), Windows' from the Action Center's (`wpndatabase.db`, read through the `winsqlite3.dll` Windows ships), Linux's from the file vmlab's recorder writes (Linux keeps no history: provisioning installs a systemd user unit that monitors `org.freedesktop.Notifications.Notify` on the session bus in both Desktop sessions; `vmlab doctor` warns when a Guest has none). `--app` is the OS's id for the sender (macOS bundle id, Windows AppUserModelID, Linux the app name given to `Notify`), by default the Lab's `app.notification_id`; with neither, every app's come back, each with its `"app"`. `--text` is a Python regex searched for in title and body. `time` is ISO 8601 UTC on the Guest's clock, and so is `--since`; a Scenario's `g.notifications()` counts only those after the Run's start (`since="all"`: all). Windows keeps Notifications only for an app with an AppUserModelID, which its installer gives it: a bare `.exe` gets an empty list.
- `wait-for` takes exactly one condition: an element (`--text`/`--role`, narrowed to one app by `--app`), `--process NAME`, `--file PATH`, `--log PATH --pattern REGEX`, a Notification (`--notification REGEX`, posted after `--since` or the call's start; `--app` as for `notifications`; `g.wait_for(notification=...)` counts those after the Run's start), an app's Tray icon (`--tray APP`, the app as `tray --app` takes it; `g.wait_for(tray=...)`: met once `tray` would find the icon, without opening, reading or choosing from its menu, so on Windows no click and no change of the foreground window, only the move of a hidden icon onto the taskbar; where the Guest shows no Tray icons at all, as a Linux Desktop session without a StatusNotifierWatcher, it is never met, `--gone` or not, and the result's `"detail"` says why), or a command, `--exec ARG...` (last, since everything after it is the command; `g.wait_for(exec=[...])`): it exits 0, or with `--pattern` its stdout matches whatever the exit code, and the result carries the last answer's `code` and the tail of its `stdout`. `--gone` inverts any condition but a Notification. It polls until the condition holds or the timeout (default: the Lab's `step_timeout`) passes, never with fixed sleeps. A poll that gets no answer from the Guest (a failed Channel, a hung call) is "not met yet", `--gone` or not, with `"error"` saying why; a command the Guest does not have (exit 127 or 9009, PowerShell's CommandNotFoundException) fails at once, so wait for an installed command's file first. `--process` matches the name exactly; on Linux, a name longer than the 15 bytes the kernel keeps is confirmed against the command line. Unmet, the CLI exits 1 and a Scenario gets `"met": false` to check.
- UI commands need a running Guest (`vmlab up` or `vmlab deploy`). In a Scenario they count against its timeout like `g.exec`.

```python
def scenario(g):
    staged = g.stage_text("Ohm's law relates voltage", then="cmd+shift+space")
    g.check("hotkey went to the editor", staged["frontmost"] == staged["app"], detail=staged)
    palette = g.wait_for(text="Selection", app="MyApp", timeout=10)
    g.check("palette read the selection", palette["met"], detail=palette)
```

Commands reach the Guest over its first working Channel (ADR 0003). When a Channel fails, the call falls back to the next Channel and the report notes it. A non-zero exit code is returned to the Scenario and never triggers a fallback. A call that exceeds its timeout is killed and fails the Run with the Scenario file and line; so does a call no Channel can carry.

## Deploy

Before Labs start, each Lab's build hook runs on the Host (with `VMLAB_LAB`, `VMLAB_OS`, `VMLAB_ARCH`) if its Build artifact is missing or older than one of its `inputs`; Labs sharing an artifact build it once. Its output lands in the Run folder as `build.log`. The artifact is then copied into a uniquely named Guest folder under `~/vmlab/artifacts/` and `install` runs. That happens once per suite, and again after any restore. Before every Run, `quit` runs, vmlab waits for the app's process to go (`process`, or `ready`'s process when `ready = { process = "X" }`; up to `quit_timeout`, default the Lab's `step_timeout`), the `state` paths are removed and `launch` runs. A Scenario's `g.quit()` does the same on its clock; with no process to wait for, it raises when `quit` exits non-zero instead. `quit_timeout` without a process to wait for is a config error. Guest recipes run in `sh` (PowerShell on Windows) with `env` plus `VMLAB_LAB`, `VMLAB_OS`, `VMLAB_ARCH` and `VMLAB_ARTIFACT`, the Guest path of the delivered copy.

An app is launched once it is ready, not when `launch` returns: `ready` takes one `wait_for` condition, in its keywords (`text`/`role`/`app`, `process`, `file`, `log` + `pattern`, `exec` + `pattern`, `tray`, `gone`; see the UI contract), e.g. `ready = { tray = "MyApp" }` for a tray app's icon or `ready = { exec = ["curl", "-fsS", "http://127.0.0.1:8080/health"] }`. vmlab waits up to `ready_timeout` seconds (default: the Lab's `step_timeout`) after every launch: before a Run, off the Scenario's clock; in `g.launch()`, on it; and in `vmlab deploy`. Unmet, the Run errors (it is not a failed Check), or `deploy` exits 1, naming the condition and the last poll's answer (its first 1000 characters). When a Scenario's `TIMEOUT` runs out first in `g.launch()`, its timeout error names them too. A malformed `ready` is a config error naming its TOML key (`labs.LAB.app.ready.pattern`), for the same mistakes `wait_for` rejects; so are `ready` without `launch` and `ready_timeout` without `ready`.

`vmlab deploy [LAB...]` does the same without Scenarios and leaves the Guests running, for exploring by hand. It deploys the Labs one after another and stops at the first that fails (a build, install or recipe error, or an unmet `ready`): the Labs after it are not deployed, so fix that one and deploy again, or name the others. The Guests it started for the Labs before it (and for the failed one, if its Guest came up) stay running, and it names them with the command that stops them.

**Step lines.** `deploy` and `run` take minutes, so both print each step on stdout as it starts, prefixed with its Lab, and its time when it ends: a hung step is the last one with no `done` line, and the error follows a failed one.

```
mac: building (log: .vmlab/runs/20260926T140717Z-mac/build.log)
mac: building done in 12s
mac: cloning
mac: cloning done in 3s
mac: booting
mac: booting done in 1s
mac: waiting for Channels
mac: waiting for Channels done in 41s
mac: delivering
mac: delivering done in 2s
mac: installing
mac: installing done in 5s
mac: quitting
mac: quitting done in 1s
mac: launching
mac: launching done in 0s
mac: waiting for ready {"process": "MyApp"}
mac: waiting for ready done in 2m05s
```

Only the steps that happen print: no building for a fresh Build artifact (and no log line outside `run`), no quitting without a `quit` recipe, no cloning for an existing clone (a Tart restore of Clean state clones afresh; a Fusion clone made from an earlier provisioning is first deleted, `deleting the old clone`), no booting nor waiting for Channels for a running Guest. `run` also prints `restoring Clean state` and one `scenario NAME` line as each Scenario starts (its result is in the Lab's summary); launching before a Run and a Scenario's own steps print nothing. The lines print without a TTY and are flushed at once; under `--parallel` every line is whole and carries its Lab. `--quiet` on `run` and `deploy` prints none of them, only the results.

## Lifecycle

- A **Regression suite** (`vmlab run` or `.vmlab/run`, with saved Scenario names or none for all) restores Clean state once per Lab at its start, and before each Scenario that declares `FRESH = True`.
- An **Ad-hoc run** (`vmlab run path/to/scenario.py` outside `scenarios/`) keeps Guest state for fast iteration and leaves its Guests running, printing the stop command.
- `--fresh` restores before every Scenario; `--keep` leaves Guests running.
- Before every Run, the Lab's `app.state` paths are removed.
- Labs run one after another. `--parallel` runs them concurrently, starting a Lab only while its `memory_gb` fits in free Host memory (free + inactive pages; override with `VMLAB_FREE_MEMORY_GB`) and queueing the rest. A Guest that is already running needs no memory, and a Lab larger than all free memory runs alone. Each Lab keeps its own Run folder and reports.
- `--repeat N` runs the whole selection N times per Lab: the Build artifact is built once, each Guest starts and stops once, and each repetition restores Clean state (not an Ad-hoc run), installs and writes its own Run folder. A line per repetition (`PASSED mac (repetition 2 of 3): ...`), then a tally per Lab, Scenario and Check: `mac: 2 of 3 repetition(s) passed`, `mac: errored e of n` for Runs that errored before their Scenarios (a failed restore), `mac/NAME: errored e of n`, `mac/NAME: CHECK: passed k of n` over the repetitions that measured it (`, skipped s` apart). `--until-fail` (with `--repeat`) stops every Lab after its repetition in which any Lab failed or errored, runs no later Lab, and keeps the Guests still up running as `--keep` does (Labs run one after another have stopped theirs once done: `--parallel` keeps every Lab's). Exit 1 if any repetition failed.
- Ctrl-C ends the invocation with no reports (with `--repeat`, the tally covers the repetitions that ended), on every Lab, `--parallel` too: what the running Scenarios staged and spawned is closed and stopped, the Guests are stopped or kept as above, and vmlab prints what it stopped and which Guests it left running with the stop command. Under `--parallel` it waits up to 60 s for the Labs: a Lab stops before its next Scenario or at its Scenario's next call into the Guest (`g.check` is none), and one still starting its Guest or app finishes that first. A second Ctrl-C exits at once and names the Labs that had not ended.
- vmlab stops only Guests it started (recorded in `$VMLAB_HOME/started.json`), including ones an earlier Ad-hoc or `--keep` run left running. A Guest started outside vmlab, or with `vmlab up`, is never stopped by `vmlab run`.

**Measuring a Check.** A new Check is trusted once it has gone red on a Build artifact without the fix and green with it. Take the fix out of the working tree, `vmlab run NAME` (the build hook rebuilds, since files in its `inputs` are now newer than the Build artifact; the Run folder's `build.log` and `report.json`'s `deploy.built` confirm it) and expect exit 1 with that Check failed; put the fix back and expect exit 0. The skill's Regression workflow (`skill/references/regression.md`) walks the agent through it.

## Architecture

A Guest's architecture follows the Host's. A Lab's `arch` is its Build artifact's, and when it differs from the Host's the Lab runs only where the Guest OS itself runs the other architecture's programs:

| Host | native | emulated by the Guest OS | not covered |
|---|---|---|---|
| arm64 | arm64 Labs | x86_64 Windows (Windows on Arm's x64 emulation) | x86_64 macOS (vmlab installs no Rosetta 2), x86_64 Linux |
| x86_64 | x86_64 Labs | none | every arm64 Lab |

An emulated Lab runs in a Guest of the Host's architecture and its reports say so. A Lab that is not covered is never silently dropped: `vmlab doctor` warns about it (and checks nothing else for it), and `vmlab run` skips it with a warning, without building or starting anything, and writes its reports with status `skipped` (a skipped `<testcase>` in `junit.xml`). A skipped Lab does not fail the Run; `vmlab deploy` skips it with the same warning, and `vmlab up` warns before starting its Guest. Under emulation Windows shows an x64 process an x64 OS, so an app sees no difference. The x86_64 Host column is encoded but untested; tests play that Host with `VMLAB_HOST_ARCH=x86_64`, which changes only this coverage decision.

## CLI

```
vmlab init | vmlab self-update [--from PYZ]
vmlab base create NAME [--image IMAGE] [--yes] [--reprovision] | vmlab base list
vmlab clean [--yes] [--bases]            # delete orphaned clones, stray files, earlier provisionings' snapshots and (with --bases) unused Base guests
vmlab run [SCENARIO|FILE...] [--lab LAB]... [--keep] [--fresh] [--parallel] [--repeat N [--until-fail]] [--quiet]
                                         # exit 0 all passed (or skipped), 1 a Check failed or a Run errored, 2 usage/config error
vmlab deploy [LAB...] [--quiet]          # build if stale, install, launch; Guests stay running
vmlab up [LAB...] | vmlab down [LAB...]  # default: all Labs
vmlab status [--json]
vmlab doctor [LAB...] [--json] [--bench [--calls N]]
                                         # Host, arch coverage, Provider, Base guest, clone, Guest, per-Channel, screenshot and
                                         # UI helper checks with fixes; exit 1 on FAIL. --bench times each Channel of running Guests.
                                         # Outside a project: the Host only (which OSes it can test, which hypervisors it has)
vmlab ui tree|find|click|press|type|focus|clipboard|stage-text|close-staged|tray|notifications|wait-for|screenshot [--lab LAB] ...  # JSON; see "UI contract"
vmlab exec [--lab LAB] [--timeout S] -- COMMAND ...   # one command in a running Guest; its output and exit code
vmlab put GUEST_PATH [--from HOSTFILE] [--lab LAB]    # write a Guest file from piped stdin (or HOSTFILE); prints its Guest path
vmlab get GUEST_PATH [--lab LAB]                      # print a Guest file to stdout, byte for byte
vmlab version
```

`VMLAB_HOME` (default `~/.vmlab`) holds host state: the Base guest registry (`bases.json`), vmlab's SSH key and known_hosts (`ssh/`), `tart run` logs (`tart/`), Fusion VMs, their clone records and Guest credentials (`fusion/`), downloaded installer ISOs (`images/`), Guest locks (`locks/`), and the Fake Provider's Guests (`fake/`, with the Guest user's home at `fs/home`).
