# macOS Guest traps

Symptoms first, then the cause and what to do. vmlab already handles, so Scenarios need nothing for them: lazy WebKit and Chromium accessibility trees (the Swift helper wakes them; the JXA fallback does not, and `vmlab doctor` warns when a Guest uses it), chords on any keyboard layout, focus between staging text and a hotkey (`stage-text --then`), a click on a covered element, and the Guest's clipboard and audio staying away from the Host's.

## The app lacks a permission

**Symptom**: the app shows its own "grant Accessibility" (or Screen Recording, Input Monitoring) message, or a system consent alert nobody answers covers the screen and `osascript` or the app hangs.

**Cause**: TCC grants belong to the app, and a fresh clone has none for the app under test. Base guests have SIP off, so grants can be written into TCC.db directly: a Guest-only trade-off, since the Guest is thrown away.

**Do**: grant in the Lab's `install` recipe, which runs again after every restore. Grant by **bundle id** (`client_type` 0): for an `.app`, a row keyed by the executable's path is stored and then ignored. Delete the app's rows first: a denial (`auth_value` 0) that macOS wrote when the app asked earlier wins over a grant.

```sh
id=com.example.myapp
db="/Library/Application Support/com.apple.TCC/TCC.db"
sudo sqlite3 "$db" "DELETE FROM access WHERE client='$id';"
for s in kTCCServiceAccessibility kTCCServiceScreenCapture; do
  sudo sqlite3 "$db" "INSERT INTO access (service, client, client_type, auth_value, auth_reason, auth_version, flags, last_modified)
    VALUES ('$s', '$id', 0, 2, 4, 1, 0, CAST(strftime('%s','now') AS INTEGER));"
done
```

Apple Events grants go in the user's TCC.db (`~/Library/Application Support/com.apple.TCC/TCC.db`), one row per target app. The app still sees no grant: stop and start the Guest (`vmlab down LAB && vmlab up LAB`), so tccd reads the database afresh.

Tell the user what this means for real users: an ad-hoc signed app's grant is tied to that exact build, so without a Developer ID signature every update revokes it.

## A dialog nobody sees blocks input

**Symptom**: keys and clicks do nothing; the app "does not respond".

**Cause**: a system prompt holds the screen: "Allow X to find devices on local networks?" (raised even for `127.0.0.1`), a notification permission banner, a crash reporter. Accessibility can still read the app behind it.

**Do**: take `vmlab ui screenshot` and look at it before concluding anything. Answer the prompt with `ui click`. macOS writes some titles with a typographic apostrophe (`Don’t Allow`), so match those by a substring without it (`--text "t Allow"`); `--text Allow` exactly matches the Allow button. A prompt that returns on every clone belongs in the Scenario (before the steps it blocks), or in `install` when a setting can silence it.

## A Screen Recording alert covers the screen

**Symptom**: an alert "“com.apple.sshd-session” (or another client) is requesting to bypass the system private window picker ..." covers the screen after a screenshot; `vmlab doctor` warns "Screen Recording alert over ssh" (or exec).

**Cause**: usually a Base guest provisioned before provisioning v6 (`vmlab doctor` shows the version), which did not yet silence the alert's first appearance. On a v6 Base guest it means macOS changed how it tracks these approvals: a vmlab gap to report.

**Do**: answer it with `vmlab ui click --text Allow` and check with a screenshot that it has gone, so the Run can go on. Then tell the user: for an older Base guest, `vmlab base create <base> --reprovision` fixes it for every Lab cloned from it; until then it returns after every restore.

## Notifications

**Symptom**: the app posted a notification, but no banner appears.

**Cause**: the Guest may not show banners for an app that was never allowed to; the notification still reaches the Notification Center's database.

**Do**: check the record, not the banner. Poll `~/Library/Group Containers/group.com.apple.usernoted/db2/db` (SQLite) with `g.exec` until the record appears or a timeout passes; the record lands later than the app's own log line. Put a nonce in the notification's text, and check the title from the same record the nonce is in, so a record from an earlier Run never passes.

## Menu bar extras

An app's status item (its tray icon) is a `menubaritem` of the app, and its menu's items are in the tree even while it is closed, with no bounds. Read them there; to choose one, open the menu first with `g.click(role="menubaritem", app=APP)` (the status item is usually the app's only nameless one), then click the item. Right after a launch `loginwindow` can lay a window over the menu bar for a moment, and the click refuses the covered item: wait a second and click again.

## Scripting System Events

`osascript` against System Events waits up to two minutes for a busy app and leaves a hung process behind it that blocks later calls. Wrap such scripts in `with timeout of 10 seconds ... end timeout`, and pass `g.exec` a `timeout=`. Full-screen mode: set the window's `AXFullScreen` attribute, since the Cmd+Ctrl+F chord zooms some windows instead; prove the new Space by a witness, an ordinary window that leaves the on-screen list.

## Restarting

A reboot from inside the Guest (`shutdown -r now`) cuts its own Channel and reports success before anything happens. Restart from the Host: `vmlab down LAB && vmlab up LAB`.

## Two macOS Guests at most

macOS runs at most two macOS VMs at once, the Host's own Tart VMs included. A third fails to start, naming the limit: `tart list` shows what runs; stop one, or run those Labs one after another (no `--parallel`).
