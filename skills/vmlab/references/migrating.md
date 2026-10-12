# Migrating Scenarios to a newer vmlab

Read the section for each major version a project crosses when `self-update` moves it (the `old -> new` it prints),
and fix its Scenarios before the next Suite run.

## 1.x → 2.0: Desktop pixels

Every coordinate the UI contract takes or returns (node `bounds`, `click(at=)`, the desktop node, `displays()`) is in
Desktop pixels on every Guest OS: a pixel of `g.screenshot()` is the point `g.click(at=...)` at the same coordinates
lands on.
Up to 1.x each OS used its own space.

- **macOS**: bounds and `click(at=)` were points; they are now pixels, twice the numbers at Tart's default scale of 2.
  A Scenario that clicks at fixed coordinates, or computes a point from bounds with numbers of its own (an offset, a
  margin, a size it compares bounds to), multiplies those numbers by the Display's scale:
  `g.displays()["displays"][0]["scale"]`.
  A Scenario that clicks elements (`g.click(text=...)`) or computes only from bounds it read needs nothing.
- **Windows**: bounds and `click(at=)` were already pixels, so they keep their numbers at any scale, the default 2 on a
  Retina Mac included.
  Numbers of the Scenario's own that stand for a logical size (an inset, a tolerance) were pixels too and still are:
  they fit only the scale they were written at, so a Scenario run at several scales multiplies them by the scale,
  as on macOS.
- **Linux** at scale 1 (every Fusion Linux Guest without a Lab `scale`): nothing changes.
- **A Linux Wayland Guest scaled by hand**: bounds and points were GNOME's logical coordinates; they are now pixels,
  as on macOS.

To find what to change, look in the Scenarios for `at=`, `"bounds"` and numbers that look like screen positions.
