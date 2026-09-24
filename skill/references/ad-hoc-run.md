# Ad-hoc run

The user described a check in plain words. Turn it into a reproducible **Run** and report with evidence. The loop: deploy → explore → crystallise → clean run → report → offer to save → remind.

Every conclusion you report rests on the clean Run, never on exploration alone. Exploration finds the steps; the Run proves them.

## 1. Pick the Labs

`vmlab status` lists the project's Labs with their OS. Map the user's request onto them: "on macOS" is every macOS Lab, "everywhere" is every Lab. No Lab for an OS they named: switch to [setup.md](setup.md) and come back.

Then `vmlab doctor LAB` for each. Done when every chosen Lab has no FAIL; a FAIL routes to [setup.md](setup.md).

## 2. Deploy

`vmlab deploy LAB` builds the Build artifact when it is stale, installs it, launches the app and waits for the Lab's `app.ready` condition (if it has one), and leaves the Guest running. Its output names what it built and where. A build or install failure is the answer to report if it is the user's code; a wrong recipe in `[labs.LAB.app]` is setup.

The user asked for a completely fresh Guest: note it for step 4 (`--fresh`). Exploration can run on the current Guest.

## 3. Explore

Drive the app one primitive at a time, observing after each step, until every step the user described has been done by hand in the Guest and you know what each one looks like when it works. Commands take `--lab LAB` when the project has several Labs; each prints JSON.

- **See**: `vmlab ui tree --app APP` for structure, `vmlab ui find --text T --role R --app APP` for one element, `vmlab ui screenshot` and then look at the PNG it names, `vmlab ui notifications [--app ID] [--text PATTERN]` for the Notifications apps posted (shown on screen or not; without `--app`, every app's with its id).
- **Act**: `vmlab ui click --text T --app APP [--timeout S]` (waits for it to appear and be uncovered), `vmlab ui press CHORD`, `vmlab ui type TEXT`, `vmlab ui focus --app APP`, `vmlab ui clipboard [--set TEXT]`, `vmlab ui tray --app APP [--choose LABEL]...` (read the app's Tray menu, or choose an item: one `--choose` per submenu level).
- **Stage and trigger**: `vmlab ui stage-text TEXT --then CHORD` puts selected text in an editor and presses the app's hotkey in one Guest call, so nothing steals focus in between. It leaves that Staged document open: close it with `vmlab ui close-staged --file PATH` (the `"file"` it printed) when done, as outside a Scenario nothing else will.
- **Wait**: `vmlab ui wait-for` an element (`--text`/`--role`/`--app`), `--process NAME`, `--file PATH`, `--log PATH --pattern REGEX`, a Notification (`--notification REGEX [--app ID]`, posted after the call's start) or a command, `--exec ARG...` (it exits 0, or with `--pattern` its output matches; `--exec` goes last, as everything after it is the command), with `--timeout S`; `--gone` waits for any of them but a Notification to stop holding. Every wait is a condition plus a timeout.
- **Look inside**: `vmlab exec -- COMMAND ...` runs one command in the Guest and returns its output and exit code (vmlab's own failures say `vmlab: error:` on stderr): read the app's logs and files, list processes. `vmlab get GUEST_PATH` prints a Guest file byte for byte, and `vmlab put GUEST_PATH [--from HOSTFILE]` writes one (from piped stdin without `--from`), for test data and config the app reads.

A step that misbehaves with no clear error (input going nowhere, an empty tree, a permission message from the app) is usually a known trap: read the Guest OS's list ([macOS](traps-macos.md), [Windows](traps-windows.md), [Linux](traps-linux.md)) before working around it.

Keep a list as you go: each step's primitive, its arguments, and the observation that showed it worked (the element that appeared, the log line, the file). That list becomes the Scenario. Before crystallising, re-read the user's request: every step and every expectation in it has an entry.

## 4. Crystallise and run clean

Write the list as a Scenario (API: [scenarios.md](scenarios.md)) at `.vmlab/runs/ad-hoc/NAME.py`: `runs/` is git-ignored, and a Scenario outside `scenarios/` runs as an Ad-hoc run, which keeps Guest state and leaves Guests running.

- One `g.check` per expectation the user stated, named in their words.
- Checks read state: the UI tree, `wait_for`, `exec` output, files, logs. A judgement only a screenshot can give is `g.check(..., visual=True)` after a `g.screenshot`.
- `g.screenshot(name)` at each moment the user will want to see.
- Several Labs with different OSes: one Scenario, branching on `g.os` only where the platforms differ.

Run it: `vmlab run .vmlab/runs/ad-hoc/NAME.py --lab LAB` (repeat `--lab` per Lab; add `--fresh` when the user asked for a fresh Guest). The Run's folder is `.vmlab/runs/<timestamp>-<lab>/`.

A red Check means one of two things: the Scenario disagrees with what you saw while exploring (fix the Scenario and run again), or the app misbehaves (that is a finding: report it). Tell them apart with the Run's screenshots and `vmlab exec`; a Check stays as strict as the user's expectation. Done when a Run's result follows from the app's behaviour, not the Scenario's mistakes.

## 5. Report

Read the Run folder: `summary.md`, `report.json`, and look at every screenshot yourself. Then tell the user, per Lab:

- the verdict of each Check, with the evidence behind it (the element, the log line, the file);
- visual Checks marked "visual, unverified", with the screenshot they rest on;
- paths to the screenshots, the Run folder and the Scenario file;
- anything the Run reported about Channels falling back or Labs skipped.

## 6. Offer to save

Offer to add the Scenario to the Regression suite. On yes, follow [regression.md](regression.md): it moves to `.vmlab/scenarios/`, where only deterministic Checks belong.

## 7. Remind

The Guests stay running for fast iteration and hold Host memory. End with the command that stops them: `vmlab down LAB ...`.
