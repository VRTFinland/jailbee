# Web dashboard browser tests

Run `make web-test` for the Vitest component/view tests and `make web-e2e`
for this suite. The latter builds the production app, type-checks the suite
and drives a fresh real dashboard server in the installed Chrome. No live
Incus or agent is needed. Dependencies belong to the `e2e` uv group.

## Gallery screenshot references

`test_gallery_snapshots.py` compares every group of the labelled gallery
panels (surfaces, type scale, icons, buttons, switches, status chips, list
rows, …) for all three themes (light, dark, e-ink), both display profiles
(normal, e-ink) and two viewport widths (390px phone, 1280px desktop): one
committed PNG per group, named `<width>-<theme>-<profile>-<group>.png`, under
`tests_e2e/snapshots/`. Capturing per group keeps one recoloured chip or icon
a large share of its image. The phone has touch/coarse-pointer emulation;
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

Pillow compares each pixel with the colour range its 3×3 neighbourhood spans
in the other image, both ways round, in R, G, B and the colour differences
R−G, G−B and B−R. An unrelated change elsewhere on the page can move a group
by a fraction of a pixel and re-shade its curved edges; such a pixel is a
blend of colours beside it and stays in range. A recoloured chip or icon, or
a thin stroke that appears or vanishes, does not. Two budgets apply to how far
pixels fall outside that range: at most 20 pixels per group image by more than
24/255, and a mean of at most 0.25/255. Dimensions must match exactly. The
first budget is an absolute count, not a fraction, so a single recoloured 16px
icon (about 36 pixels) or chip (about a thousand) fails however large its
group. The mean cap catches broad, low-amplitude colour drift (a uniform shift
is out of range on every flat area).
Do not loosen these limits merely to accept an intended visual change.

For an intentional design change:

```bash
make web-e2e-snapshots
# Review the changed PNGs, then verify without updating:
make web-e2e
```

The regeneration target builds the app and runs only the twelve capture
cases with `--update-gallery-snapshots`. It rewrites only the references that
no longer match (unchanged groups keep their bytes, so a design change churns
only what it touched) and writes them losslessly at Pillow's maximum,
deterministic PNG compression. Lossy quantisation (pngquant) is not used: its
error would exceed the mean cap. It never touches application source; build/dependency output stays gitignored and
Playwright's output uses a temporary directory, leaving existing failure
evidence untouched. It does not update references during ordinary
`make web-e2e` runs. Review the images and commit them with the design change.
When adding primitives, put their variants/states into a `.group` (with a
`.group-title`) of the gallery's labelled panels; a new group is captured
automatically after regeneration. A reference left by a removed or renamed
group fails the comparison; regeneration deletes it.

A mismatch writes `actual.png` and a four-times-amplified `diff.png` under
`<group>/` in that test's directory in `_build/e2e-results/` (or `E2E_OUTPUT`). The assertion
prints both paths and error measurements. A missing reference fails too,
retaining the actual image rather than silently accepting it. The web CI
workflow runs the same comparison and uploads the evidence directory on
failure. CI never regenerates references.
