# Setup

Bring the project to a green `vmlab doctor` for every Lab the user needs. Setup changes the Host, so every host-level install and every large download waits for the user's explicit yes, asked with what it installs or downloads and how big it is.

## 1. The project folder

No `.vmlab/` in the project root: run `python3 SKILL_DIR/scripts/vmlab.pyz init` there (`SKILL_DIR` is this skill's folder). It creates `.vmlab/vmlab.toml` (a commented template), `.vmlab/scenarios/`, a `.gitignore` for `runs/`, and the pinned `.vmlab/vmlab.pyz`. It never overwrites what exists.

`.vmlab/` exists but `vmlab version` is older than the skill's copy (`python3 SKILL_DIR/scripts/vmlab.pyz version`) and the user wants newer features: `vmlab self-update` (it refuses to downgrade).

## 2. Labs

Each Lab is one Guest: one OS version on one Provider. Edit `.vmlab/vmlab.toml` from its template, keeping the user's comments:

- macOS: `provider = "tart"` (Apple Silicon Hosts), Base guest `macos-tahoe` or `macos-sequoia`.
- Linux: `provider = "fusion"`, Base guest `ubuntu-26.04`, `session = "wayland"` or `"x11"` (two Desktop sessions are two Labs).
- Windows: `provider = "fusion"`, Base guest `windows-11`.

`[labs.LAB.app]` tells vmlab how to find, build, install, quit and launch the app and which Guest paths hold its state. Derive each recipe from the project's build system and packaging, and show the user what you wrote.

## 3. Doctor

`vmlab doctor` checks the Host, architecture coverage, Providers, Base guests, clones, Guests, Channels, screenshots and UI helpers, and prints a fix beside every FAIL. Work through the FAILs top down, one fix at a time, re-running `vmlab doctor` after each:

- **A hypervisor to install** (`brew install cirruslabs/cli/tart`, `brew install --cask vmware-fusion`): ask first.
- **A Base guest to create**: `vmlab base create NAME` downloads an image (tens of GB for macOS, about 4 GB for Ubuntu) and asks before it does; without a terminal that question reads as no. Relay it to the user, and on yes run it with `--yes`. It is idempotent: after a failure, run it again.
- **Windows** (`vmlab base create windows-11`) is a wizard: the user clicks through Fusion's "Get Windows from Microsoft" and answers one Windows prompt. It needs the user's terminal: ask them to run it there (in Claude Code, `! python3 .vmlab/vmlab.pyz base create windows-11`).

Done when `vmlab doctor` shows no FAIL for every Lab the user needs. WARN lines (an architecture this Host cannot cover, a slower UI fallback) go to the user as they are.
