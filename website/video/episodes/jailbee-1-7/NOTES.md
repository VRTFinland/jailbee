# JailBee 1.7 — episode notes

Rendered 2026-10-12. Main video 3:22 (1920×1080), Short 0:34 (1080×1920),
narration `gpt-4o-mini-tts-2025-12-15`, voice `marin`.

## YouTube

| | ID | URL |
|---|---|---|
| Main | `4FWhKtnBpXo` | https://youtu.be/4FWhKtnBpXo |
| Short | `4HqNJCPtfws` | https://youtube.com/shorts/4HqNJCPtfws |

Uploaded unlisted, to go public with the 1.7.0 release. The main video is
embedded in `website/news/posts/2026-10-12-jailbee-1-7.md` and in the landing
page's `#demos`; the Short is not embedded anywhere.

Title: *JailBee 1.7: run a dozen AI coding agents from one dashboard*. Short:
*Which of your 12 AI agents needs you right now? #shorts*. Description
hashtags: `#AIagents #ClaudeCode #DevTools`.

## Frames used elsewhere

- Player thumbnail (`website/assets/videos/4FWhKtnBpXo.jpg`): `out/main.mp4`
  at 8.5 s, the cold open's title over the dashboard.
- Landing stills: `ai-column --at 22` → `tour-agents.jpg`,
  `details --at 18.5` → `tour-details.jpg`, `marks --at 18` → `tour-bulk.jpg`.
- YouTube thumbnail: the 1.7 feature image, scaled to 1280 wide and padded to
  720 high with its own background.

## Illustration prompts

Generated in ChatGPT with the 1.6 or 1.7 feature image attached as the style
reference, saved as `assets/*.jpg` (JPEG quality 90).

**cold-open.jpg**

> A 1920x1080 cinematic background in exactly the visual style of the attached
> image: near-black background with a faint hexagon pattern, warm amber glow
> accents, clean flat-3D illustration. A wide honeycomb of about twelve
> hexagonal cells seen at a slight angle, each cell a small dark room with one
> glowing amber window, a few cells brighter than the others as if busy. The
> right third of the image is calm and mostly empty dark space for overlaid
> text. No text, no logos, no bees, no user interface, no watermark.

**agent-cells.jpg**

> A 1920x1080 illustration in exactly the style of the attached image: the
> same cute, determined bee mascot sits at a tiny desk with a laptop inside ONE
> hexagonal honeycomb cell drawn as a cosy dark room with an amber-lit window.
> Around it, neighbouring cells are separate rooms with their own bees and
> desks, clearly walled off from each other. Outside the honeycomb, at the
> bottom left, a bigger plain desk with a laptop represents the developer's own
> computer, untouched and calm. Near-black background, amber glow, faint
> hexagon pattern. No text, no logos, no user interface, no watermark.

**outro.jpg, outro-tall.jpg**

> Two versions of the same image, one 1920x1080 and one 1080x1920, in exactly
> the style of the attached image: the caged bee mascot from the reference,
> small and centred in the upper half, glowing softly, with a few hexagonal
> cells floating around it. The lower half is empty near-black space with a
> faint hexagon pattern, left free for overlaid text. No text, no logos, no
> watermark.

The 1.7 feature image (`website/assets/news/jailbee-1-7.jpg`) came from a
prompt asking for the 1.6 card's style with "JailBee 1.7 / A dozen agents, one
dashboard" and an abstract, illegible terminal dashboard whose rows link to
honeycomb cells.

## Privacy

The screenshots are from the maintainer's real use. One internal repository
name (`vaaka-combined`) is pixelated and blurred in every crop that shows it.
