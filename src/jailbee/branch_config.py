"""Read and compare a target branch's `autostart` config.

`jailbee new <branch>` clones the target branch's files but has historically
provisioned the container with the *host checkout's* config, so a branch that
changed its startup steps produced a container that fails to start. This module
reads the branch's own `autostart` at the commit about to be cloned, and reports
how it deviates from the host's.

Only `autostart` is taken from the branch. Everything else — mounts, resource
limits, network defaults, host-level keys — stays under host control; a branch
must not be able to silently change how the operator runs containers.

Two questions, two comparisons
------------------------------
These are deliberately separate, because conflating them made an out-of-date
checkout look like a privilege escalation:

1. *"Why does my container run different startup steps than I expected?"* —
   `diff_autostart` against the **host checkout**, rendered by
   `format_deviation`. Informational, always warned about.

2. *"Does this grant privileges the repo has not already granted?"* —
   `assess_escalation` against the **reviewed baseline**
   (`refs/remotes/<upstream_remote>/<default_branch>`), rendered by
   `format_escalation`.
   Only this one can prompt.

The checkout is one arbitrary snapshot of one arbitrary branch: it may be
behind, ahead, or an unrelated feature branch with local edits, so the same
`jailbee new` would prompt one developer and not another. The default branch on
the upstream is what review and CI gate, which makes it the privilege baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.autostart_plan import normalize_stages
from jailbee.config import AutostartStage

if TYPE_CHECKING:
    from jailbee.config import Autostart, AutostartStep, Config

_TRIGGERS = ("on_create", "on_start")

# How a unit is named in the rendered diff and in the privilege verdict.
# Two namespaces, two bracket shapes, on purpose: `normalize_stages` names an
# implicit stage after its *first step*, so a flat block's stage names and its
# step names routinely coincide. `[` and `<` sit at the same offset in every
# key, so no step key can ever equal a stage key however the two are spelled.
_STEP_KEY = "{trigger}[{name}]"
_STAGE_KEY = "{trigger}<{name}>"

# `stage` is the key itself; `chains`/`steps` hold the steps, which are diffed
# at the step level. Everything else on an `AutostartStage` is stage-owned and
# compared here, enumerated (not listed) so a new field forces a decision
# rather than being silently ignored — as at the step level.
_STAGE_STRUCTURE_FIELDS = frozenset({"stage", "chains", "steps"})


def _config_text_at_ref(repo_root: Path, ref: str) -> tuple[str, str] | None:
    """Return (text, rel_path) for the first config `ref` carries, else None.

    Mirrors `paths.REPO_CONFIG_DIRS` preference order against a git ref,
    which cannot be probed with `Path.is_file`.
    """
    from jailbee.git import show_file_at_ref
    from jailbee.paths import REPO_CONFIG_DIRS

    for name in REPO_CONFIG_DIRS:
        rel = f"{name}/config.yaml"
        text = show_file_at_ref(repo_root, ref, rel)
        if text is not None:
            return text, rel
    return None


@dataclass(frozen=True)
class StepChange:
    """One step — or one stage — that exists in both configs but differs.

    `name` is trigger-qualified (`"on_create[build]"` for a step,
    `"on_create<setup>"` for a stage) because the same name may appear under
    both triggers and they are distinct units.
    """

    name: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class BlockChange:
    """A change to the `Autostart` block itself rather than to one step.

    `detail` is pre-rendered for display: `"600 → 900"` for a scalar,
    a sorted comma-joined key list for `env`.
    """

    name: str
    detail: str


@dataclass(frozen=True)
class AutostartDeviation:
    """How one autostart block differs from another.

    Used for both comparisons this module makes (see the module docstring), so
    it states facts and draws no conclusion — the privilege decision lives in
    `assess_escalation`, which needs a baseline and a provenance this object
    knows nothing about.

    Two of the differences are the branch reaching past the container it
    already controls. In the flat form the reach is a step's; in the stage
    form a step may not carry either (`AutostartStage` rejects it) and the
    reach is the stage's, so these two name whichever level owns it:

    - `widening_steps`: units that run with `network: loose` where the other
      block does not (including a brand-new one).
    - `attached_mounts`: names of `optional_mounts` attached that the other
      block's counterpart does not attach. The executor binds each named
      optional mount — typically a personal credential directory such as
      `~/.aws` or `~/.m2` — into the container for the step's or stage's
      duration, so adding one gets a host path mounted into a container whose
      command lines the same branch writes.

    Everything else a step controls (`run`, `env`, `working_dir`, `background`,
    `timeout`, `continue_on_error`), and a stage's `detach` and chain layout,
    is container-internal: it adds nothing beyond the code execution a cloned
    branch inherently has.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[StepChange, ...] = ()
    block_changes: tuple[BlockChange, ...] = ()
    widening_steps: tuple[str, ...] = ()
    attached_mounts: tuple[str, ...] = ()

    @property
    def any_change(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.block_changes)

    @property
    def widens_network(self) -> bool:
        """True when at least one step or stage widens network access to `loose`."""
        return bool(self.widening_steps)


