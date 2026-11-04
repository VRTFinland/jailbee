"""Pure autostart planning: legacy normalization, agent placement, detach split.

Deliberately import-light — `jailbee.config` and nothing else. Every
decision about *what runs, in what order, and on which side of the attach
boundary* is made here, so it is testable without a mocked Incus. The
executor (`autostart.py`) only carries out what this module decides.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import Field

from jailbee.config import Autostart, AutostartChain, AutostartStage, AutostartStep

SERVICES_STAGE = "services"
"""Reserved on_start stage name for starting selected services."""


class GeneratedServicesStage(AutostartStage):
    """Executor marker for explicit slots and the implicit fallback.

    Type-based dispatch keeps legacy flat steps named services ordinary steps.
    ``services`` names a subset; None means all selected services except
    ``excluded_services`` (the services explicitly placed in other slots).
    """

    progress_stage: str = Field(
        default=SERVICES_STAGE,
        description="Unique progress label for this generated services slot.",
    )
    excluded_services: list[str] = Field(
        default_factory=list,
        description="Services explicitly placed in other slots, excluded from the remainder.",
    )


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


@dataclass(frozen=True)
class RerunTarget:
    trigger: AutostartTrigger
    stage: str
    step: str | None


class RerunSelectionError(ValueError):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def rerunnable_stages(autostart: Autostart, trigger: AutostartTrigger) -> list[AutostartStage]:
    return [
        stage
        for stage in normalize_stages(getattr(autostart, trigger.value))
        if stage.stage != AGENTS_STAGE
        and not isinstance(stage, GeneratedServicesStage)
        and not stage.is_services_slot()
    ]


def resolve_rerun(
    autostart: Autostart,
    *,
    stages: Sequence[str],
    steps: Sequence[str],
    trigger: AutostartTrigger | None,
) -> list[RerunTarget]:
    available = {t: rerunnable_stages(autostart, t) for t in TRIGGER_ORDER}
    triggers = (trigger,) if trigger is not None else TRIGGER_ORDER
    selections: list[tuple[str, str | None]] = [(name, None) for name in stages]
    for value in steps:
        parts = value.split("/")
        if len(parts) != 2 or not all(parts):
            raise RerunSelectionError(f"Invalid step '{value}'; expected STAGE/STEP", 2)
        selections.append((parts[0], parts[1]))

    result: list[RerunTarget] = []
    for name, step in selections:
        matches = [(t, s) for t in triggers for s in available[t] if s.stage == name]
        if not matches:
            reserved = name == AGENTS_STAGE or any(
                name in (key, s.stage)
                and (s.is_services_slot() or isinstance(s, GeneratedServicesStage))
                for t in triggers
                for entries in [normalize_stages(getattr(autostart, t.value))]
                for s, key in zip(entries, stage_keys(entries), strict=True)
            )
            if reserved:
                raise RerunSelectionError(
                    f"{name} is not re-runnable; use `jailbee service` for services", 1
                )
            candidates = sorted({s.stage for t in triggers for s in available[t]})
            raise RerunSelectionError(
                f"Unknown stage '{name}'; candidates: {', '.join(candidates)}", 2
            )
        if len({t for t, _ in matches}) > 1:
            raise RerunSelectionError(f"Stage '{name}' is ambiguous: on_create, on_start", 2)
        matched_trigger, stage = matches[0]
        if step is not None:
            candidates = [item.name for chain in stage.all_chains() for item in chain.steps]
            if step not in candidates:
                raise RerunSelectionError(
                    f"Unknown step '{name}/{step}'; candidates: {', '.join(candidates)}", 2
                )
        target = RerunTarget(matched_trigger, name, step)
        if target not in result:
            result.append(target)
    return result


def rerun_block(autostart: Autostart, targets: Sequence[RerunTarget]) -> Autostart:
    selected: dict[str, object] = {"profiles": {}, "default_profile": None}
    for trigger in TRIGGER_ORDER:
        kept: list[AutostartStage] = []
        for stage in rerunnable_stages(autostart, trigger):
            matching = [t for t in targets if t.trigger == trigger and t.stage == stage.stage]
            if not matching:
                continue
            updates: dict[str, object] = {"detach": False}
            if not any(t.step is None for t in matching):
                names = {t.step for t in matching}
                chains = []
                for chain in stage.all_chains():
                    steps = [step for step in chain.steps if step.name in names]
                    if steps:
                        chains.append(chain.model_copy(update={"steps": steps}))
                if not chains:
                    continue
                updates.update({"chains": chains, "steps": []})
            kept.append(stage.model_copy(update=updates))
        selected[trigger.value] = kept
    return autostart.model_copy(update=selected)


def stage_keys(stages: list[AutostartStage]) -> list[str]:
    """Keep ordinary stage names; identify reserved slots by occurrence.

    Slot numbering is independent of ordinary stages named services. The same
    labels identify branch diffs and progress rows across detach hand-offs.
    """
    occurrence = 0
    keys: list[str] = []
    for stage in stages:
        if stage.is_services_slot():
            occurrence += 1
            keys.append(f"services#{occurrence}")
        else:
            keys.append(stage.stage)
    return keys


def profile_names(autostart: Autostart) -> tuple[str, ...]:
    from jailbee.autostart_profiles import profile_names as names

    return names(autostart)


def select_profile(autostart: Autostart, profile: str | None) -> Autostart:
    """Filter before planning, preserving agents and config order."""
    from jailbee.autostart_profiles import selection

    chosen = selection(autostart, profile)
    if chosen == "all":
        return autostart
    names = set(chosen.stages) | {AGENTS_STAGE}
    return autostart.model_copy(
        update={
            trigger.value: [
                s
                for s in normalize_stages(getattr(autostart, trigger.value))
                if s.stage in names
                or (trigger == AutostartTrigger.ON_START and s.is_services_slot())
            ]
            for trigger in TRIGGER_ORDER
        }
    )


def selected_stage_names(
    autostart: Autostart,
    profile: str | None,
    trigger: AutostartTrigger,
) -> tuple[str, ...]:
    return tuple(
        s.stage
        for s in normalize_stages(getattr(select_profile(autostart, profile), trigger.value))
    )


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
    agents_last: bool = False,
    services_stage: bool = False,
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

    ``agents_last`` is a pending initial prompt: the prompted agent starts on
    its task at once, so the agents stage runs after every other stage of the
    trigger — last of the blocking ones when nothing detaches, else last of
    the detached run — instead of at the attach boundary. An explicit
    ``stage: agents`` is moved there too, keeping its network and mounts; its
    ``detach`` flag stays where the user drew it, on the stage that followed.
    """
    entries = getattr(autostart, trigger.value)
    stages = normalize_stages(entries)
    steps = list(agent_steps)
    is_stage_form = bool(entries) and isinstance(entries[0], AutostartStage)

    service_slots = (
        [s for s in stages if s.is_services_slot()]
        if is_stage_form and trigger == AutostartTrigger.ON_START
        else []
    )
    placed = [name for s in service_slots for name in (s.services or [])]
    if not services_stage:
        for i in range(len(stages) - 1, -1, -1):
            if stages[i] in service_slots:
                dropped = stages.pop(i)
                if dropped.detach and i < len(stages):
                    stages[i] = stages[i].model_copy(update={"detach": True})
    else:
        stages = [
            GeneratedServicesStage.model_construct(
                **s.model_dump(), excluded_services=placed if s.services is None else []
            )
            if s in service_slots
            else s
            for s in stages
        ]

    explicit = [i for i, s in enumerate(stages) if s.stage == AGENTS_STAGE] if is_stage_form else []
    if agents_last and steps:
        agents = _agents_stage(steps)
        if explicit:
            i = explicit[0]
            moved = stages.pop(i)
            if moved.detach and i < len(stages):
                stages[i] = stages[i].model_copy(update={"detach": True})
            agents = moved.model_copy(
                update={
                    "chains": [AutostartChain(name="main", steps=steps)],
                    "steps": [],
                    "detach": False,
                }
            )
        boundary = _boundary(stages, override=override, already_detached=already_detached)
        if boundary == len(stages) and not already_detached:
            boundary += 1  # nothing detaches: the agents stage is the last blocking one
        stages.append(agents)
    elif explicit:
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

    if (
        services_stage
        and trigger == AutostartTrigger.ON_START
        and not any(s.services is None for s in service_slots)
    ):
        if steps:
            if agents_last:
                position = len(stages) - 1
            elif explicit:
                position = explicit[0]
            else:
                position = boundary if already_detached else boundary - 1
        else:
            position = len(stages)
        if position < boundary or (position == boundary == len(stages) and not already_detached):
            boundary += 1
        stages.insert(
            position,
            GeneratedServicesStage.model_construct(
                stage=SERVICES_STAGE,
                network=None,
                mounts=[],
                detach=False,
                chains=[],
                steps=[],
                excluded_services=placed,
            ),
        )

    generated = [s for s in stages if isinstance(s, GeneratedServicesStage)]
    if len(generated) > 1 or any(
        s.stage == SERVICES_STAGE and not isinstance(s, GeneratedServicesStage) for s in stages
    ):
        for s, key in zip(stages, stage_keys(stages), strict=True):
            if isinstance(s, GeneratedServicesStage):
                s.progress_stage = key

    return AutostartPlan(blocking=stages[:boundary], detached=stages[boundary:])
