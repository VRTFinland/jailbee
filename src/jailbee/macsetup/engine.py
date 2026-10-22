"""The resumable step engine behind `jailbee mac setup`.

Each `Step` has a *fingerprint*: a string that changes when the step's inputs
change. A step is skipped while its recorded fingerprint matches, so running
setup again is also the upgrade path — only what moved is redone. State is saved
after every step, so a failure or Ctrl-C resumes where it stopped.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from jailbee.mac_container import AppleContainer

_SCHEMA = 1
RERUN_HINT = 'Re-run "jailbee mac setup" to continue.'


class SetupError(Exception):
    """A setup step cannot continue. `remedy` says what the user should do next."""

    def __init__(self, message: str, remedy: str | None = None, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.remedy = remedy
        self.exit_code = exit_code


@dataclass
class SetupOptions:
    mode: Literal["mac-repos", "isolated"] = "mac-repos"
    machine: str = "jailbee"
    cpus: int | None = None
    memory: str | None = None
    gui: bool = True
    assume_yes: bool = False


@dataclass
class MacPaths:
    config_dir: Path
    state_file: Path
    kernels_dir: Path
    ssh_dir: Path

    @classmethod
    def default(cls) -> MacPaths:
        config_dir = Path.home() / ".config" / "jailbee" / "mac"
        return cls(
            config_dir=config_dir,
            state_file=config_dir / "setup-state.json",
            kernels_dir=Path.home() / "Library" / "Application Support" / "jailbee" / "kernels",
            ssh_dir=config_dir / "ssh",
        )


@dataclass
class SetupState:
    options: SetupOptions
    done: dict[str, str] = field(default_factory=dict)
    facts: dict[str, str] = field(default_factory=dict)


def load_state(path: Path) -> SetupState | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        return None
    return SetupState(
        options=SetupOptions(**raw.get("options", {})),
        done=dict(raw.get("done", {})),
        facts=dict(raw.get("facts", {})),
    )


def save_state(path: Path, state: SetupState) -> None:
    """Write the state atomically: a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = {"schema": _SCHEMA, **asdict(state)}
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


@dataclass
class SetupContext:
    ac: AppleContainer
    state: SetupState
    paths: MacPaths
    interactive: bool
    confirm: Callable[[str], bool]
    log: Callable[[str], None]


class Step(Protocol):
    id: str
    title: str

    def fingerprint(self, ctx: SetupContext) -> str: ...

    def run(self, ctx: SetupContext) -> None: ...


def ask(ctx: SetupContext, question: str, command: str) -> bool:
    """Confirm a change that touches state beyond jailbee's own (never silent)."""
    if ctx.state.options.assume_yes:
        return True
    if not ctx.interactive:
        raise SetupError(
            question,
            remedy=f"Run it yourself: {command}  (or re-run with --yes to allow it)",
            exit_code=2,
        )
    return ctx.confirm(question)


def _report_failure(ctx: SetupContext, message: str, remedy: str | None) -> None:
    ctx.log(message)
    if remedy:
        ctx.log(remedy)
    ctx.log(RERUN_HINT)


def run_steps(ctx: SetupContext, steps: Sequence[Step]) -> int:
    """Run `steps` in order, skipping the ones whose fingerprint is unchanged."""
    total = len(steps)
    for number, step in enumerate(steps, start=1):
        label = f"[{number}/{total}] {step.title}"
        try:
            fingerprint = step.fingerprint(ctx)
            if ctx.state.done.get(step.id) == fingerprint:
                ctx.log(f"{label}: up to date")
                continue
            ctx.log(f"{label} ...")
            step.run(ctx)
        except SetupError as exc:
            _report_failure(ctx, str(exc), exc.remedy)
            return exc.exit_code
        except KeyboardInterrupt:
            _report_failure(ctx, "Interrupted.", None)
            return 130
        ctx.state.done[step.id] = fingerprint
        save_state(ctx.paths.state_file, ctx.state)
    return 0