def _is_stage_form(entries: list[AutostartStep] | list[AutostartStage]) -> bool:
    """True when the trigger *declares* stages rather than a flat step list.

    Not the same as "has stages": `normalize_stages` gives every flat block
    implicit stages too. Only a declared stage is a name the repo chose, and
    only a declared stage owns `network`/`mounts` — an implicit one merely
    inherited them from the steps it collapsed.
    """
    return bool(entries) and isinstance(entries[0], AutostartStage)


def _steps_by_name(
    entries: list[AutostartStep] | list[AutostartStage], trigger: str
) -> dict[str, AutostartStep]:
    """Every step the trigger runs, in either form, keyed by step name.

    Step names are unique per trigger — enforced at load time in
    `config._build_config_from_dict` — so the key identifies a step across
    both configs *and* across the two forms: a step keeps its name when a
    repo migrates a flat block into stages, which is exactly why matching
    happens here and not by stage name.
    """
    return {
        _STEP_KEY.format(trigger=trigger, name=step.name): step
        for stage in normalize_stages(entries)
        for chain in stage.all_chains()
        for step in chain.steps
    }


def _declared_stages(
    entries: list[AutostartStep] | list[AutostartStage], trigger: str
) -> dict[str, AutostartStage]:
    """The trigger's stages, keyed by name — empty for a flat block.

    A flat block's implicit stages are deliberately excluded: their names are
    an artefact of `normalize_stages` (the first step's name), so reporting
    them as added/removed/changed would turn every ordinary flat-form diff
    into a duplicate of its own step lines.
    """
    if not _is_stage_form(entries):
        return {}
    return {_STAGE_KEY.format(trigger=trigger, name=s.stage): s for s in normalize_stages(entries)}


def _chain_layout(stage: AutostartStage) -> tuple[tuple[str, int], ...]:
    """The stage's parallel shape: each chain's name and how many steps it runs.

    Read through `all_chains()`, so the `steps:` shorthand and an explicit
    single chain named `main` compare equal: they *are* the same stage.

    Deliberately the *shape*, not the step names. Names here would make every
    renamed step inside a stage add a `chains changed` line on top of the `+`/
    `-` step lines that already say it — true but empty. The cost is that an
    exchange of steps between two equally-sized chains is invisible, since a
    plain move changes a count; that is an ordering change, container-internal
    like `run`, and no privilege signal reads this.
    """
    return tuple((c.name, len(c.steps)) for c in stage.all_chains())


