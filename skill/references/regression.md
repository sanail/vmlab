# Regression suite

Saved Scenarios in `.vmlab/scenarios/*.py` form the project's Regression suite: they run from a terminal, cron or CI through the pinned copy, with no agent: `.vmlab/run` (the same as `python3 .vmlab/vmlab.pyz run`) runs them all. API: [scenarios.md](scenarios.md).

The loop for a new or changed Scenario: write → run as CI would → measure → report.

## 1. Write

Start from what already works: an Ad-hoc run's Scenario (`.vmlab/runs/ad-hoc/NAME.py`), or explore first as [ad-hoc-run.md](ad-hoc-run.md) steps 1-3 describe. Then save it as `.vmlab/scenarios/NAME.py`:

- One file per behaviour, named for it (`palette_reads_selection.py`); files starting with `_` are skipped, so shared helpers go in `_name.py` there and are imported plainly (`import _helpers`; [scenarios.md](scenarios.md)).
- Only deterministic Checks: the UI tree, `wait_for`, `exec` output, files, logs, processes. Turn a visual Check from an Ad-hoc run into one of these, or drop it and keep the `g.screenshot` as evidence.
- Name each Check for the behaviour it guards, in words a reader of a red CI job understands: the name is what the `FAIL` line and `junit.xml` show first.
- `FRESH = True` when the Scenario must not see what earlier Scenarios left in the Guest.
- No `time.sleep` in a Scenario: every wait is `wait_for` with a timeout, so it shows in the report and ends with the Scenario's clock. What no other condition says (a server answering, a tray icon registered) is an `exec=` condition; what every Scenario would wait for after the launch belongs in the Lab's `app.ready`; a process quitting is `process=..., gone=True`. Every read-based Check first waits for what it reads.
- Nothing from your exploration: no paths outside the project, no state you set up by hand in a Guest. The Lab's `[labs.LAB.app]` recipes and the Scenario itself must bring the Guest to where the Checks start.

## 2. Run as CI would

From the project root, with the pinned copy, as a terminal or CI job will:

```sh
.vmlab/run NAME --lab LAB
```

Repeat `--lab` per Lab the Scenario is meant for. A saved Scenario's run restores Clean state first, so nothing you did in the Guest while exploring can make it pass.

Done when it passes on every Lab it is meant for, and the whole suite (`.vmlab/run`, no arguments) still does.

## 3. Measure

A new Check proves nothing until it has failed: measure it goes red on a Build artifact without the fix (or without the feature), for the reason it names, and green with it.

1. Take the fix out of the working tree, leaving the Scenario in: `git stash push -- PATHS` for the fix's files (`-u` for new ones), or revert its hunks by hand. When the fix is not written yet, the current Build artifact is already without it.
2. `.vmlab/run NAME --lab LAB`. The build hook rebuilds the Build artifact, since the files you touched in its `inputs` are now newer than it. Expect exit 1 with **the new Check** in the `FAIL` lines, its detail showing the bug's symptom. Any other Check that fails must follow from the missing fix too.
3. Put the fix back (`git stash pop`) and run again: exit 0.

Check both Runs tested the Build artifact you meant: each Run folder holds a `build.log` only when it built, and `report.json` says `"deploy": {"built": true}`. No rebuild means nothing in the Lab's `inputs` got newer: the fix lives outside them, or taking it out only deleted files (a missing input counts for nothing). Widen `inputs`, or delete the Build artifact to force a build.

What each outcome means:

- Red without the fix, green with it: the Check guards the regression.
- Green without the fix: the Check does not see the bug. Tighten it (read what the bug changes, not what surrounds it) and measure again.
- Red for another reason (an error, a different Check, a timeout): the Scenario breaks before it reaches the behaviour. Fix that first; this measurement says nothing yet.
- A Check that cannot be measured (the unfixed build is gone, the bug needs Host conditions) is reported as unmeasured, never as proven.

## 4. Report

Tell the user, per Lab: the Scenario file, both measured Runs (exit codes, the Check's detail on the red one, both Run folders), and the command that runs the suite without you: `.vmlab/run`.

## Running the suite

- `.vmlab/run` runs every saved Scenario on every Lab, from anywhere in the project; `.vmlab/run NAME ... --lab LAB` narrows it. It passes its arguments to `vmlab run`. A project made by an older vmlab gets it from `vmlab init`, which keeps everything else.
- The suite restores Clean state once per Lab at its start, and stops the Guests vmlab started. `--keep` leaves them running for inspection; `--fresh` restores before every Scenario; `--parallel` runs Labs concurrently as free Host memory allows.
- Exit code: 0 all passed (or skipped), 1 a Check failed or a Run errored, 2 a usage or config error. Each Lab's Run folder `.vmlab/runs/<timestamp>-<lab>/` holds `report.json`, `junit.xml`, `summary.md` and screenshots.
- Every `vmlab run` installs the Build artifact afresh, rebuilding it first when anything in its `inputs` is newer.
