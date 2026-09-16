"""What `jailbee autostart status` shows and what the stop/net guards ask.

Three concerns, all keyed on the one job row a detached autostart run owns
(`JOB_AUTOSTART`, written by the `_autostart-worker` supervisor or adopted
in-process by a background worker at its detach boundary):

* :func:`live_run` — "is a worker still running this container's stages?",
  the question `stop` refuses on and `net` warns on;
* :func:`step_views` — the progress log turned into one row per step,
  grouped by stage;
* the field specs and header/note text that render them.

No subprocess, and the only DB access is :func:`live_run`'s row lookup —
everything else is a pure function of (row, entries, liveness), so
`cli.py` stays argument parsing plus delegation.

**Liveness decides what a dangling ``start`` means.** A step's ``"start"``
is not guaranteed a terminal entry: the executor's abort handler interrupts
in-flight steps without routing them through `autostart._finish_step`, and a
SIGTERM'd supervisor writes nothing at all. Under a live worker such a step
is genuinely running; under a dead one it was cut off and will never be
resolved, so it renders as ``interrupted`` rather than as forever-running.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee import table_format

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.autostart_progress import ProgressEntry
    from jailbee.config import Config
    from jailbee.db.models import BackgroundJob

STATE_RUNNING = "running"
STATE_INTERRUPTED = "interrupted"
STATE_OK = "ok"
STATE_FAILED = "failed"

_STATE_STYLE = {
    STATE_OK: "green",
    STATE_FAILED: "red",
    STATE_RUNNING: "yellow",
    STATE_INTERRUPTED: "red",
}


@dataclass(frozen=True)
class StepView:
    """One step of a run, as `jailbee autostart status` renders it."""

    stage: str
    step: str
    state: str
    at: str
    # Whether this is the first step of its stage, i.e. the row that carries
    # the stage name in the table. Grouping is presentation only: the JSON
    # value keeps the stage on every row.
    first_in_stage: bool


FieldSpecStep = table_format.FieldSpec[StepView]


def autostart_row(cfg: Config, name: str) -> BackgroundJob | None:
    """The container's autostart job row, live or dead.

    What `status` and `cancel` look up: unlike :func:`live_run` a finished-but-
    unswept row is still worth reporting — it is where the run stopped — and
    `cancel` needs it to explain that the worker is already gone.

    ``None`` for a row of any other kind: since a background `new` / boot
    worker re-kinds its own row at the detach boundary
    (`background.adopt_autostart`), a `JOB_AUTOSTART` row is exactly the run
    these commands are about, on both the spawned-supervisor and the
    in-process path — while a `create` or `boot` row belongs to a worker that
    has not reached its stages yet and has recorded no progress.
    """
    from jailbee import background
    from jailbee.lifecycle import lookup_background_job

    row = lookup_background_job(cfg, name)
    if row is None or row.op_kind != background.JOB_AUTOSTART:
        return None
    return row


def is_live(row: BackgroundJob) -> bool:
    """Whether a worker is still running this row's stages.

    The feature's one definition of "live", so `status` and the two guards can
    never disagree about the same row: `status` resolves an unterminated step
    by it, `live_run` refuses/warns by it. `background.clearable` negated —
    which also counts a row whose phase reached a terminal one as done, even
    in the moment before its pid disappears. That is what a reader wants
    either way: the run is over, so a step it never finished was interrupted,
    and there is nothing left to guard against.
    """
    from jailbee import background

    return not background.clearable(row.phase, row.pid)


def live_run(cfg: Config, name: str) -> BackgroundJob | None:
    """The container's autostart job row while a worker is still running it.

    What the `stop` and `net` guards ask: ``None`` unless a worker is right
    now running this container's stages — no row, another kind of job, or a
    worker that is gone all answer "nothing in flight".
    """
    row = autostart_row(cfg, name)
    if row is None or not is_live(row):
        return None
    return row


def signal_worker(pid: int) -> None:
    """Ask the supervisor to stop.

    A seam, so a test can assert the signal without patching `os.kill`, which
    is process-wide: `background.worker_alive` probes with the same call, and
    a mocked `os.kill` would make every dead pid look alive.
    """
    os.kill(pid, signal.SIGTERM)


def step_views(entries: Sequence[ProgressEntry], *, live: bool) -> list[StepView]:
    """One row per step, stages in the order they were entered.

    The progress file is an append-only log, so a step appears once per state
    change; the last entry wins and carries the timestamp shown. A step left
    at ``"start"`` resolves through ``live`` — see the module docstring.
    """
    # stage -> step -> (state, at). Both dicts keep insertion order, which is
    # the order the run reached them: that ordering *is* the grouping.
    by_stage: dict[str, dict[str, tuple[str, str]]] = {}
    for entry in entries:
        steps = by_stage.setdefault(entry.stage, {})
        steps[entry.step] = (entry.state, entry.at)

    out: list[StepView] = []
    for stage, steps in by_stage.items():
        for i, (step, (state, at)) in enumerate(steps.items()):
            out.append(
                StepView(
                    stage=stage,
                    step=step,
                    state=_resolve_state(state, live=live),
                    at=at,
                    first_in_stage=i == 0,
                )
            )
    return out


def _resolve_state(state: str, *, live: bool) -> str:
    """The rendered state of a step whose last logged entry is ``state``."""
    if state == "ok":
        return STATE_OK
    if state == "fail":
        return STATE_FAILED
    # "start", or anything a future writer adds: unterminated.
    return STATE_RUNNING if live else STATE_INTERRUPTED


def _state_cell(view: StepView) -> str:
    return f"[{_STATE_STYLE[view.state]}]{view.state}[/]"


def step_field_specs() -> list[FieldSpecStep]:
    """Columns for `jailbee autostart status`, in `jailbee job ls`'s idiom."""
    return [
        table_format.FieldSpec(
            name="stage",
            header="STAGE",
            cell=lambda v: v.stage if v.first_in_stage else "",
            json=lambda v: v.stage,
        ),
        table_format.FieldSpec(
            name="step",
            header="STEP",
            cell=lambda v: v.step,
            json=lambda v: v.step,
        ),
        table_format.FieldSpec(
            name="state",
            header="STATE",
            cell=_state_cell,
            json=lambda v: v.state,
        ),
        table_format.FieldSpec(
            name="at",
            header="AT",
            cell=lambda v: v.at,
            json=lambda v: v.at,
        ),
    ]