def _stage_fields(host: AutostartStage, branch: AutostartStage) -> tuple[str, ...]:
    """The stage-owned fields that differ, plus `chains` for a re-layout."""
    fields = [
        name
        for name in type(branch).model_fields
        if name not in _STAGE_STRUCTURE_FIELDS and getattr(host, name) != getattr(branch, name)
    ]
    if _chain_layout(host) != _chain_layout(branch):
        # Same steps, different chains: a change in what runs in parallel,
        # which no step-level field records.
        fields.append("chains")
    return tuple(fields)


@dataclass(frozen=True)
class _Grant:
    """The container-scoped reach of one autostart unit.

    `key` matches the unit against the other config; `label` names it to the
    user. They differ for a declared stage, and that split is the whole point:

    - matching is per **step**, because step names survive a flat↔stage
      migration and stage names do not. Comparing stage names would call a
      repo's move to stages a widening (the stage is "new"), and would miss a
      step whose `loose` run got absorbed into an already-`loose` stage.
    - the label is the **stage**, because in the stage form the grant is the
      stage's: it holds for every chain it runs, and naming its five steps
      instead would be five lines about one decision.

    `stepless` marks the one unit that has no step to match on, so its `key`
    is its stage name after all — see `_stepless_counterpart`.
    """

    key: str
    label: str
    network: str | None
    mounts: tuple[str, ...]
    stepless: bool = False


def _grants(entries: list[AutostartStep] | list[AutostartStage], trigger: str) -> dict[str, _Grant]:
    """What each unit of `entries` reaches outside the container.

    One entry per step, carrying the *effective* network and mounts — the
    stage's in the stage form (steps may not carry either: `AutostartStage`
    rejects it), the step's own in the flat form (where `normalize_stages`
    lifts them onto an implicit stage that spans exactly the steps sharing
    them, so the two agree by construction).
    """
    grants: dict[str, _Grant] = {}
    declared = _is_stage_form(entries)
    for stage in normalize_stages(entries):
        stage_key = _STAGE_KEY.format(trigger=trigger, name=stage.stage)
        steps = [step for chain in stage.all_chains() for step in chain.steps]
        if declared and not steps:
            # A stage with no steps still switches the profile and attaches
            # its mounts, so it reaches outside the container with no step to
            # hang that on. Give it a unit of its own rather than lose it.
            grants[stage_key] = _Grant(
                key=stage_key,
                label=stage_key,
                network=stage.network,
                mounts=tuple(stage.mounts),
                stepless=True,
            )
        for step in steps:
            key = _STEP_KEY.format(trigger=trigger, name=step.name)
            grants[key] = _Grant(
                key=key,
                label=stage_key if declared else key,
                network=stage.network if stage.network is not None else step.network,
                mounts=tuple(stage.mounts) + tuple(step.mounts),
            )
    return grants


def _step_value(step: AutostartStep, grant: _Grant, field: str) -> object:
    """The value to compare one step field on, across both forms.

    `network` and `mounts` come from the step's grant — the stage's in the
    stage form, the step's own in the flat form — because a step inside a
    stage may not carry either, so its stored value says nothing about what it
    actually runs with. Every other field is the step's own.
    """
    if field == "network":
        return grant.network
    if field == "mounts":
        return grant.mounts
    return getattr(step, field)


def _step_fields(
    host: AutostartStep,
    branch: AutostartStep,
    host_grant: _Grant,
    branch_grant: _Grant,
    *,
    stage_owned: bool,
) -> tuple[str, ...]:
    """The step fields that differ, at the level that owns each of them.

    `network` and `mounts` are the two that move between levels:

    - they are compared *as they take effect* (`_step_value`), never as
      stored. A step inside a stage may not carry either, so its stored value
      says nothing about what it runs with — and a raw diff would announce a
      `network` change for a step moved unchanged into a stage carrying the
      same mode, which is exactly the flat→stage migration this feature
      exists to enable.
    - they are not reported here at all once the branch declares stages
      (`stage_owned`): the stage owns them and the stage level names them, so
      repeating it on every step of that stage is a double-report the flat
      form does not have. The grant level still measures them in both forms —
      no privilege signal depends on this choice, only the rendering does.
    """
    skip = {"name", "network", "mounts"} if stage_owned else {"name"}
    return tuple(
        name
        for name in type(branch).model_fields
        if name not in skip
        and _step_value(host, host_grant, name) != _step_value(branch, branch_grant, name)
    )


