# Setup

Bring the project to a green `vmlab doctor` for every Lab the user needs, with a running Guest and a working deploy. Setup changes the Host, so every host-level install and every large download waits for the user's explicit yes, asked with what it installs or downloads, from where, and how big it is. A yes covers the one thing asked about.

Before `.vmlab/` exists, `vmlab` below is `python3 SKILL_DIR/scripts/vmlab.pyz` (`SKILL_DIR` is this skill's folder); after step 4 it is the project's pinned copy.

## 1. What this Host can test

Run `vmlab doctor` in the project root. Before `.vmlab/` exists it checks only the Host: the `Host` line says which OSes this Host can test, `Hypervisors` which are installed. Map the OSes the user asked for onto Providers:

| Guest OS | Provider | Host |
| --- | --- | --- |
| macOS | `tart` (Tart) | Apple Silicon Mac only |
| Linux, Windows | `fusion` (VMware Fusion) | Mac |

Tell the user which of their OSes this Host covers, which need a hypervisor installed, and which it cannot cover at all (macOS on an Intel Mac; the other architecture's builds, see the WARN lines in step 6). Done when the user has agreed on the list of Labs to set up.

## 2. Hypervisors

A hypervisor is missing when step 1's `Hypervisors` line says `not found` for it and its `Host` line says this Host can run its Guests (Tart needs Apple Silicon). For each missing one the agreed Labs need, ask first, then:

- **Tart** (an agent can install it): `brew install cirruslabs/cli/tart`. No `brew`: installing Homebrew is its own host install, asked about separately (https://brew.sh).
- **VMware Fusion** (only a person can install it): Fusion Pro is free, but the download sits behind a free Broadcom account, and Homebrew has no cask for it. Walk the user through it one step at a time, and verify each step before giving the next:
  1. Sign in or register at https://support.broadcom.com, then find "VMware Fusion" under the free downloads and download the latest Fusion Pro. Verified when the user names the downloaded `VMware-Fusion-*.dmg` (usually in `~/Downloads`) and it is there.
  2. Open the `.dmg` and drag VMware Fusion to Applications. Verified when `/Applications/VMware Fusion.app/Contents/Public/vmrun` exists.
  3. Open VMware Fusion once and answer what it asks on first launch (its licence terms, macOS permission prompts). Verified when the user confirms Fusion's window is open with no prompt left, and `vmlab doctor` shows `VMware Fusion <version>` on its `Hypervisors` line.

Done when the `Hypervisors` line names every hypervisor the agreed Labs need.

## 3. Base guests

A Base guest is one OS version, shared by every project on this Host, and each Lab runs in a clone of it. `vmlab base list` shows the ones this Host has; create only those the agreed Labs need that it does not list as `ready`. This works before `.vmlab/` exists:

- **macOS**: `vmlab base create macos-tahoe` (or `macos-sequoia`) downloads a cirruslabs image of tens of GB. When asking, tell the user the trade-off it makes: the image has SIP off, and provisioning writes automation grants straight into the Guest's TCC database, the only way to grant them without MDM. It weakens only this throwaway Guest, never the Host.
- **Linux**: `vmlab base create ubuntu-26.04` downloads the Ubuntu desktop ISO (about 4 GB) and installs it unattended (about 15 minutes in all). An ISO the user already has goes in with `--image PATH`.
- **Windows**: `vmlab base create windows-11` is a wizard: the user clicks through Fusion's "Get Windows from Microsoft" and answers one Windows prompt in a window it opens. It needs the user's terminal: ask them to run it there (in Claude Code: `! python3 SKILL_DIR/scripts/vmlab.pyz base create windows-11`, with the real path).

`base create` asks before it downloads; without a terminal that question reads as no. Relay it to the user, and on yes run it with `--yes`. It is idempotent: after a failure, read its error, fix the cause, and run it again; it resumes. Done when `vmlab base list` shows each one `ready`.

## 4. The project folder

`vmlab init` in the project root creates `.vmlab/vmlab.toml` (a commented template), `.vmlab/scenarios/`, a `.gitignore` for `runs/`, and the pinned `.vmlab/vmlab.pyz`. It never overwrites what exists.

`.vmlab/` already exists, but `vmlab version` is older than the skill's copy (`python3 SKILL_DIR/scripts/vmlab.pyz version`) and the user wants newer features: `vmlab self-update` (it refuses to downgrade).

## 5. Labs and the app recipe

Each Lab is one Guest: one OS version on one Provider. Add one `[labs.NAME]` table per agreed Lab to `.vmlab/vmlab.toml`, adapted from the template's examples, keeping every comment the user has written:

- macOS: `provider = "tart"`, `[labs.NAME.tart] base = "macos-tahoe"`.
- Linux: `provider = "fusion"`, `[labs.NAME.fusion] base = "ubuntu-26.04"`, `session = "wayland"` (GNOME) or `"x11"` (Xfce). Testing both Desktop sessions means two Labs.
- Windows: `provider = "fusion"`, `[labs.NAME.fusion] base = "windows-11"`.
- `arch` is the Build artifact's; leave it out for the Host's own.

Then write each Lab's `[labs.NAME.app]`: how the Build artifact is built on the Host, installed, quit and launched in the Guest, and which Guest paths hold the app's state. Read [app-recipes.md](app-recipes.md) and derive every recipe from the project's build system and packaging. Show the user what you wrote, and say which recipes are guesses to confirm.

## 6. Doctor to green

`vmlab doctor` checks the Host, architecture coverage, Providers, Base guests, clones, Guests, Channels, screenshots and UI helpers, and prints a fix beside every FAIL. Work through the FAILs top down, one fix at a time, running `vmlab doctor` again after each. A fix that installs or downloads something is asked about first, as above.

A stopped Guest shows `info ... stopped; Channels not checked`: start it with `vmlab up LAB` and run `vmlab doctor LAB` again, so its Channels, screenshots and UI helper are checked too. WARN lines (an architecture this Host cannot cover, a slower fallback Channel or UI helper) go to the user as they are.

Then prove the recipes: `vmlab deploy LAB` builds when stale, installs and launches. A failure names the recipe and the Guest command to try by hand (`vmlab exec --lab LAB -- ...`). Something odd with no clear error (a permission prompt, an empty accessibility tree, keys going to the wrong window): read the Guest OS's traps: [macOS](traps-macos.md), [Windows](traps-windows.md), [Linux](traps-linux.md).

Done when, for every agreed Lab, `vmlab doctor LAB` with its Guest running shows no FAIL and `vmlab deploy LAB` exits 0 with the app running: `vmlab ui screenshot --lab LAB`, look at the PNG, and see the app's window with nothing covering it. A system alert or prompt over it is a finding: clear it (see the traps above) or report it to the user as unresolved. Tell the user it is ready, remind them `vmlab down` stops the Guests, and return to the workflow that sent you here, if any.
