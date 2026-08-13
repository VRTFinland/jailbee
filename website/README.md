# website/

The public landing page for JailBee, served from `jailbee.gisgro.io`. This
directory *is* the site: `index.html`, `assets/`, and `demo/`, as committed.
There is no build step — nothing here is compiled, bundled, or templated
before it ships.

## Previewing locally

From the repo root:

```bash
uv run python -m http.server -d website 8099
```

Then open <http://localhost:8099>. Opening `website/index.html` directly as
a `file://` URL also works — the page makes no requests that need a server,
local or otherwise.

## The demo clips are out for 1.0

The page shipped four short terminal clips behind tabs. They were staged
reconstructions, and they are being replaced by real workflow recordings made
against a live Incus daemon — work that needs a host, so it did not gate 1.0.
The `#demos` section, the clips, the posters and their CSS were removed rather
than left half-finished on a public page.

`demo/` is still here, untouched: `render.sh`, the tapes, the scene scripts,
and `generate.py`. Keeping it costs nothing — it is not served as part of the
page's content and never enters the PyPI package — and the recordings will be
built on top of it.

`tests/test_website.py::test_the_page_ships_no_clips_while_they_are_being_rerecorded`
asserts the removal is complete and fails the moment a `<video>` or a demo tab
reappears, naming the playback and tab-wiring guards that must come back with
it. Restore those from git history rather than rewriting them.

Rendering needs `vhs`, `ttyd`, `ffmpeg` and a system Chrome on the `PATH` —
none of which exist in this repo's dev container, so it runs on a host.

## How the scenes are produced

Nothing shown in the demo tables or the reconstructed terminal scripts is
invented: each is either a real render through JailBee's own code
(`demo/generate.py`, pinned against the renderer by
`tests/test_website.py`) or a hand-transcribed reproduction of a string a
named function in `src/jailbee/` actually prints, cited in a comment above
the line that uses it. The full account of which scenes are which, the
honesty rules that bind the transcriptions, and how to replace a
reconstruction with a real captured session are in
[`demo/scenes/README.md`](demo/scenes/README.md) — this file doesn't repeat
it.

## Deployment

`.github/workflows/pages.yml` deploys this directory to GitHub Pages on
every push to `main` that touches `website/**`, plus manual dispatch. There
is no build job — the artifact uploaded to Pages is `website/` exactly as
committed.

`CNAME` pins the custom domain to `jailbee.gisgro.io`. That domain resolves
only once a DNS record exists pointing it at `vrtfinland.github.io`, and
Pages is enabled on the repo — both are one-time host-side setup, not
something this workflow does.

## Tests

`tests/test_website.py` is what catches a broken reference before it ships:
mistyped asset paths, a stray absolute URL outside an anchor, a missing
font or licence file, a demo table that no longer matches what JailBee's
renderer produces. It runs as part of the normal suite (`uv run pytest`) —
there is no separate website test command.