def _stepless_counterpart(host_grants: dict[str, _Grant], branch: _Grant) -> _Grant | None:
    """A host stepless stage granting exactly what `branch` grants, if any.

    Every other unit is matched by step name, which survives a rename of the
    stage around it. A stepless stage has no step, so its key is its own name —
    and renaming one would otherwise read as a brand-new stage that both widens
    and attaches mounts. `attached_mounts` is the always-prompts, default-no
    path that aborts `jailbee new` with nothing created, so a cosmetic rename
    would block container creation.

    The fallback is deliberately an *exact* grant match: same network, same
    mounts (order-insensitive — reordering a mount list grants nothing). A
    rename that also changes what the stage grants finds no counterpart and is
    reported in full. The structural `+`/`-` stage lines are still printed
    either way, exactly as for a renamed stage that does have steps.
    """
    return next(
        (
            g
            for g in host_grants.values()
            if g.stepless
            and g.network == branch.network
            and sorted(g.mounts) == sorted(branch.mounts)
        ),
        None,
    )


def diff_autostart(host: Autostart, branch: Autostart) -> AutostartDeviation:
    """Compare two autostart blocks. Pure — no git, no Incus, no filesystem.

    Handles both forms on either side, including a flat host against a stage
    branch: both are normalized through `autostart_plan.normalize_stages`, and
    the three comparisons below each run at the level that owns what they
    measure — stages for the side effects a stage owns, steps for the work a
    step does, and grants for the reach outside the container.
    """
    added: list[str] = []
    removed: list[str] = []
    changed: list[StepChange] = []
    widening: list[str] = []
    attached_mounts: set[str] = set()

    for trigger in _TRIGGERS:
        host_entries = getattr(host, trigger)
        branch_entries = getattr(branch, trigger)

        # Stage level: the structure, and the side effects a stage owns.
        # Listed before the steps so the rendering reads top-down.
        host_stages = _declared_stages(host_entries, trigger)
        branch_stages = _declared_stages(branch_entries, trigger)
        added.extend(key for key in branch_stages if key not in host_stages)
        removed.extend(key for key in host_stages if key not in branch_stages)
        for key, b_stage in branch_stages.items():
            h_stage = host_stages.get(key)
            if h_stage is None:
                continue
            stage_fields = _stage_fields(h_stage, b_stage)
            if stage_fields:
                changed.append(StepChange(name=key, fields=stage_fields))

        host_grants = _grants(host_entries, trigger)
        branch_grants = _grants(branch_entries, trigger)

        # Step level: the work a step does — plus `network`/`mounts` while the
        # branch keeps them on its steps. See `_step_fields`.
        host_steps = _steps_by_name(host_entries, trigger)
        branch_steps = _steps_by_name(branch_entries, trigger)
        for key in branch_steps:
            if key not in host_steps:
                added.append(key)
        for key in host_steps:
            if key not in branch_steps:
                removed.append(key)
        for key, b_step in branch_steps.items():
            h_step = host_steps.get(key)
            if h_step is None:
                continue
            fields = _step_fields(
                h_step,
                b_step,
                host_grants[key],
                branch_grants[key],
                stage_owned=_is_stage_form(branch_entries),
            )
            if fields:
                changed.append(StepChange(name=key, fields=fields))

        # Grant level: the reach outside the container, matched per step.
        for key, b_grant in branch_grants.items():
            h_grant = host_grants.get(key) or (
                _stepless_counterpart(host_grants, b_grant) if b_grant.stepless else None
            )
            # Widening: loose on the branch that the host did not already
            # grant. Covers a changed unit and a brand-new one alike.
            if b_grant.network == "loose" and (h_grant is None or h_grant.network != "loose"):
                widening.append(b_grant.label)
            # Escalation: an optional_mount the host's counterpart does not
            # attach — a host path bound into a container the branch scripts.
            # A brand-new unit has no counterpart, so every mount it names
            # counts.
            attached_mounts |= set(b_grant.mounts) - set(
                h_grant.mounts if h_grant is not None else ()
            )

    # Block-level fields are hand-compared, unlike the step-level diff which
    # enumerates `model_fields`. `test_branch_config` pins the field set so a
    # fifth field on `Autostart` forces a decision here instead of being
    # silently ignored.
    block_changes: list[BlockChange] = []
    if host.step_timeout != branch.step_timeout:
        block_changes.append(
            BlockChange(name="step_timeout", detail=f"{host.step_timeout} → {branch.step_timeout}")
        )
    if host.env != branch.env:
        # Values may hold secrets-ish content; name the affected keys only.
        affected = sorted(
            k for k in set(host.env) | set(branch.env) if host.env.get(k) != branch.env.get(k)
        )
        block_changes.append(BlockChange(name="env", detail=", ".join(affected)))

    return AutostartDeviation(
        added=tuple(added),
        removed=tuple(removed),
        changed=tuple(changed),
        block_changes=tuple(block_changes),
        # `dict.fromkeys`, not `set`: one declared stage is the label for every
        # step it widens, so the same name arrives once per step — deduplicated
        # in first-seen order, which is the order the stages run in.
        widening_steps=tuple(dict.fromkeys(widening)),
        attached_mounts=tuple(sorted(attached_mounts)),
    )


