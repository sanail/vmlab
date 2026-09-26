---
name: vmlab
description: Test desktop apps inside macOS, Windows and Linux virtual machines on this machine. Use when the user asks to check, test or try their app on macOS, Windows or Linux (click through it, press a hotkey, take a screenshot), to set up VMs for that (Tart, VMware Fusion), or to write or run saved VM tests; or when the project has a .vmlab/ folder.
---

# vmlab

This skill runs a desktop app inside **Guests** (virtual machines) on this **Host** and drives its UI. A project describes its Guests as **Labs** in `.vmlab/vmlab.toml`; a **Scenario** is a Python file of steps and **Checks**; a **Run** executes one Scenario on one Lab and writes reports and screenshots.

## The CLI

In a project, `vmlab` means `python3 .vmlab/vmlab.pyz`, run from the project root: the copy pinned in the project. Before `.vmlab/` exists, the only copy is `scripts/vmlab.pyz` in this skill's folder. Every command has `--help`.

## Route

Pick the one workflow that fits and read it before acting:

- **Setup**: no `.vmlab/` yet, no Lab for the OS the user wants, `vmlab doctor` shows FAIL, or the user asks to set up Guests. Read [references/setup.md](references/setup.md).
- **Ad-hoc run**: the user describes something to check in their app now ("open X, press Y, check Z appears, take a screenshot"). Read [references/ad-hoc-run.md](references/ad-hoc-run.md).
- **Regression**: the user wants saved Scenarios written, changed, measured, run as a suite or checked for flakiness. Read [references/regression.md](references/regression.md).

A workflow that hits a setup problem (a Lab missing, `doctor` failing) switches to Setup, then returns.
