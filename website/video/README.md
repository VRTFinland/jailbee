# Narrated videos

The YouTube videos — a main video and a Short per episode — are rendered here
from sources: a script, one HTML page per scene, and the screenshots and
illustrations those pages show. Narration is OpenAI text-to-speech. Nothing is
screen-recorded, and the finished videos are not committed: they go to
YouTube, and the site embeds them from there.

```bash
uv run website/video/render.py tts --dry-run        # what narration is missing, no API call
uv run website/video/render.py tts                  # generate it
uv run website/video/render.py preview what --at 12 # one frame of a scene, as PNG
uv run website/video/render.py preview what         # one scene as a quick, small mp4
uv run website/video/render.py render main          # out/main.mp4 + out/main.srt
uv run website/video/render.py render short         # out/short.mp4 + out/short.srt
```

`--episode DIR` picks another episode (default `episodes/jailbee-1-7`). Output
lands in `out/`; narration clips and per-scene renders in the repo's
`.local/video-cache/`. Both are gitignored. The cache lives under `.local/`
because narration costs money: in the maintainer's setup `.local/` is a
host mount, so it survives `jailbee destroy`, where `out/` goes with the
container. Copy anything from `out/` you want to keep (the uploaded
`main.mp4`, say) to `.local/` too. Without such a mount, back the cache up
yourself before destroying the container.

## What it needs

- **ffmpeg, ffprobe and Chrome**: in this repo's golden image
  (`golden.extra_apt_packages` and `.jailbee/install.d/65-vhs.sh`). Another
  Chrome can be named with `JAILBEE_VIDEO_CHROME`.
- **Playwright and PyYAML**: declared inline at the top of `render.py`, so
  `uv run` fetches them into its own cache. The project's dependencies are
  untouched.
- **An OpenAI API key** for narration and word timings: `OPENAI_API_KEY`, or the file
  `~/.config/openai/api-key`. It is never written to the repo, a log or the
  cache. Keep it on the host, in that same file, and attach it to the
  container for a render with `jailbee mount openai <container>` (an optional
  mount in `.jailbee/config.yaml`: read-only, detached again after an hour).
  `api.openai.com:443` is in the same file's `egress_allow`.

## How an episode works