def format_deviation(dev: AutostartDeviation, *, source: str) -> str:
    """Render a deviation as a compact, reviewable block.

    `source` names the git ref or commit the branch config was read from, so it
    is never ambiguous which commit produced these steps.

    Explains the surprise only. The privilege verdict is rendered separately by
    `format_escalation`, which compares against the baseline rather than the
    checkout — printing it here too would let the two disagree in the one place
    the user reads them.

    Plain text, not Rich markup: names are trigger-qualified
    (`on_create[build]` for a step, `on_create<setup>` for a stage) and
    `source` may carry a branch name like `feat/[wip]`, so callers must print
    the result without markup parsing — `tui.warn_plain`, not `tui.warn`.
    """
    lines = [f"autostart config comes from {source}, not your checkout:"]
    for name in dev.added:
        lines.append(f"  + {name}")
    for name in dev.removed:
        lines.append(f"  - {name}")
    for change in dev.changed:
        lines.append(f"  ~ {change.name}: {', '.join(change.fields)} changed")
    for block in dev.block_changes:
        lines.append(f"  ! {block.name}: {block.detail}")
    return "\n".join(lines)


@dataclass(frozen=True)
class EscalationVerdict:
    """Whether a branch's autostart reaches past the container, and whether
    that reach needs an answer from the operator.

    `prompts` is derived, never stored, so the gate can never disagree with the
    reasons `format_escalation` renders. The two reaches are weighed
    differently on purpose:

    - `attached_mounts` always prompts. Attaching an `optional_mounts` entry is
      what *creates* the asset: a credential directory the container did not
      otherwise hold, inside a container whose command lines the branch writes.
      No network mode protects against that, and steps naming mounts are rare,
      so the question is cheap.
    - `widening_steps` prompts only for an `untrusted` head. Once the container
      runs the branch's code — which is the whole premise — `strict` is an
      egress allowlist of package registries and forges that all accept
      uploads, so it is not a confidentiality boundary against that code;
      `loose` is also the ordinary way a step installs dependencies. Asking
      about every branch of your own repo would be noise with no protection,
      whereas a PR review container is exactly the case where the head is
      code nobody has vouched for yet.
    """

    widening_steps: tuple[str, ...]
    attached_mounts: tuple[str, ...]
    baseline_source: str
    untrusted: bool

    @property
    def any_widening(self) -> bool:
        """True when the branch reaches past the container at all — worth
        warning about even when it does not warrant a question."""
        return bool(self.widening_steps or self.attached_mounts)

    @property
    def prompts(self) -> bool:
        return bool(self.attached_mounts) or (bool(self.widening_steps) and self.untrusted)


