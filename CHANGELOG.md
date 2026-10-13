# Changelog

Release notes, cut from here by the release workflow; see the README's Release section.

## Unreleased

- Linux: a WebKitGTK page's elements (a Tauri app's) in a window with a header bar get their true bounds,
  on Wayland and X11, and `click` lands on them;
  until now they were shifted up and left by the shadow and header bar,
  and a click landed beside the element with no error.

## 2.0.0-beta.5

- Labs: `grant` under `[labs.NAME]` gives apps the permissions the OS would ask for by a dialog,
  e.g. `grant = { screen_capture = ["/Applications/MyApp.app"] }`,
  with names that do not depend on the OS (screen_capture, accessibility, input_monitoring, camera, microphone) and apps
  by Guest path or id. vmlab gives them after install, at every restore, on deploy and on an Ad-hoc run:
  on macOS TCC rows and, for screen_capture, replayd approvals,
  so no "bypass the system private window picker" alert appears, an ad-hoc signed app's included;
  on Wayland the portal's non-interactive Screenshot; on Windows camera and microphone.
  `vmlab doctor` names a grant that does not hold.
  See references/app-recipes.md.
- Scenarios: `SESSIONS = ["x11"]` (or `"wayland"`) limits a Scenario's Linux Labs to that desktop session,
  next to `OS` and `LANGUAGES`; macOS and Windows Labs still run it.
  `g.session` gives a Linux Lab's session.
- UI: a click by text or role without `index` clicks the first match in view:
  a match without bounds or covered by another window is passed over, and when every one is,
  the error says why for each; `timeout` retries the set.
  With `index` the click keeps that match or its error.
  A drag's source does the same. click's result, each drag end and report.json's calls name the match taken (`index`).
- Linux: GTK 4 elements that take clicks read `enabled: true`; until now they read false.
  An insensitive one still reads false.
- Linux: each of an app's lookalike windows (several dialogs alike left open) has its own bounds;
  find gave a covered one the bounds of the one on top, or the other way round.
- Linux: on X11 a click into a GTK 4 window opened under the resting pointer is no longer dropped.
- Runs: a Check whose detail is a tuple no longer crashes the end of the Run.
- macOS: `vmlab doctor` right after `up` no longer warns now and then that Channel exec "hangs":
  the first `tart exec` after a boot, which may wait 10-20 s for the guest agent, is now made by `up`,
  also when a Scenario restores the Guest.
- `vmlab clean` removes the clones of a project whose vmlab.toml has no Lab left,
  and names a project whose config does not load (its clones are kept) and says to fix it and run clean again.
- Docs: references/app-recipes.md says how a Wayland app's ScreenCast can be allowed ahead,
  through the app's own restore token, which grant cannot give;
  references/traps-linux.md says to answer the GlobalShortcuts approval dialog in every Scenario that launches the app.

## 2.0.0-beta.4

- Linux: on an X11 Lab, a pointer call whose first point is where the pointer already is no longer waits about 16 s.
  A second click at the same point, a `pointer down` or `up` where a click left the pointer,
  a `drag` from where a click landed, and a drag whose steps round to the same pixel each took ~16.7 s,
  since 2.0.0-beta.2 at least; they now take under a second.
  GNOME on Wayland was not affected.

## 2.0.0-beta.3

- Labs: a Lab's clone or Windows copy prepared by an earlier vmlab (its session, language or scale set by that version's
  extra boot) is made again at the next `vmlab up`, and `vmlab doctor` says so in its Clone row.
  An Xfce Lab at scale 2 cloned by 2.0.0-beta.1 failed every `up` under beta.2 with "asks for scale 2,
  but its Guest shows 1"; it now comes up at scale 2 without renaming the Lab.
  Clones with none of those settings are kept.
- Windows: a Base guest has no Windows Spotlight item on the desktop and no Edge first-run page (provisioning v8).
  A click near the top right corner hit "Learn about this picture",
  which opened Edge's full-screen welcome page over the app under test.
  `vmlab base create` provisions a v7 Base guest again, and its copies are made again.
- Runs: a failed or errored Scenario's report lists the `g.*` calls it made:
  `report.json` holds its last 200 under `calls` (name, arguments, error, start, duration; a `wait_for` is one call),
  and `summary.md` shows the last 20 under its result.
  A passed Scenario writes none.
  `--quiet`'s help says what step lines are.
- Linux: on Xfce, a drag after a drag of selected text selects nothing in GTK 4 apps (GTK 4 on X11 keeps the press for
  the text drag). references/traps-linux.md says so, and what to do: a click before the next drag.
- Linux: a staged text no longer shows "File Has Changed on Disk" in gnome-text-editor now and then.
- Runs: a failed or skipped Check whose detail is not text no longer cuts `junit.xml` short.

## 2.0.0-beta.2

- Linux: an Xfce Lab with scale = 2 comes up at scale 2; in 2.0.0-beta.1 every `vmlab up` of one failed with "asks for
  scale 2, but its Guest shows 1".
