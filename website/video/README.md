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
lands in `out/`; clips and per-scene renders in `.cache/`. Both are gitignored.

## What it needs

- **ffmpeg, ffprobe and Chrome**: in this repo's golden image
  (`golden.extra_apt_packages` and `.jailbee/install.d/65-vhs.sh`). Another
  Chrome can be named with `JAILBEE_VIDEO_CHROME`.
- **Playwright and PyYAML**: declared inline at the top of `render.py`, so
  `uv run` fetches them into its own cache. The project's dependencies are
  untouched.
- **An OpenAI API key** for narration: `OPENAI_API_KEY`, or the file
  `~/.config/openai/api-key`. It is never written to the repo, a log or the
  cache. `api.openai.com:443` is in `.jailbee/config.yaml`'s `egress_allow`.

## How an episode works

`episodes/<name>/script.yaml` lists the scenes in order. Each has an `id`, a
`page`, its `narration` and the `formats` it appears in (`main` 1920×1080,
`short` 1080×1920); `params` are passed to the page. Episode-level keys set the
voice (`tts.model`, `tts.voice`, `tts.instructions`), `fps`, `pad` (silence
either side of a scene's narration) and `fade` (the cross-fade between scenes).

1. **Narration.** One TTS call per scene, cached under a hash of the model,
   voice, instructions and text. Re-rendering costs nothing; editing one
   scene's narration regenerates that scene's clip only.
2. **Timing.** A scene lasts its clip plus `pad` either side. Pages time their
   animations with `JB.at(fraction)` — a fraction of the narration, 0 the first
   word, 1 the last — so the picture follows the voice when the text changes.
3. **Frames.** Each page is opened in headless Chrome at the format's size and
   stepped frame by frame: `JB.seek(t)` puts every Web Animation at time `t`,
   then the page is screenshotted and piped to ffmpeg. Nothing runs on
   wall-clock time, so a frame is the same however slow the machine.
4. **Assembly.** Scenes are joined with cross-fades, the clips are laid under
   them at their offsets and loudness-normalised, and an SRT is written with
   one cue per sentence.

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
  JB.scene(({ at, show, zoom, ring, params, format }) => {
    const img = zoom(document.querySelector(".shot"), 2);  // crisp 2x
    show(".head", 0.15);                                     // at 0.15 s
    show(ring(img, [446, 155, 50, 52]), at(0.47));           // a box in the image's own pixels
  });
</script>
```

`html[data-format="short"]` selects the portrait layout. `show(el, from, to)`
fades an element in and optionally out; when a second animation targets the
same element, pass `"forwards"` as `JB.anim`'s `fill`, or its first keyframe
overrides the first animation before the second starts.

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

## Illustrations

The episode's illustrations were generated in ChatGPT with the 1.6 and 1.7
feature images as the style reference, then saved here as JPEG. Prompts that
worked: near-black background with a faint hexagon pattern, warm amber glow,
clean flat-3D illustration, the caged bee mascot unchanged in style, a stated
empty area for overlaid text, and "no text, no logos, no user interface, no
watermark".