def _can_widen(autostart: Autostart) -> bool:
    """True when `autostart` holds anything that *could* be a widening.

    A block that runs nothing `loose` and attaches no mount cannot widen
    against any baseline, so the baseline need not be read at all — this keeps
    the common `jailbee new` off the git path entirely.

    Asked of the same `_grants` the diff uses, so the fast path cannot answer
    "nothing to see" about a form the diff would have had something to say
    about — in the stage form the reach is the stage's, and a stage with no
    steps of its own still has one.
    """
    return any(
        grant.network == "loose" or grant.mounts
        for trigger in _TRIGGERS
        for grant in _grants(getattr(autostart, trigger), trigger).values()
    )


def _baseline_autostart(cfg: Config) -> tuple[Autostart, str]:
    """The autostart the privilege gate measures against, plus its label.

    `refs/remotes/<upstream_remote>/<default_branch>` — what review and CI
    gate. Falls back to the host checkout (never to "no baseline", which would
    grant everything silently) when that ref carries no usable config: a repo
    with no upstream remote, a default branch never fetched, or a baseline
    config that does not load. The label says which of the two was used,
    because it changes what the verdict means.

    An *unreachable* ref is warned about; an absent config on a reachable one
    is not. The difference matters: the second is a repo that simply keeps no
    autostart config on its default branch, while the first silently reduces
    the baseline to the caller's own checkout — the very config a branch could
    have authored for itself. That is the gate getting weaker, and it must be
    said out loud rather than inferred from a label nobody reads.
    """
    from jailbee.config import ConfigError, load_config_from_text
    from jailbee.git import remote_ref_exists
    from jailbee.tui import warn_plain

    ref = f"refs/remotes/{cfg.upstream_remote}/{cfg.default_branch}"
    found = _config_text_at_ref(cfg.repo_root, ref)
    if found is None:
        if not remote_ref_exists(cfg.repo_root, cfg.upstream_remote, cfg.default_branch):
            warn_plain(
                f"Cannot use {ref} as the privilege baseline — the ref does not "
                f"exist on this host (no '{cfg.upstream_remote}' remote, or "
                f"'{cfg.default_branch}' never fetched).\n"
                f"Falling back to your checkout's autostart config, which is a "
                f"weaker gate: it is not what review and CI approved."
            )
            return cfg.autostart, f"your checkout ({ref} cannot be read)"
        return cfg.autostart, f"your checkout ({ref} has no .jailbee/config.yaml)"
    text, config_rel = found
    try:
        baseline_cfg = load_config_from_text(text, cfg.repo_root / config_rel)
    except ConfigError as e:
        warn_plain(
            f"Cannot use {ref} as the privilege baseline — it is not valid: {e}\n"
            f"Falling back to your checkout's autostart config."
        )
        return cfg.autostart, f"your checkout ({ref} is not valid)"
    return baseline_cfg.autostart, ref


def assess_escalation(cfg: Config, branch: Autostart, *, untrusted: bool) -> EscalationVerdict:
    """Weigh `branch`'s autostart against the repo's reviewed baseline.

    `untrusted` marks a head nobody has vouched for — a `jailbee new --pr N` review
    container, whose head is arbitrary code from a PR (a fork's, most sharply).
    See `EscalationVerdict` for why that only matters for network widening.
    """
    if not _can_widen(branch):
        return EscalationVerdict(
            widening_steps=(), attached_mounts=(), baseline_source="", untrusted=untrusted
        )
    baseline, source = _baseline_autostart(cfg)
    dev = diff_autostart(baseline, branch)
    return EscalationVerdict(
        widening_steps=dev.widening_steps,
        attached_mounts=dev.attached_mounts,
        baseline_source=source,
        untrusted=untrusted,
    )