`episodes/<name>/script.yaml` lists the scenes in order. Each has an `id`, a
`page`, its `narration` and the `formats` it appears in (`main` 1920×1080,
`short` 1080×1920); `params` are passed to the page. Episode-level keys set the
voice (`tts.model`, `tts.voice`, `tts.instructions`), `fps`, `pad` (silence
either side of a scene's narration) and `fade` (the cross-fade between scenes).

1. **Narration.** One TTS call per scene, cached under a hash of the model,
   voice, instructions and text. Re-rendering costs nothing; editing one
   scene's narration regenerates that scene's clip only.
2. **Timing.** A scene lasts its clip plus `pad` either side. Pages pin each
   beat to the phrase it illustrates, `JB.word("green dot")` (see step 5 and
   *Pinning beats to words*), so the picture follows the voice when the text
   changes. `JB.at(fraction)` is the older way and is kept for what has no
   phrase to anchor to.
3. **Frames.** Each page is opened in headless Chrome at the format's size and
   stepped frame by frame: `JB.seek(t)` puts every Web Animation at time `t`,
   then the page is screenshotted and piped to ffmpeg. Nothing runs on
   wall-clock time, so a frame is the same however slow the machine.
4. **Assembly.** Scenes are joined with cross-fades, the clips are laid under
   them at their offsets and loudness-normalised, and an SRT is written with
   one cue per sentence.
5. **Word timings and burned-in captions.** `render.py tts` also sends each
   clip to Whisper (`whisper-1`, word timestamps) once and caches the result
   next to the clip, keyed by the same hash. The script's own words, with
   their spelling and punctuation, are aligned to those timings
   (`pipeline.align_words`). The short format gets them burned in: two or
   three words at a time, the spoken one in amber (`runtime.js`'s `captions()`,
   styled in `base.css`). A scene opts out with `params: {captions: false}`.
   The main video has none, and the SRT files are unchanged.

Write narration as speech: "one point seven", "jailbee dot gisgro dot io",
"star dot example dot com". The page shows the literal form. The captions are
the spoken text, so fix anything YouTube's auto-captions would be better at in
YouTube Studio rather than here.

### Writing a scene

A page links `../../../lib/base.css` (the site's fonts, palette and the
shared components) and `../../../lib/runtime.js`, then registers its
animations:

```html
<script src="../../../lib/runtime.js"></script>
<script>
  JB.scene(({ word, show, zoom, ring, params, format }) => {
    const img = zoom(document.querySelector(".shot"), 2);  // crisp 2x
    show(".head", 0.15);                                     // at 0.15 s
    show(ring(img, [446, 155, 50, 52]), word("green dot"));  // a box in the image's own pixels,
  });                                                        // shown as "green dot" is said
</script>
```

`html[data-format="short"]` selects the portrait layout. `show(el, from, to)`
fades an element in and optionally out; when a second animation targets the
same element, pass `"forwards"` as `JB.anim`'s `fill`, or its first keyframe
overrides the first animation before the second starts.

### Pinning beats to words

Pin every highlight, ring, packet and list item to the phrase being said, not
to a fraction of the clip. Fractions are guesses that go stale the moment the
narration is edited; in the 1.7 main video several were a whole sentence off.

- `JB.word("half-filled dot")` is the scene time that phrase starts;
  `JB.word("example dot com", { end: true })` is when it ends (an arrival, the
  end of a drawn line). The phrase is matched against the narration's own words,
  ignoring case and punctuation, so write it as spoken ("one point seven").
- `JB.when(x)` takes a number (a fraction, as `at`) or a phrase, for values a
  page reads from `params` or an attribute. Use `data-word="…"` for list items
  and let `params` carry phrases (`at: {green: "green dot"}`).
- Anchor on the first words of the thing being said, and on a phrase that is
  unique in the narration: the first match wins (`jailbee upgrade`, not
  `upgrade`).
- Order beats as the narration orders them. A picture that appears before the
  sentence that explains it reads as a mistake.
- A phrase that is not in the narration throws, so editing a sentence fails the
  render and names the phrase instead of drifting silently. After editing,
  `render.py tts` regenerates the clip and its word timings.

### Burned-in captions in a short

The short format gets word-by-word captions automatically. Design the portrait
layout around them:

- They occupy a band at the bottom, `--caption-bottom` (15% by default), about
  130 px tall. **Keep every scene's own text clear of that band**, whatever the
  moment: check the frame when the scene's last element has appeared. A scene
  whose text sits low sets `params: {caption_bottom: 24%}` to lift the band
  above it (the hook does, to stay under the screenshot and above its title);
  one that should have none sets `captions: false`.
- They are a dark plate with the spoken word as a filled amber chip, so they
  can never be taken for the scenes' own white-and-amber text. Do not restyle
  scene text with plates or chips.
- The overlay's classes are `burned-captions` and `burned-caption`. A scene's
  own `.captions` / `.caption` rules (`details.html` has them) are a different
  thing, and a clash silently moves the overlay.
- YouTube's Shorts controls cover the bottom stretch of the screen. If a
  rendered Short shows the captions hidden behind them, raise the default.

### Making a new episode

1. Write `script.yaml` (scenes, narration as speech) and the pages, with
   phrase anchors from the start.
2. `render.py tts`: narration and word timings (needs the OpenAI mount).
3. For each scene, `render.py preview <scene> [--format short] --at <t> --scale 0.35`
   at the moment *after* each anchor and again when everything is on screen.
   Check the right element is on screen at the right word, and in a short that
   nothing sits under the caption band. A contact sheet of the frames
   (`convert a.png b.png +append`) is quick to scan.
4. `render.py render main` and `render short`, then watch both end to end
   before *Publishing an episode*.

## Honesty rules

The same rules as the terminal clips (`../demo/README.md`), for the same
reason: the videos are worth making only if they show the real thing.

- Every picture of JailBee's interface is a real screenshot of real use.
  Private names are blurred, never retyped.
- Any command or output on screen was really run. Output is never invented; a
  scene without recorded output shows the commands only.
- AI illustrations are visibly illustrations (bees, honeycomb, cells) and never
  depict a user interface.
- No narration or caption states a duration.
- Example host and container names in diagrams are examples
  (`example.com`, `feat-login`), not anyone's real setup.

## Publishing an episode

Every completed render handoff includes a ready-to-paste YouTube title and
English description for **both main and Short**, without waiting for a separate
request. Include documentation/setup links, the main video's chapter timestamps,
and paths to the MP4/SRT files. Keep the current copy in the episode's `NOTES.md`
under **Proposed YouTube copy**, and give the copy in the chat as well.

Calculate chapter starts from `pipeline.timeline(...)` using the **current cached
narration durations**, padding and cross-fades; round down to whole seconds and
start at `0:00`. Recalculate after narration edits and re-renders: old chapter
lists are stale even when the scene order is unchanged. Shorts need their own
description, not a copy of the main video's chapters. If the full video's URL is
not known, refer to the channel rather than inventing a link.

After `render main` and `render short`, in this order:

1. **Watch both, end to end.** Frame checks catch layout, not pacing.
2. **Thumbnails.** YouTube wants 1280×720 for the main video. Scale the
   episode's feature image to that width and pad top and bottom with its own
   background colour rather than cropping (the image is near-black, so the
   bands do not show). A Short's cover is picked in the YouTube app; a full-size
   frame of its hook is `render.py preview <hook> --format short --at <t> --scale 1`.
3. **Upload** in YouTube Studio: video language English; subtitles from
   `out/<format>.srt` via *Upload file → With timing*; the chapter list in the
   description comes from the scene start times
   (`pipeline.timeline(...)` — each slot's `start`). Upload unlisted and publish
   with the release.
4. **Embed in the release post.** The video ID alone on its own line in the
   article (`https://youtu.be/<id>`) becomes a click-to-load player; its
   thumbnail goes to `website/assets/videos/<id>.jpg`. Use a frame of the video
   (`ffmpeg -ss <t> -i out/main.mp4 -frames:v 1 -vf scale=1280:720 …`), not the
   post's feature image, which already heads the article. See
   `website/news/README.md`.
5. **The landing page.** `#demos` in `website/index.html` carries the tour as
   the same player (update its `data-yt-id`, thumbnail, `aria-label` and
   caption) and three stills cut from the episode's scenes
   (`render.py preview <scene> --at <t> --scale 1`, saved as JPEG under
   `website/assets/img/tour-*.jpg`). Pick a `<t>` where every label and ring of
   the scene is showing.

Each episode's own facts — its video IDs, the illustration prompts used, the
frames picked for stills — live in `episodes/<name>/NOTES.md`.

## Illustrations

The episode's illustrations were generated in ChatGPT with the 1.6 and 1.7
feature images as the style reference, then saved here as JPEG. Prompts that
worked: near-black background with a faint hexagon pattern, warm amber glow,
clean flat-3D illustration, the caged bee mascot unchanged in style, a stated
empty area for overlaid text, and "no text, no logos, no user interface, no
watermark".
