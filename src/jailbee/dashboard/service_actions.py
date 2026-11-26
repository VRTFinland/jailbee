"""Run terminal-dashboard actions through the shared state service."""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from jailbee.dashboard.commands import Transport
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.notifications import NotificationQueue
from jailbee.dashboard.runner import ActionOutcome, ActionRun, RunProgress, RunResult, _TailBuffer
from jailbee.state_service import StateServiceUnavailable
from jailbee.state_service.client import DISCONNECTED
from jailbee.state_service.protocol import ActionEntry, RunAction

POLL_SECONDS = 0.05
RECONNECT_TIMEOUT_SECONDS = 30.0


class _NoProgress:
    """Progress adapter for direct actions with no notification queue."""

    def entry_id(self, _index: int) -> int:
        return secrets.randbits(63) or 1

    def output(self, _index: int, _chunk: str) -> None:
        pass

    def question(self, _index: int, _asking: bool) -> None:
        pass


class ActionClient(Protocol):
    """The action-facing part of a state client."""

    @property
    def dashboard_id(self) -> str | None: ...

    def run_action(self, request: RunAction) -> None: ...

    def action_entries(self) -> dict[int, ActionEntry]: ...

    def status(self) -> str | None: ...

    def action_connection(self) -> int | None: ...

    def action_rejection(self, entry_id: int) -> str | None: ...


def run_actions(
    client: ActionClient,
    runs: Sequence[ActionRun],
    progress: RunProgress | None = None,
    *,
    groups: Sequence[RepoGroup],
    transport: Transport,
    queue: NotificationQueue | None = None,
) -> ActionOutcome:
    """Submit sequential actions and wait for their shared snapshots to finish.

    ``progress`` supplies the stable queue ids; no local runner or event drain is
    used. Output snapshots are relayed as new suffixes, and the final outcome
    retains the runner's batch-stop and refusal semantics.
    """
    if progress is None:
        progress = _NoProgress()
    outcome_text = _TailBuffer()
    outputs: list[str] = []
    results: list[RunResult] = []
    for index, item in enumerate(runs):
        entry_id = progress.entry_id(index)
        root = str(item.cwd.resolve())
        group = next(
            (
                g
                for g in groups
                if g.repo_root and Path(g.repo_root).resolve() == item.cwd.resolve()
            ),
            None,
        )
        prefix = group.prefix if group is not None else ""
        queued = queue.get(entry_id) if queue is not None else None
        target = (
            queued.target if queued is not None else _target(item.argv, group) if group else root
        )
        request = RunAction(
            entry_id=entry_id,
            argv=list(item.argv),
            cwd=str(item.cwd),
            target=target,
            critical=bool(queued.critical) if queued is not None else False,
            origin=client.dashboard_id or "",
            transport=transport,
            repo_root=root,
            prefix=prefix or root,
        )
        if entry_id in client.action_entries():
            results.append(RunResult(error="invalid or duplicate action id"))
            outputs.append("")
            break
        last_output = ""
        sent = False
        sent_generation: int | None = None
        acknowledged = False
        uncertain_generation: int | None = None
        disconnected_at: float | None = None
        while True:
            generation = client.action_connection()
            snapshot = client.action_entries().get(entry_id)
            rejection = client.action_rejection(entry_id)
            if snapshot is not None:
                acknowledged = True
                sent = True
                uncertain_generation = None
            elif rejection is not None:
                results.append(RunResult(refused=rejection))
                outputs.append("")
                return ActionOutcome(results, outcome_text.text(), outputs)
            if snapshot is not None and snapshot.finished:
                generation = generation or sent_generation or 1
            if generation is None or (
                uncertain_generation is not None and generation == uncertain_generation
            ):
                if disconnected_at is None:
                    disconnected_at = time.monotonic()
                if time.monotonic() - disconnected_at >= RECONNECT_TIMEOUT_SECONDS:
                    results.append(RunResult(error=client.status() or DISCONNECTED))
                    outputs.append(last_output)
                    outcome_text.append(last_output)
                    return ActionOutcome(results, outcome_text.text(), outputs)
                time.sleep(POLL_SECONDS)
                continue
            disconnected_at = None
            if acknowledged and snapshot is None and sent_generation != generation:
                results.append(RunResult(error="action missing after state service reconnect"))
                outputs.append(last_output)
                outcome_text.append(last_output)
                return ActionOutcome(results, outcome_text.text(), outputs)
            if sent and not acknowledged and sent_generation != generation:
                sent = False
            if not sent:
                try:
                    client.run_action(request)
                    sent = True
                    sent_generation = generation
                except (OSError, StateServiceUnavailable):
                    uncertain_generation = generation
                continue
            if snapshot is None:
                time.sleep(POLL_SECONDS)
                continue
            text = snapshot.output
            if text.startswith(last_output):
                chunk = text[len(last_output) :]
            else:
                chunk = text
            if chunk:
                replace_output = getattr(progress, "replace_output", None)
                if replace_output is not None:
                    replace_output(index, text)
                else:
                    progress.output(index, chunk)
            last_output = text
            if snapshot.finished:
                results.append(
                    RunResult(
                        status=snapshot.status,
                        refused=snapshot.refused,
                        error=snapshot.error,
                        cancelled=snapshot.cancelled,
                        declined=snapshot.declined,
                    )
                )
                outputs.append(text)
                outcome_text.append(text)
                if snapshot.refused is not None or snapshot.error is not None:
                    return ActionOutcome(results, outcome_text.text(), outputs)
                break
            time.sleep(POLL_SECONDS)
    return ActionOutcome(results, outcome_text.text(), outputs)


def _target(argv: Sequence[str], group: RepoGroup) -> str:
    containers = {container.name for container in group.containers}
    return next((word for word in argv if word in containers), group.prefix)