def format_escalation(verdict: EscalationVerdict) -> str:
    """Render the privilege verdict, or `""` when there is nothing to report.

    Names the baseline: "widens X beyond your checkout" and "beyond
    origin/main" are different claims, and the reader cannot tell which
    comparison was made otherwise.

    Plain text, not Rich markup — see `format_deviation`.
    """
    if not verdict.any_widening:
        return ""
    lines = [f"branch autostart widens privileges beyond {verdict.baseline_source}:"]
    if verdict.widening_steps:
        lines.append(f"  ⚠ network access 'loose' in: {', '.join(verdict.widening_steps)}")
    if verdict.attached_mounts:
        lines.append(f"  ⚠ attaches host mount(s): {', '.join(verdict.attached_mounts)}")
    return "\n".join(lines)


def _fallback_warning(source_label: str, reason: str) -> str:
    """The shared shape of both "can't use the branch config" warnings.

    `reason` completes "… — <reason>" and may be multi-line. Printed with
    `tui.warn_plain`: `reason` interpolates a `ConfigError`, whose pydantic v2
    text routinely contains `[type=…, input_value=…]`, and a
    `validate_runtime` issue naming a step as `on_create[build]`.
    """
    return (
        f"Ignoring autostart config from {source_label} — {reason}\n"
        f"Falling back to your checkout's autostart config."
    )


@dataclass(frozen=True)
class BranchAutostart:
    """A host Config with the branch's autostart grafted on, plus the diff."""

    cfg: Config
    deviation: AutostartDeviation
    source: str


def load_branch_autostart(
    cfg: Config,
    ref: str,
    *,
    source_label: str,
) -> BranchAutostart | None:
    """Load the target branch's autostart at `ref` and graft it onto `cfg`.

    Returns `None` — meaning "use the host autostart unchanged" — when the
    branch commits no config at either `.jailbee/config.yaml` or the
    deprecated `.gie/config.yaml` (silently: a branch need not define one),
    or when the branch config cannot be used (after warning).

    `source_label` is what the user sees: the full ref (`"refs/heads/feat/x"`)
    when the clone follows a local branch, or `"<sha12> (<branch>)"` when it is
    pinned to a commit (origin-mode, `--pr`).
    """
    from jailbee.config import ConfigError, load_config_from_text
    from jailbee.tui import warn_plain

    found = _config_text_at_ref(cfg.repo_root, ref)
    if found is None:
        return None
    text, config_rel = found

    try:
        branch_cfg = load_config_from_text(text, cfg.repo_root / config_rel)
    except ConfigError as e:
        warn_plain(_fallback_warning(source_label, f"it is not valid: {e}"))
        return None

    grafted = cfg.model_copy(update={"autostart": branch_cfg.autostart})

    # Cross-config check: each config validated fine on its own, but the
    # *combination* may not — a branch step may name an optional_mount that
    # only the branch's config defines. Compare issue sets so the host's own
    # pre-existing issues (a mount path missing on this machine) don't block
    # us — validate_runtime() returns advisory issues, not exceptions, and a
    # non-empty list is not itself a failure: only issues the graft
    # *introduces* (present in `grafted` but not already in `cfg`) matter.
    new_issues = set(grafted.validate_runtime()) - set(cfg.validate_runtime())
    if new_issues:
        detail = "\n".join(f"  - {i}" for i in sorted(new_issues))
        warn_plain(_fallback_warning(source_label, f"it does not fit your host config:\n{detail}"))
        return None

    return BranchAutostart(
        cfg=grafted,
        deviation=diff_autostart(cfg.autostart, branch_cfg.autostart),
        source=source_label,
    )
