# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "pyyaml>=6.0"]
# ///
"""Render a narrated video episode: TTS clips, HTML scenes, ffmpeg.

    uv run website/video/render.py tts     [--episode DIR] [--dry-run]
    uv run website/video/render.py preview SCENE [--format main|short] [--at SECONDS]
    uv run website/video/render.py render  main|short [--episode DIR]

See website/video/README.md. The pure parts live in pipeline.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import (
    Episode,
    MissingKeyError,
    Scene,
    Slot,
    api_key,
    cache_key,
    captions,
    load_episode,
    timeline,
    total_length,
)

HERE = Path(__file__).resolve().parent
# Under the repo's .local/ so paid narration can outlive the container: there
# .local/ is commonly a host mount, while the rest of the clone dies with it.
CACHE = HERE.parents[1] / ".local" / "video-cache"
OUT = HERE / "out"
DEFAULT_EPISODE = HERE / "episodes" / "jailbee-1-7"
VIEWPORTS = {"main": (1920, 1080), "short": (1080, 1920)}
SPEECH_URL = "https://api.openai.com/v1/audio/speech"
ESTIMATED_CHARS_PER_SECOND = 15.0
"""Only for previewing a scene whose clip has not been generated yet."""


def chrome() -> str:
    path = os.environ.get("JAILBEE_VIDEO_CHROME") or shutil.which("google-chrome")
    if not path:
        sys.exit("render: no Chrome: put google-chrome on PATH or set JAILBEE_VIDEO_CHROME")
    return path


def clip_path(episode: Episode, scene: Scene) -> Path:
    return CACHE / "tts" / f"{cache_key(episode.tts, scene.narration)}.wav"


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def synthesize(episode: Episode, scene: Scene, key: str, target: Path) -> None:
    body = json.dumps(
        {
            "model": episode.tts.model,
            "voice": episode.tts.voice,
            "instructions": episode.tts.instructions,
            "input": scene.narration,
            "response_format": "wav",
        }
    ).encode()
    request = urllib.request.Request(
        SPEECH_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            audio = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        sys.exit(f"render: TTS for {scene.id} failed: HTTP {error.code}: {detail}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    partial.write_bytes(audio)
    partial.replace(target)


def ensure_clips(episode: Episode, scenes: Sequence[Scene], *, dry_run: bool) -> None:
    missing = [scene for scene in scenes if not clip_path(episode, scene).is_file()]
    chars = sum(len(scene.narration) for scene in missing)
    print(f"tts: {len(scenes) - len(missing)} cached, {len(missing)} missing ({chars} characters)")
    if dry_run or not missing:
        return
    try:
        key = api_key()
    except MissingKeyError as error:
        sys.exit(f"render: {error}")
    for scene in missing:
        print(f"tts: {scene.id}")
        synthesize(episode, scene, key, clip_path(episode, scene))


def durations(
    episode: Episode, scenes: Sequence[Scene], *, estimate: bool = False
) -> dict[str, float]:
    result = {}
    for scene in scenes:
        path = clip_path(episode, scene)
        if path.is_file():
            result[scene.id] = probe_duration(path)
        elif estimate:
            result[scene.id] = len(scene.narration) / ESTIMATED_CHARS_PER_SECOND
        else:
            raise SystemExit(f"render: no clip for {scene.id}; run `render.py tts` first")
    return result


def scene_url(slot: Slot, clip: float, fmt: str) -> str:
    query = urlencode(
        {
            "duration": f"{slot.length:.3f}",
            "speech": f"{slot.speech_at - slot.start:.3f}",
            "clip": f"{clip:.3f}",
            "format": fmt,
            "params": json.dumps(slot.scene.params),
        }
    )
    return f"{slot.scene.page.resolve().as_uri()}?{query}"


def open_scene(browser, slot: Slot, clip: float, fmt: str, scale: float):
    width, height = VIEWPORTS[fmt]
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(scene_url(slot, clip, fmt))
    page.wait_for_function("window.JB && window.JB.isReady", timeout=30_000)
    if errors:
        sys.exit(f"render: scene {slot.scene.id} threw: {errors[0]}")
    return page


def render_slot(
    browser, slot: Slot, clip: float, fmt: str, fps: int, target: Path, scale: float = 1.0
) -> None:
    page = open_scene(browser, slot, clip, fmt, scale)
    frames = round(slot.length * fps)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoder = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "image2pipe",
            "-framerate",
            str(fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            target,
        ],
        stdin=subprocess.PIPE,
    )
    assert encoder.stdin is not None
    for index in range(frames):
        page.evaluate("t => JB.seek(t)", index / fps)
        # JPEG at this quality is indistinguishable after H.264, and Chrome
        # encodes it several times faster than PNG.
        encoder.stdin.write(page.screenshot(type="jpeg", quality=95))
        if index % (fps * 5) == 0:
            print(f"  {slot.scene.id}: {index}/{frames}", flush=True)
    encoder.stdin.close()
    if encoder.wait() != 0:
        sys.exit(f"render: ffmpeg failed on {slot.scene.id}")
    page.close()


def assemble(episode: Episode, slots: Sequence[Slot], parts: Sequence[Path], target: Path) -> None:
    total = total_length(slots)
    args = ["ffmpeg", "-v", "error", "-y"]
    for part in parts:
        args += ["-i", part]
    for slot in slots:
        args += ["-i", clip_path(episode, slot.scene)]
    chains = []
    video = "[0:v]"
    for index, slot in enumerate(slots[1:], start=1):
        label = f"[v{index}]"
        chains.append(
            f"{video}[{index}:v]xfade=transition=fade:duration={episode.fade}:offset={slot.start:.3f}{label}"
        )
        video = label
    chains.append(f"{video}fade=t=in:st=0:d=0.5,fade=t=out:st={total - 0.7:.3f}:d=0.7[vout]")
    audio = []
    for index, slot in enumerate(slots):
        delay = round(slot.speech_at * 1000)
        chains.append(f"[{len(parts) + index}:a]adelay={delay}|{delay}[a{index}]")
        audio.append(f"[a{index}]")
    chains.append(
        f"{''.join(audio)}amix=inputs={len(audio)}:normalize=0,apad,"
        f"atrim=0:{total:.3f},loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
    )
    args += [
        "-filter_complex",
        ";".join(chains),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(episode.fps),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "-t",
        f"{total:.3f}",
        target,
    ]
    subprocess.run(args, check=True)


def command_tts(episode: Episode, args: argparse.Namespace) -> None:
    ensure_clips(episode, episode.scenes, dry_run=args.dry_run)


def command_preview(episode: Episode, args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    scene = next((s for s in episode.scenes if s.id == args.scene), None)
    if scene is None:
        sys.exit(
            f"render: no scene {args.scene}; scenes: {', '.join(s.id for s in episode.scenes)}"
        )
    clip = durations(episode, [scene], estimate=True)
    slot = timeline([scene], clip, pad=episode.pad, fade=0.0)[0]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chrome(), args=["--no-sandbox"])
        if args.at is not None:
            page = open_scene(browser, slot, clip[scene.id], args.format, args.scale)
            page.evaluate("t => JB.seek(t)", args.at)
            target = OUT / "preview" / f"{scene.id}-{args.format}-{args.at:g}s.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target))
        else:
            target = OUT / "preview" / f"{scene.id}-{args.format}.mp4"
            render_slot(browser, slot, clip[scene.id], args.format, args.fps, target, args.scale)
        browser.close()
    print(target)


def part_key(episode: Episode, slot: Slot, clip: float, fmt: str) -> str:
    """What a rendered scene depends on: its page, the shared runtime, the
    episode's assets, its timing and its params. Not the news crops a page
    may show from website/assets/; render with --fresh after changing one."""
    digest = hashlib.sha256()
    files = [slot.scene.page, *sorted((HERE / "lib").iterdir())]
    files += sorted((slot.scene.page.parent.parent / "assets").iterdir())
    for path in files:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    timing = (
        fmt,
        episode.fps,
        round(slot.length, 3),
        round(slot.speech_at - slot.start, 3),
        round(clip, 3),
        json.dumps(slot.scene.params, sort_keys=True),
    )
    digest.update(repr(timing).encode())
    return digest.hexdigest()[:16]


def _render_part(job: tuple[Slot, float, str, int, Path]) -> str:
    """One scene in its own process with its own Chrome (fork: Playwright is
    never started in the parent)."""
    from playwright.sync_api import sync_playwright

    slot, clip, fmt, fps, target = job
    partial = target.with_suffix(".part.mp4")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chrome(), args=["--no-sandbox"])
        render_slot(browser, slot, clip, fmt, fps, partial)
        browser.close()
    partial.replace(target)
    return slot.scene.id


def command_render(episode: Episode, args: argparse.Namespace) -> None:
    scenes = episode.for_format(args.format)
    ensure_clips(episode, scenes, dry_run=False)
    clips = durations(episode, scenes)
    slots = timeline(scenes, clips, pad=episode.pad, fade=episode.fade)
    parts_dir = CACHE / "parts" / args.format
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts = [
        parts_dir / f"{s.scene.id}-{part_key(episode, s, clips[s.scene.id], args.format)}.mp4"
        for s in slots
    ]
    jobs = [
        (slot, clips[slot.scene.id], args.format, episode.fps, part)
        for slot, part in zip(slots, parts, strict=True)
        if args.fresh or not part.is_file()
    ]
    print(f"render: {len(slots) - len(jobs)} scenes unchanged, {len(jobs)} to render", flush=True)
    if jobs:
        workers = max(1, min(args.jobs, len(jobs)))
        with multiprocessing.get_context("fork").Pool(workers) as pool:
            for scene_id in pool.imap_unordered(_render_part, jobs):
                print(f"render: {scene_id} done", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"{args.format}.mp4"
    assemble(episode, slots, parts, target)
    (OUT / f"{args.format}.srt").write_text(captions(slots, clips), encoding="utf-8")
    print(f"{target} ({total_length(slots):.1f}s)")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--episode", type=Path, default=DEFAULT_EPISODE)
    commands = parser.add_subparsers(dest="command", required=True)
    tts = commands.add_parser("tts", help="generate missing narration clips")
    tts.add_argument("--dry-run", action="store_true", help="count, call nothing")
    preview = commands.add_parser("preview", help="render one scene")
    preview.add_argument("scene")
    preview.add_argument("--format", choices=sorted(VIEWPORTS), default="main")
    preview.add_argument("--at", type=float, help="a single frame at this scene time, as PNG")
    preview.add_argument("--fps", type=int, default=15)
    preview.add_argument("--scale", type=float, default=0.5)
    render = commands.add_parser("render", help="render a whole format")
    render.add_argument("format", choices=sorted(VIEWPORTS))
    render.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    render.add_argument("--fresh", action="store_true", help="re-render unchanged scenes too")
    args = parser.parse_args(argv)
    try:
        episode = load_episode(args.episode / "script.yaml")
    except (OSError, ValueError) as error:
        sys.exit(f"render: {error}")
    {"tts": command_tts, "preview": command_preview, "render": command_render}[args.command](
        episode, args
    )


if __name__ == "__main__":
    main()
