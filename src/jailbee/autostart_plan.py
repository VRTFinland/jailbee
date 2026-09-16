"""Pure autostart planning: legacy normalization, agent placement, detach split.

Deliberately import-light — `jailbee.config` and nothing else. Every
decision about *what runs, in what order, and on which side of the attach
boundary* is made here, so it is testable without a mocked Incus. The
executor (`autostart.py`) only carries out what this module decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from jailbee.config import Autostart, AutostartChain, AutostartStage, AutostartStep

AGENTS_STAGE = "agents"
"""Reserved stage name whose steps jailbee generates from `agents`."""


class AutostartTrigger(Enum):
    ON_CREATE = "on_create"
    ON_START = "on_start"


TRIGGER_ORDER = (AutostartTrigger.ON_CREATE, AutostartTrigger.ON_START)
"""The triggers in the order a container meets them.

Spelled out rather than derived: sorting the enum's *values* would only
work by the accident that ``"on_create" < "on_start"``, and would silently
mis-order the day a third trigger lands. The detached supervisor resumes at
the trigger it was spawned from and runs every later one in full.
"""


@dataclass(frozen=True)
class AutostartPlan:
    """One trigger's stages, split at the attach boundary.

    ``blocking`` runs in the calling process; ``detached`` runs in the
    supervisor. Either may be empty.
    """

    blocking: list[AutostartStage]
    detached: list[AutostartStage]


def normalize_stages(entries: list[AutostartStep] | list[AutostartStage]) -> list[AutostartStage]:
    """Return ``entries`` as stages, converting a legacy flat step list.

    Consecutive steps sharing the same ``network`` collapse into one stage
    carrying that network, named after the first step in the run. Each
    implicit stage holds a single chain, so ordering is unchanged; the only
    observable difference from the old executor is that a run of steps
    sharing a mode switches the profile once rather than once per step.

    Per-step ``mounts`` stay on the step: they remain legal in the flat
    form (deprecated, not removed), and the executor still honours them.
    """
    if not entries:
        return []
    if isinstance(entries[0], AutostartStage):
        # A trigger cannot mix shapes — `Autostart._no_mixed_shapes` rejects it.
        return [e for e in entries if isinstance(e, AutostartStage)]

    stages: list[AutostartStage] = []
    run: list[AutostartStep] = []
    run_network: str | None = None

    def flush() -> None:
        if not run:
            return
        # `model_construct` (not `model_validate`) deliberately bypasses
        # `AutostartStage`'s "no network/mounts on steps" validator: the
        # flat legacy form legitimately carries `network`/`mounts` per step,
        # and this is exactly the conversion that lifts them to the stage
        # level. Do not "fix" this into `model_validate` — it will reject
        # every legacy config that still sets per-step network or mounts.
        stages.append(
            AutostartStage.model_construct(
                stage=run[0].name,
                network=run_network,
                mounts=[],
                detach=False,
                chains=[AutostartChain(name="main", steps=list(run))],
                steps=[],
            )
        )

    for entry in entries:
        assert isinstance(entry, AutostartStep)  # shape guaranteed above
        if run and entry.network == run_network:
            run.append(entry)
            continue
        flush()
        run = [entry]
        run_network = entry.network
    flush()
    return stages


def _agents_stage(agent_steps: list[AutostartStep]) -> AutostartStage:
    return AutostartStage.model_construct(
        stage=AGENTS_STAGE,
        network=None,
        mounts=[],
        detach=False,
        chains=[AutostartChain(name="main", steps=list(agent_steps))],
        steps=[],
    )


def _boundary(
    stages: list[AutostartStage],
    *,
    override: Literal["wait", "no_wait"] | None,
    already_detached: bool,
) -> int:
    """Index of the first detached stage."""
    if already_detached:
        return 0
    if override == "wait":
        return len(stages)
    if override == "no_wait":
        return min(1, len(stages))
    for i, stage in enumerate(stages):
        if stage.detach:
            return i
    return len(stages)


def plan_autostart(
    autostart: Autostart,
    trigger: AutostartTrigger,
    *,
    agent_steps: list[AutostartStep] | tuple[AutostartStep, ...] = (),
    override: Literal["wait", "no_wait"] | None = None,
    already_detached: bool = False,
) -> AutostartPlan:
    """Plan one trigger's run.

    ``agent_steps`` are the generated per-agent launch steps (empty for
    ``ON_CREATE``, and empty whenever no agent has ``autostart: true``).
    They are placed at the *effective* attach boundary — the last thing the
    caller runs before handing over — so the agent window exists by the time
    the user is attached. An explicitly written ``stage: agents`` is filled
    in place instead, wherever the repo put it.

    The reserved ``agents`` slot only exists in the stage form. A legacy
    flat trigger has no such slot — even a step literally named ``agents``
    is just an ordinary step, and the generated agent steps still get their
    own stage — because a flat config's step names are user data, not a
    namespace jailbee reserves.

    ``override`` is the CLI's ``--wait`` / ``--no-wait``.
    ``already_detached`` is how ``ON_START`` learns that ``ON_CREATE``
    already crossed the boundary: once detached, everything after it is.
    """
    entries = getattr(autostart, trigger.value)
    stages = normalize_stages(entries)
    steps = list(agent_steps)
    is_stage_form = bool(entries) and isinstance(entries[0], AutostartStage)

    explicit = [i for i, s in enumerate(stages) if s.stage == AGENTS_STAGE] if is_stage_form else []
    if explicit:
        i = explicit[0]
        if steps:
            # `model_copy` (not `_agents_stage`) so the repo's own stage —
            # its `network`, `mounts`, `detach` — survives filling; only the
            # chains are replaced.
            stages[i] = stages[i].model_copy(
                update={
                    "chains": [AutostartChain(name="main", steps=list(steps))],
                    "steps": [],
                }
            )
            boundary = _boundary(stages, override=override, already_detached=already_detached)
            # The agents stage is the hand-off point: launching it must stay
            # blocking. When its own `detach: true` is what the natural scan
            # found (boundary landed exactly on it), the boundary is *after*
            # it, not on it — unlike an ordinary stage, which starts the
            # detached region it flags.
            if override is None and not already_detached and boundary == i:
                boundary = i + 1
        else:
            dropped = stages[i]
            del stages[i]
            if dropped.detach and i < len(stages):
                # No agent to launch, so the slot disappears — but the user
                # marked it as where detachment begins. Hand that flag to
                # whatever now occupies its position so the boundary they
                # drew doesn't silently vanish.
                stages[i] = stages[i].model_copy(update={"detach": True})
            boundary = _boundary(stages, override=override, already_detached=already_detached)
    else:
        boundary = _boundary(stages, override=override, already_detached=already_detached)
        if steps:
            stages.insert(boundary, _agents_stage(steps))
            if not already_detached:
                boundary += 1

    return AutostartPlan(blocking=stages[:boundary], detached=stages[boundary:])
