# Regression suite

Saved Scenarios in `.vmlab/scenarios/*.py` form the project's Regression suite: they run from a terminal, cron or CI through the pinned `python3 .vmlab/vmlab.pyz`, with no agent. API: [scenarios.md](scenarios.md).

## Writing a saved Scenario

- One file per behaviour, named for it (`palette_reads_selection.py`); files starting with `_` are skipped.
- Only deterministic Checks: the UI tree, `wait_for`, `exec` output, files, logs, processes. Turn a visual Check from an Ad-hoc run into one of these, or drop it and keep the `g.screenshot` as evidence.
- `FRESH = True` when the Scenario must not see what earlier Scenarios left in the Guest.
- Every wait is `wait_for` with a timeout.

Done when `vmlab run NAME` passes on every Lab it is meant for.

## Running

- `vmlab run` runs every saved Scenario on every Lab; `vmlab run NAME ... --lab LAB` narrows it.
- The suite restores Clean state once per Lab at its start, and stops the Guests vmlab started. `--keep` leaves them running for inspection; `--fresh` restores before every Scenario; `--parallel` runs Labs concurrently as free Host memory allows.
- Exit code: 0 all passed (or skipped), 1 a Check failed or a Run errored, 2 a usage or config error. Each Lab's Run folder `.vmlab/runs/<timestamp>-<lab>/` holds `report.json`, `junit.xml`, `summary.md` and screenshots.
