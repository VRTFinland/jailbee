# Web dashboard browser tests

Run `make web-test` for the Vitest component/view tests and `make web-e2e`
for this suite. The latter builds the production app, type-checks the suite
and drives a fresh real dashboard server in the installed Chrome. No live
Incus or agent is needed. Dependencies belong to the `e2e` uv group.

## Gallery screenshot references

`test_gallery_snapshots.py` compares the entire labelled gallery panel for
all three themes (light, dark, e-ink), both display profiles (normal, e-ink)
and two viewport widths (390px phone, 1280px desktop): twelve committed PNGs
under `tests_e2e/snapshots/`. The phone has touch/coarse-pointer emulation;
the desktop has a fine pointer. Only the gallery is compared, not variable
live dashboard data or the surrounding app chrome.

Capture settings are fixed: device scale factor 1, CSS-pixel screenshots,
844px viewport height, en-US locale, light OS colour preference, reduced
motion, disabled screenshot animations and hidden carets. The test waits for
the bundled Inter font. Token labels use DejaVu Sans Mono explicitly because
the app's platform monospace stack otherwise differs across Linux installs;
install `fonts-dejavu-core` locally if missing (the web workflow installs it).
The fixed bottom reading guard is hidden during capture so it cannot obscure
a stripe of a tall panel. No application stylesheet or CSP is changed.

Pillow compares RGB channels with two small error budgets: at most 0.2% of
pixels may differ by more than 24/255 in any channel, and mean absolute
channel error must stay at or below 0.25/255. Dimensions must match exactly.
The first budget allows sparse text-edge rasterisation differences between
Chrome versions; the mean cap also catches broad, low-amplitude colour drift.
Do not loosen these limits merely to accept an intended visual change.

For an intentional design change:

```bash
make web-e2e-snapshots
# Review the changed PNGs, then verify without updating:
make web-e2e
```

The regeneration target builds the app and runs only the twelve capture
cases with `--update-gallery-snapshots`. It rewrites all reference PNGs,
never application source; build/dependency output stays gitignored and
Playwright's output uses a temporary directory, leaving existing failure
evidence untouched. It does not update references during ordinary
`make web-e2e` runs. Review the images and commit them with the design change.
When adding primitives, put their variants/states into the gallery's existing
labelled panels; they are included automatically after regeneration.

A mismatch writes `actual.png` and a four-times-amplified `diff.png` under
that test's directory in `_build/e2e-results/` (or `E2E_OUTPUT`). The assertion
prints both paths and error measurements. A missing reference fails too,
retaining the actual image rather than silently accepting it. The web CI
workflow runs the same comparison and uploads the evidence directory on
failure. CI never regenerates references.
