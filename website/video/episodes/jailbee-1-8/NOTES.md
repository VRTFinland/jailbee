# JailBee 1.8 episode notes

## Production status

Narration draft awaiting editorial and paid-API approval. No audio generated,
no video uploaded, no website embeds changed. Final duration and scene start
times are unknown until narration exists. Targets: approximately three minutes
main, 35-45 seconds Short. Same voice settings as episode 1.7.

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
- Pipeline regression tests: 24 passed. Existing renderer and 1.7 unchanged.

## Proposed YouTube copy

Main title: JailBee 1.8: one AI coordinator, isolated workers, your rules

Main description:

Let a coding agent coordinate child containers without giving it a host shell.
This tour shows grants, real dashboard roles, the import-versus-merge boundary,
and the human-controlled PR outbox. The bug-backlog sequence is illustrative;
the recorded test demonstrates spawn, messaging, stop and destroy.

JailBee is free and open source for Linux hosts.
Documentation: https://jailbee.gisgro.io/docs/orchestration/
Setup: https://jailbee.gisgro.io/docs/installation/

Chapters: pending measured scene start times; do not publish estimated chapters.

Short title: One AI coordinator. Four isolated workers. Your rules. #shorts

Short description: JailBee 1.8 adds bounded container orchestration for coding
agents. Separate environments, host-enforced grants, human-controlled publication.
Watch the full tour on the channel. https://jailbee.gisgro.io/

## Remaining production and publication steps

1. Approve `script.yaml` narration and paid API calls.
2. Generate narration, inspect each phrase-anchored reveal and caption band.
3. Preserve any existing `website/video/out/` exports, render both formats with
   two jobs, and archive MP4/SRT plus thumbnail/contact sheets under
   `.local/jailbee-1-8-video/`.
4. Verify streams with ffprobe; watch both exports end to end (frame checks are
   not a substitute). Record measured durations and chapter starts here.
5. Obtain explicit publication approval before any YouTube upload or embed edit.
