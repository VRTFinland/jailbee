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

## The demo clip

The page ships **one** clip: `assets/media/a.{webm,mp4}` with
`assets/media/a.poster.png`, in `#demos`, click-to-play. It is a real
recording of `jb new` → an agent working in the container → `jb git pull` →
`jb destroy`, made against a live (nested) Incus daemon. Every command and
every line of output on screen was executed; two stretches of dead time are
sped up and carry a visible `×6` badge for exactly as long as they run.

The four staged reconstructions the 1.0 page had are gone for good, along
with their tabs, their autoplay script and `generate.py`. Two more real
recordings are planned (a PR review and the git bridge); the four-clip tab
wiring comes back with the second one, from git history at `a68553e`, and
`tests/test_website.py::test_a_second_clip_brings_the_demo_tabs_back` fails
the moment a second `<video>` appears without it.

Three tests hold the rest of the contract:
`test_every_demo_clip_is_click_to_play_with_a_poster` (the playback guards —
`controls`, `preload="none"`, `poster`, `muted`, `playsinline`, and **not**
`loop`/`autoplay`, which belonged to the old hero loops),
`test_every_clip_the_page_references_is_actually_committed`, and
`test_no_media_file_is_a_zero_byte_stand_in`.

## How the clip is produced

`demo/render.sh` is the whole pipeline: `--record` drives a VHS tape against
the demo substrate, then a cut list under `demo/cuts/` speeds up the dead
spans and a poster is cut from the raw render. Rendering needs `vhs`, `ttyd`,
`ffmpeg` and a system Chrome on the `PATH`, plus a working Incus daemon and
the rig under `demo/rig/` — see [`demo/rig/README.md`](demo/rig/README.md).

Nothing about a clip is invented, and the honesty rules are not negotiable:
no caption states a duration (the recordings are made on a nested daemon, and
nested timings are not host timings), the agent in the recording is real and
the caption never claims what it wrote, and no stretch is sped up without the
`×N` marker on screen.

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
font or licence file, a clip the page names but nobody committed, a stale
stylesheet cache-buster. It runs as part of the normal suite
(`uv run pytest`) — there is no separate website test command.
