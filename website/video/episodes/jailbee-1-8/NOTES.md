# JailBee 1.8 episode notes

## Production status

Narration and paid API calls approved in-session on 2026-10-26. All 11 TTS
clips and Whisper word timings generated. Measured timeline: main 166.2 seconds,
Short 39.3 seconds (including padding and cross-fades). Same voice settings as
episode 1.7. Both exports and SRT files are archived under
`.local/jailbee-1-8-video/`; no video uploaded or embed changed.

Headline: **One agent. Four workers. Your rules.**

## Source provenance

All coordinates are source pixels, `width x height + x + y`; PNG crops are
lossless. Cropping removes unrelated repository rows, never replaces UI text.
The source screenshots are genuine captured states supplied by the maintainer.

| Asset | Source under `.local/` | Crop |
| --- | --- | --- |
| orchestration.png | jailbee1.8.png | Full 1731 x 909 textless illustration |
| permissions.png | orch.png | 380 x 244 + 17 + 376 |
| workers.png | orch.png | 744 x 88 + 24 + 151 |
| roles.png | orch.png | 114 x 88 + 650 + 151 |
| worker-names.png | orch.png | 225 x 88 + 24 + 151 |
| test-card.png | Screenshot_20261026_145907.png | Full 785 x 265 |
| proposal.png | Screenshot_20261026_150231.png | 335 x 155 + 508 + 30 |
| outbox-controls.png | Screenshot_20261026_150231.png | 844 x 38 + 0 + 199 |

The illustration is the supplied textless feature artwork, not a UI rendering.
The title is animated separately; `ai` in the amber wordmark is white.

## Editorial distinctions

- The bug backlog and commit / done / import / merge sequence are labelled
  illustrations, not a claim that the screenshots show that workflow executed.
- The worker screenshot shows idle children. It proves hierarchy and role/count
  visibility, not active coding.
- The test card reports four forks and replies, stopping and destroying. It
  does not show four completed bug fixes or imported commits. Its first line
  reports a retest, not evidence for the conceptual workflow.
- The outbox proposal is pending. Preview and publication controls are shown
  separately; no PR has been published by this production task.
- A host-side grant bounds broker actions. Import never implies merge.

## Layout and timing checks

Pre-audio preview timing is provisional. Real-word timing and burned-in caption
checks must be repeated after approved TTS and Whisper calls. Main has seven
scenes; Short has four separate narration clips with portrait compositions.
Keep all Short scene content above y=1430; the default caption band is near
y=1500-1632. Real screenshot close-ups remain readable rather than fitting an
entire terminal into portrait.

## Pre-audio verification (2026-10-26)

- `tts --dry-run`: 11 missing clips, 2983 characters, no API call.
- Narration: 386 main words, 86 Short words. Estimated speech at the pipeline's
  15 characters/second is about 2:43 main and 0:36 Short, before padding/fades;
  these are not final durations.
- All 11 final-reveal layouts rendered at native resolution using the existing
  preview command through an ignored wrapper supplying synthetic timings in
  memory. The default preview has no words before TTS, so phrase anchors need
  this provisional input. No synthetic timing was written to the paid cache.
- Additional provisional frames after phrase anchors and with captions are in
  `.local/jailbee-1-8-video/provisional/`; `layout.json` records element bounds.
  All measured text/screenshot bounds fit the viewport, and portrait content
  ends above y=1430. Full audio-timed checks remain outstanding.
- Preview PNGs, main/Short contact sheets, and a 1280x720 padded thumbnail from
  `website/assets/news/jailbee-1-8.jpg` are archived under
  `.local/jailbee-1-8-video/`. No earlier `website/video/out/` existed.
- Pipeline regression tests: 24 passed. Full suite: 16186 passed, 12 snapshots
  passed (639.20 seconds). Ruff check and format check clean; mypy clean across
  285 source files. Existing renderer and 1.7 unchanged.
- Independent read-only source review found no Critical, Important or Minor
  findings. This review does not substitute for final playback.

## Audio-timed verification

All 11 clips and word timings are cached. Actual-word frames after the phrase
anchors and fully revealed/caption end frames are in
`.local/jailbee-1-8-video/timed/`. Contact sheets cover the main story, sequential
menu/outbox highlights and portrait anchors. Used anchors loaded without runtime
errors; portrait captions remain separate from scene content. Main timeline is
166.2 seconds; Short is 39.3 seconds.

Complete audiovisual playback has not been performed by the production agent.
Frame inspection and decode checks cannot establish listening quality or pacing;
a human must watch both exports end to end before publication.

## Deliverables and stream verification

| File under `.local/jailbee-1-8-video/` | Verified result |
| --- | --- |
| main.mp4 | 1920 x 1080, 30 fps, H.264 + AAC, 166.200 s, 12656497 bytes |
| short.mp4 | 1080 x 1920, 30 fps, H.264 + AAC, 39.300 s, 5338982 bytes |
| main.srt / short.srt | Separate English timed subtitles |
| thumbnail.jpg | 1280 x 720, feature graphic scaled and padded, not cropped |
| main-export-contact.jpg / short-export-contact.jpg | Frames extracted from final MP4 exports |

`ffprobe` confirms both audio/video streams and dimensions. Full sequential
`ffmpeg -v error -i FILE -f null -` decoding completed with no errors for both
exports. Final MP4 frame contact sheets were inspected, including portrait
captions. Rendering/preview workers terminate after their commands; no production
server was started. The pre-existing blog server was not intentionally changed.

## Proposed YouTube copy

Main title: JailBee 1.8: one AI coordinator, isolated workers, your rules

Main description:

One agent coordinates. Other agents work in their own isolated environments.
You decide what they are allowed to do.

JailBee 1.8 adds container orchestration for AI coding agents on your own machine.
Give each worker its own git branch, checkout, tools and Docker daemon. Set
host-enforced permissions, exchange messages, bring the branches back, and
integrate the work. Agents prepare pull request proposals; humans publish them.

JailBee is free and open source for Linux hosts.
Documentation: https://jailbee.gisgro.io/docs/orchestration/
Setup: https://jailbee.gisgro.io/docs/installation/

Chapters from the measured timeline (rounded down to whole seconds):

```text
0:00 One agent. Four workers. Your rules.
0:14 Separate environments
0:37 Host-enforced grants
1:05 Orchestrator and children
1:28 Import is not merge
1:52 Humans publish
2:09 Get JailBee
```

Exact starts after the narration cleanup: 0.00, 14.30, 37.75, 65.90, 88.95,
112.20, 129.45 seconds. Main export: 144.80 seconds.
Short starts: 0.00, 7.90, 18.10, 28.30 seconds. Short export: 37.60 seconds.
Current exports and subtitles are in `website/video/out/`; the archive and
verification records above describe the earlier render.

Short title: One AI coordinator. Four isolated workers. Your rules. #shorts

Short description: JailBee 1.8 adds bounded container orchestration for coding
agents. Separate environments, host-enforced grants, human-controlled publication.
Watch the full tour on the channel. https://jailbee.gisgro.io/

## Remaining production and publication steps

1. Narration and paid calls: approved and generated; real-word frame and
   caption checks performed.
2. Rendering uses two jobs per format. Archive MP4/SRT, padded thumbnail and
   contact sheets under `.local/jailbee-1-8-video/`; media is not committed.
3. Watch both exports end to end (frame checks and decode checks are not a
   substitute). In particular, listen for pronunciation and transitions.
4. Obtain explicit publication approval before any YouTube upload or embed edit.
   Upload English subtitles with timing, then confirm the measured chapter list.
5. Only after upload, record genuine video IDs and update any approved embeds.