- UI: g.drag takes fractional points, as click(at=) and pointer do (a Scenario multiplying by a fractional scale passes
  floats), and every point reaches the Guest rounded to whole Desktop pixels:
  click and pointer used to send the floats on, which Linux truncated.
- UI: g.displays() on Linux gives a whole scale as an int (2, not 2.0), as on macOS and Windows.
- Docs: references/migrating.md, "1.x → 2.0", now says a Windows Guest is at scale 2 on a Retina Mac:
  its bounds and click(at=) keep their numbers, but a Scenario's own numbers for a logical size (an inset, a tolerance)
  multiply by the scale to work at several scales.
  Without a Lab scale a Guest keeps its own: 2 on macOS and on Windows on a Retina Mac, 1 on Linux.
- Labs: a scale the Guest OS cannot show is a config error, and like any config error it stops every command for every
  Lab of the project.

## 2.0.0-beta.1

- Breaking: every coordinate of the UI contract (node bounds, click(at=), the desktop node) is in Desktop pixels on
  every Guest OS, so a pixel of g.screenshot() is the point g.click(at=...) hits.
  On macOS these were points (half the numbers at Tart's scale of 2).
  A Scenario that clicks at fixed coordinates or computes points with numbers of its own multiplies them by the
  Display's scale; one that clicks elements needs nothing.
  See references/migrating.md, "1.x → 2.0".
- UI: g.displays() / vmlab ui displays list the Guest's Display (bounds, scale, primary).
- UI: g.drag(source, target, button=, hold=, steps=, duration=, timeout=) / vmlab ui drag drags between elements (or
  points), and g.pointer("move"|"down"|"up", ...) / vmlab ui pointer sends one pointer event;
  click takes button= (left, right, middle, back, forward) and hold= (modifiers).
  A press on an element something else covers is refused, as a click is,
  and a button a Scenario leaves held is released at the end of its Run.
- Labs: [labs.NAME] scale sets the Guest's Display scale (macOS 1 or 2, Windows and GNOME 1 to 2 in steps of 0.25, Xfce
  1 or 2); a scale the Guest OS cannot show is a config error listing those it can.
- Linux: stopping a Guest no longer waits boot_timeout when an app with unsaved work blocks the session's shutdown.
- doctor no longer fails with a traceback when another vmlab re-clones a Fusion VM while it runs.
- Re-provisioning: Tart provisioning is v18 and Fusion Linux provisioning v9;
  the next `vmlab base create` re-provisions macOS and Linux Base guests by itself.
- License: Apache-2.0 from this release on; released versions stay MIT.

## 1.1.1

- Windows: provisioning turns account lockout off.
  A WebView2 app tries a blank password for the Guest user at every start,
  and after 10 such starts in 10 minutes Windows locked the user out:
  every new ssh connection was reset ("scp: Connection closed", "the Guest's call folder ... could not be made").
  Provisioning is v7; run `vmlab base create windows-11` to re-provision Windows Base guests,
  and Labs are copied again at their next start.

## 1.1.0

- Scenarios: `OS` and `LANGUAGES` limit a Scenario to the Labs they match;
  the ones left out are listed in report.json ("excluded") and summary.md,
  and a Lab left with no Scenario is skipped unbuilt.
- run --parallel: the Labs running at once stay within the Host's cores × 1.5 vCPUs (`VMLAB_HOST_CPUS` sets it);
  the rest queue.
- UI: every node has `checked` (true, false, "mixed", or null) on macOS, Windows and Linux; find,
  click and wait_for take `--checked`, `--unchecked` and `--mixed`.
- Tart: provisioning v15; the next `vmlab base create` re-provisions macOS Base guests by itself.

## 1.0.3

- macOS: type returns once the focused element holds the typed text,
  so a key pressed right after it no longer lands mid-text.
- macOS: closed Tray menus stay out of tree, find and wait_for;
  tray with a timeout waits for an item the app adds or rebuilds after launch.
- macOS: a missing Accessibility grant is asked for again for 5 s before failing.
- Fusion: a Guest that boots without showing its screen within 30 s fails the Lab, and a 1x1 screenshot is an error.
- Tart: provisioning v14; the next `vmlab base create` re-provisions macOS Base guests by itself.

## 1.0.2

- macOS: stage-text fails, naming the app, when another app's panel holds the keyboard,
  instead of reporting nothing selected.
- macOS: quit and wait_for(process=...) wait until LaunchServices lets go of the app,
  so an immediate relaunch no longer fails with -600.
- Tart: --parallel Labs no longer fail while another Lab is cloning its Guest.
- The macOS traps note that an app's first Notification in a fresh clone can take up to 60 s.

## 1.0.1

- Windows: key chords (Ctrl+A and the like) are paced like a real keyboard's,
  so the chord no longer occasionally types a plain letter.
- The plugin marketplace installs over HTTPS, without a GitHub SSH key.

## 1.0.0

- First release: the vmlab skill and CLI for testing desktop apps in macOS (Tart, VMware Fusion),
  Windows and Linux Guests.
