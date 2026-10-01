# Regression suite

Saved Scenarios in `.vmlab/scenarios/*.py` form the project's Regression suite: they run from a terminal, cron or CI through the pinned copy, with no agent: `.vmlab/run` (the same as `.vmlab/vmlab run`) runs them all. API: [scenarios.md](scenarios.md).

The loop for a new or changed Scenario: write → run as CI would → measure → report.

## 1. Write

Start from what already works: an Ad-hoc run's Scenario (`.vmlab/runs/ad-hoc/NAME.py`), or explore first as [ad-hoc-run.md](ad-hoc-run.md) steps 1-3 describe. Then save it as `.vmlab/scenarios/NAME.py`:

- One file per behaviour, named for it (`palette_reads_selection.py`); files starting with `_` are skipped, so shared helpers go in `_name.py` there and are imported plainly (`import _helpers`; [scenarios.md](scenarios.md)).
- Only deterministic Checks: the UI tree, `wait_for`, `exec` output, files, logs, processes. Turn a visual Check from an Ad-hoc run into one of these, or drop it and keep the `g.screenshot` as evidence.
- Name each Check for the behaviour it guards, in words a reader of a red CI job understands: the name is what the `FAIL` line and `junit.xml` show first.
- `FRESH = True` when the Scenario must not see what earlier Scenarios left in the Guest.
- No `time.sleep` in a Scenario: every wait is `wait_for` with a timeout, so it ends with the Scenario's clock; `g.check` its result (`detail=` the result) and it shows in the report, with what the last poll saw. A tray app's icon is `tray=APP` (it does not open the menu); what no other condition says (a server answering) is an `exec=` condition; what every Scenario would wait for after the launch belongs in the Lab's `app.ready`; a process quitting is `process=..., gone=True`, and quitting the app itself is `g.quit()`, which waits for it. Every read-based Check first waits for what it reads.
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
- Green without the fix, and the bug itself no longer shows in the Guest by hand either (the Guest changed under it: an update, a setting, what a Base guest now leaves out): tightening cannot help. Stop and tell the user what you measured (both Runs, what you tried by hand) and why it may no longer go red, then wait for their decision: keep the Check as a guard of the path it covers, look for the conditions the bug needs and set them up in the Scenario, or drop it.
- Red for another reason (an error, a different Check, a timeout): the Scenario breaks before it reaches the behaviour. Fix that first; this measurement says nothing yet.
- A Check that cannot be measured (the unfixed build is gone, the bug needs Host conditions) is recorded with `g.skip(name, reason)`, the reason saying why, and reported as unmeasured, never as proven.

### Measure flakiness

A Check that fails once and passes on the next run is flaky until measured: run it many times on one Lab and read how often it passed.

```sh
.vmlab/run NAME --lab LAB --repeat 10
```

Each repetition is a whole Suite run (restore, install, the Scenarios, its own Run folder and reports) with the Guest up throughout and the Build artifact built once. It prints a line per repetition (`FAILED LAB (repetition 2 of 10): ...; report: FOLDER`) and ends with a tally: `LAB: 9 of 10 repetition(s) passed`, then `LAB/NAME: CHECK: passed k of n` for each Check that did not pass in every repetition, over the repetitions that measured it (`, skipped s` counts its Skipped Checks apart), `LAB/NAME: errored e of n` for a Scenario that errored (`LAB: errored e of n`: the Run errored before its Scenarios, e.g. restoring Clean state), and one line for the Checks that passed every time: `LAB: all 12 Check(s) passed 10 of 10` (`11 other Check(s)` when some had lines of their own; `passed whenever their Scenario reached them` when an error stopped a repetition before some of them). Exit 1 if any repetition failed.

- The Check that failed has no line of its own (it passed 10 of 10) on the Lab where it failed: not reproduced; try the whole suite (`--repeat` with no NAME), since some failures need what earlier Scenarios leave.
- `passed k of n` with k < n: the Check or the app is flaky. Read the failing repetitions' Run folders; fix the Scenario (a missing `wait_for`) or report the app's flakiness, and measure again.
- `--until-fail` stops at the first repetition that did not pass (on every Lab) and keeps the Guests running for inspection, naming them and the stop command: `.vmlab/run NAME --lab LAB --repeat 50 --until-fail` catches a rare failure with its Guest as it was. With several Labs it runs no later Lab; a Lab that already finished has stopped its Guest, so measure one Lab at a time.

## 4. Report

Tell the user, per Lab: the Scenario file, both measured Runs (exit codes, the Check's detail on the red one, both Run folders), and the command that runs the suite without you: `.vmlab/run`.

## Running the suite

- `.vmlab/run` runs every saved Scenario on every Lab, from anywhere in the project; `.vmlab/run NAME ... --lab LAB` narrows it. It passes its arguments to `vmlab run`. A project made by an older vmlab gets it from `vmlab init`, which keeps everything else.
- The suite restores Clean state once per Lab at its start, and stops the Guests vmlab started. `--keep` leaves them running for inspection; `--fresh` restores before every Scenario; `--parallel` runs Labs concurrently as free Host memory allows; `--repeat N [--until-fail]` runs it N times per Lab ([measure flakiness](#measure-flakiness)). It prints each Lab's steps as they start and end (`LAB: restoring Clean state`, `LAB: ... done in 41s`) and each Scenario as it starts and ends (`LAB: scenario NAME`, `LAB: scenario NAME failed in 1m12s (2 of 8 Checks failed)`); `--quiet` leaves only the results.
- Exit code: 0 all passed (or skipped: a Lab this Host does not cover, or Skipped Checks next to passed ones), 1 a Check failed or a Run errored, 2 a usage or config error. Each Lab's Run folder `.vmlab/runs/<timestamp>-<lab>/` holds `report.json`, `junit.xml`, `summary.md` and `screenshots/<scenario>/NN-<name>.png`, one folder per Scenario (a second Scenario of the same name in one Suite run gets `<scenario>-2`).
- Every `vmlab run` installs the Build artifact afresh, rebuilding it first when anything in its `inputs` is newer.
