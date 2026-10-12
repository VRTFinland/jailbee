"""The narrated-video pipeline's pure parts: episodes, the timeline, captions.

No network, no browser and no ffmpeg in here, so the project's test suite can
import it (it has PyYAML but not Playwright). `render.py` does the I/O.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FORMATS = ("main", "short")
"""`main` is 1920x1080, `short` 1080x1920; see render.py's VIEWPORTS."""

CAPTION_LINE = 42
"""Characters per caption line before it wraps onto a second one."""


class MissingKeyError(Exception):
    """No OpenAI API key in the environment or in the key file."""


@dataclass(frozen=True)
class TTS:
    model: str
    voice: str
    instructions: str


@dataclass(frozen=True)
class Scene:
    id: str
    page: Path
    narration: str
    formats: tuple[str, ...]
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Episode:
    title: str
    tts: TTS
    scenes: tuple[Scene, ...]
    fps: int = 30
    pad: float = 0.6
    fade: float = 0.4

    def for_format(self, name: str) -> list[Scene]:
        return [scene for scene in self.scenes if name in scene.formats]


@dataclass(frozen=True)
class Slot:
    """A scene placed on the timeline: where it starts, how long it shows,
    and when its narration starts."""

    scene: Scene
    start: float
    length: float
    speech_at: float


def load_episode(path: Path) -> Episode:
    """Read and validate an episode's script.yaml; ValueError names the problem."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tts_data = data.get("tts") or {}
    for key in ("model", "voice", "instructions"):
        if not str(tts_data.get(key) or "").strip():
            raise ValueError(f"{path}: tts.{key} is required")
    tts = TTS(
        model=str(tts_data["model"]),
        voice=str(tts_data["voice"]),
        instructions=str(tts_data["instructions"]).strip(),
    )
    scenes: list[Scene] = []
    seen: set[str] = set()
    for raw in data.get("scenes") or []:
        scene_id = str(raw.get("id") or "")
        if not scene_id:
            raise ValueError(f"{path}: a scene has no id")
        if scene_id in seen:
            raise ValueError(f"{path}: duplicate scene id '{scene_id}'")
        seen.add(scene_id)
        page = path.parent / str(raw.get("page") or "")
        if not page.is_file():
            raise ValueError(f"{path}: {scene_id}: page {raw.get('page')} does not exist")
        narration = " ".join(str(raw.get("narration") or "").split())
        if not narration:
            raise ValueError(f"{path}: {scene_id}: narration is empty")
        formats = tuple(str(f) for f in raw.get("formats") or [])
        if not formats:
            raise ValueError(f"{path}: {scene_id}: in no format")
        for name in formats:
            if name not in FORMATS:
                raise ValueError(f"{path}: {scene_id}: unknown format '{name}'")
        scenes.append(Scene(scene_id, page, narration, formats, dict(raw.get("params") or {})))
    if not scenes:
        raise ValueError(f"{path}: no scenes")
    return Episode(
        title=str(data.get("title") or path.parent.name),
        tts=tts,
        scenes=tuple(scenes),
        fps=int(data.get("fps", 30)),
        pad=float(data.get("pad", 0.6)),
        fade=float(data.get("fade", 0.4)),
    )


def cache_key(tts: TTS, text: str) -> str:
    """The clip's cache name: changes with anything the audio depends on."""
    digest = hashlib.sha256()
    for part in (tts.model, tts.voice, tts.instructions, text):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
    return digest.hexdigest()


def key_file(home: Path) -> Path:
    return home / ".config" / "openai" / "api-key"


def api_key(environ: Mapping[str, str] | None = None, home: Path | None = None) -> str:
    """OPENAI_API_KEY, else ~/.config/openai/api-key. Never logged."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    value = environ.get("OPENAI_API_KEY", "").strip()
    if value:
        return value
    path = key_file(home)
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise MissingKeyError(f"no OpenAI API key: set OPENAI_API_KEY or write it to {path}")


def timeline(
    scenes: Sequence[Scene], durations: Mapping[str, float], *, pad: float, fade: float
) -> list[Slot]:
    """Scenes end to end, each `pad` of silence either side of its clip, the
    next starting `fade` before the previous ends so the two can cross-fade."""
    slots: list[Slot] = []
    start = 0.0
    for scene in scenes:
        if scene.id not in durations:
            raise KeyError(f"no narration clip for scene {scene.id}")
        length = durations[scene.id] + 2 * pad
        slots.append(Slot(scene, start, length, start + pad))
        start += length - fade
    return slots


def total_length(slots: Sequence[Slot]) -> float:
    return slots[-1].start + slots[-1].length if slots else 0.0


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def wrap_caption(text: str) -> str:
    """One line, or two balanced lines split at the space nearest the middle."""
    text = " ".join(text.split())
    if len(text) <= CAPTION_LINE:
        return text
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    if not spaces:
        return text
    middle = min(spaces, key=lambda i: abs(i - len(text) / 2))
    return f"{text[:middle]}\n{text[middle + 1 :]}"


def _stamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def captions(slots: Sequence[Slot], durations: Mapping[str, float]) -> str:
    """SRT: one cue per sentence, each sentence's share of its clip in
    proportion to its length."""
    cues: list[str] = []
    for slot in slots:
        sentences = [s for s in _SENTENCE.split(slot.scene.narration) if s]
        total = sum(len(s) for s in sentences)
        at = slot.speech_at
        for sentence in sentences:
            end = at + durations[slot.scene.id] * len(sentence) / total
            cues.append(
                f"{len(cues) + 1}\n{_stamp(at)} --> {_stamp(end)}\n{wrap_caption(sentence)}\n"
            )
            at = end
    return "\n".join(cues)
