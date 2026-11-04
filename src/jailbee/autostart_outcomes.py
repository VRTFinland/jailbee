"""Best-effort persistent last outcome of each autostart step."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from jailbee import db


@dataclass(frozen=True)
class StepOutcome:
    state: Literal["start", "ok", "fail"]
    at: str


Outcomes = dict[str, dict[str, dict[str, StepOutcome]]]


def outcomes_path(container: str) -> Path:
    return db.state_dir() / "autostart-last" / f"{container}.json"


def read(container: str) -> Outcomes:
    try:
        raw = json.loads(outcomes_path(container).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: Outcomes = {}
    for trigger, stages in raw.items():
        if not isinstance(trigger, str) or not isinstance(stages, dict):
            continue
        for stage, steps in stages.items():
            if not isinstance(stage, str) or not isinstance(steps, dict):
                continue
            for step, entry in steps.items():
                if not isinstance(step, str) or not isinstance(entry, dict):
                    continue
                state, at = entry.get("state"), entry.get("at")
                if state not in ("start", "ok", "fail") or not isinstance(at, str):
                    continue
                result.setdefault(trigger, {}).setdefault(stage, {})[step] = StepOutcome(
                    cast(Literal["start", "ok", "fail"], state), at
                )
    return result


def record(container: str, trigger: str, stage: str, step: str, state: str, *, now: datetime | None = None) -> None:
    if state not in ("start", "ok", "fail"):
        return
    path = outcomes_path(container)
    tmp: str | None = None
    try:
        data = read(container)
        data.setdefault(trigger, {}).setdefault(stage, {})[step] = StepOutcome(
            cast(Literal["start", "ok", "fail"], state), (now or datetime.now(timezone.utc)).isoformat()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as fh:
            tmp = fh.name
            json.dump({t: {s: {k: asdict(v) for k, v in steps.items()} for s, steps in stages.items()} for t, stages in data.items()}, fh)
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def failed_steps(outcomes: Outcomes) -> list[tuple[str, str, str]]:
    return [(trigger, stage, step) for trigger, stages in outcomes.items() for stage, steps in stages.items() for step, outcome in steps.items() if outcome.state in ("start", "fail")]


def failed_count(container: str) -> int:
    return len(failed_steps(read(container)))


def remove(container: str) -> None:
    try:
        outcomes_path(container).unlink(missing_ok=True)
    except OSError:
        pass