def header(short: str, row: BackgroundJob) -> str:
    """The line above the table: which stage the run is on, and whose pid.

    The state text is `background.job_label`, the same source `jailbee ls` and
    `jailbee job ls` render — so a dead worker reads ``deps (worker gone)``
    here exactly as it does there, and the two can never disagree.
    """
    from jailbee import background

    label = background.job_label(row.phase, row.pid, kind=row.op_kind)
    return f"Autostart for '{short}': {label} (pid {row.pid})"


def notes(short: str, views: Sequence[StepView], *, live: bool) -> list[str]:
    """The advisory lines under the table — none while the worker lives.

    A run whose worker is gone is over however it ended, so the row it leaves
    behind is the user's to drop; an interrupted step additionally needs
    saying out loud, since nothing will ever resolve it.
    """
    if live:
        return []
    lines = []
    if any(v.state == STATE_INTERRUPTED for v in views):
        lines.append("  Interrupted steps were cut off when the worker died — they never report.")
    lines.append(f"  Drop the record:  jailbee job clear {short}")
    return lines


def running_label(row: BackgroundJob) -> str:
    """How the guards name a run in flight: ``autostart:deps, pid 1234``."""
    from jailbee import background

    return f"{background.job_label(row.phase, row.pid, kind=row.op_kind)}, pid {row.pid}"
