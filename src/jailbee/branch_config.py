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

if TYPE_CHECKING:
    from jailbee.config import Autostart, AutostartStep, Config

_TRIGGERS = ("on_create", "on_start")


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
    """One step that exists in both configs but differs.

    `name` is trigger-qualified (`"on_create[build]"`) because the same step
    name may appear under both triggers and they are distinct steps.
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
    already controls:

    - `widening_steps`: steps that run with `network: loose` where the other
      block's same-named step does not (including a brand-new step).
    - `attached_mounts`: names of `optional_mounts` a step attaches that the
      other block's same-named step does not. `_apply_step` binds each named
      optional mount — typically a personal credential directory such as
      `~/.aws` or `~/.m2` — into the container for the step's duration, so a
      step that adds one gets a host path mounted into a container whose
      command line the same branch writes.

    Everything else a step controls (`run`, `env`, `working_dir`, `background`,
    `timeout`, `continue_on_error`) is container-internal: it adds nothing
    beyond the code execution a cloned branch inherently has.
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
        """True when at least one step widens network access to `loose`."""
        return bool(self.widening_steps)


def _by_name(steps: list[AutostartStep], trigger: str) -> dict[str, AutostartStep]:
    # Step names are unique per trigger — enforced at load time in
    # `config._build_config_from_dict`.
    return {f"{trigger}[{s.name}]": s for s in steps}


def diff_autostart(host: Autostart, branch: Autostart) -> AutostartDeviation:
    """Compare two autostart blocks. Pure — no git, no Incus, no filesystem."""
    added: list[str] = []
    removed: list[str] = []
    changed: list[StepChange] = []
    widening_steps: list[str] = []
    attached_mounts: set[str] = set()

    for trigger in _TRIGGERS:
        host_steps = _by_name(getattr(host, trigger), trigger)
        branch_steps = _by_name(getattr(branch, trigger), trigger)

        for key in branch_steps:
            if key not in host_steps:
                added.append(key)
        for key in host_steps:
            if key not in branch_steps:
                removed.append(key)

        for key, b_step in branch_steps.items():
            h_step = host_steps.get(key)
            # Widening: loose on the branch that the host did not already
            # grant. Covers both a changed step and a brand-new one.
            if b_step.network == "loose" and (h_step is None or h_step.network != "loose"):
                widening_steps.append(key)
            # Escalation: an optional_mount the host's same-named step does not
            # attach — a host path bound into a container the branch scripts.
            # A brand-new step has no host counterpart, so every mount it names
            # counts.
            attached_mounts |= set(b_step.mounts) - set(h_step.mounts if h_step is not None else ())
            if h_step is None:
                continue
            fields = tuple(
                name
                for name in type(b_step).model_fields
                if name != "name" and getattr(h_step, name) != getattr(b_step, name)
            )
            if fields:
                changed.append(StepChange(name=key, fields=fields))

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
        widening_steps=tuple(widening_steps),
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

    Plain text, not Rich markup: step names are trigger-qualified
    (`on_create[build]`) and `source` may carry a branch name like
    `feat/[wip]`, so callers must print the result without markup parsing —
    `tui.warn_plain`, not `tui.warn`.
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

    A block with no `loose` step and no step mounts cannot widen against any
    baseline, so the baseline need not be read at all — this keeps the common
    `jailbee new` off the git path entirely.
    """
    return any(
        step.network == "loose" or step.mounts
        for trigger in _TRIGGERS
        for step in getattr(autostart, trigger)
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
