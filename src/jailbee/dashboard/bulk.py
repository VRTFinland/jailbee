"""Several containers, one action: which can take it, how it runs, what came of it.

Frontend-agnostic like `jailbee.dashboard.menus`: the terminal dashboard and the
Qt window both plan through here. Eligibility is never decided twice — a marked
container takes a bulk verb exactly when its own menu offers that verb
(:func:`jailbee.dashboard.menus.actions_for_container`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard.jobs import JobResult, needs_terminal
from jailbee.dashboard.menus import actions_for_container
from jailbee.dashboard.model import RepoGroup, RepoTarget, _find_group
from jailbee.dashboard.overlays import PickerEntry
from jailbee.lifecycle import ContainerInfo

if TYPE_CHECKING:
    from jailbee.config import Config

BulkMode = Literal["parallel", "foreground"]

# The "N selected" menu, in this order.
BULK_VERBS: tuple[str, ...] = (
    "start",
    "stop",
    "restart",
    "net strict",
    "net loose",
    "git push",
    "git pull",
    "merge",
    "destroy",
)
# One detached child per container. The rest run once per repo, in the
# terminal, over every name: their CLI asks questions and prints a roll-up.
PARALLEL_VERBS: frozenset[str] = frozenset(
    {"start", "stop", "restart", "net strict", "net loose", "destroy"}
)
BULK_LABELS: dict[str, str] = {
    "start": "Start",
    "stop": "Stop",
    "restart": "Restart",
    "net strict": "Network: strict",
    "net loose": "Network: loose…",
    "git push": "Update from base (git push)",
    "git pull": "Send commits to host (git pull)",
    "merge": "Merge into…",
    "destroy": "Destroy…",
}
_GIT_VERBS = frozenset({"git push", "git pull", "merge"})


@dataclass(frozen=True)
class BulkAction:
    """``verb`` over the marked containers: who takes it, who does not and why."""

    verb: str
    eligible: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def mode(self) -> BulkMode:
        return "parallel" if self.verb in PARALLEL_VERBS else "foreground"

    @property
    def label(self) -> str:
        return f"{BULK_LABELS[self.verb]} ({len(self.eligible)})"


def _container(groups: Sequence[RepoGroup], name: str) -> ContainerInfo | None:
    group = _find_group(list(groups), name)
    if group is None:
        return None
    return next((c for c in group.containers if c.name == name), None)


def _offered(
    groups: Sequence[RepoGroup],
    name: str,
    *,
    remote: bool,
    ssh_policy: RemoteSSHConfig | None,
    over_ssh: bool,
) -> set[str]:
    return {
        verb
        for _label, verb in actions_for_container(
            list(groups), name, remote=remote, ssh_policy=ssh_policy, over_ssh=over_ssh
        )
    }


def _skip_reason(container: ContainerInfo | None, verb: str) -> str:
    """Why a container's menu does not offer ``verb``, in a few words."""
    if container is None:
        return "gone"
    state = container.state
    if verb == "start":
        return "already running" if state == "Running" else state.lower()
    if verb in _GIT_VERBS and container.mode == "mount":
        return "mount mode"
    if state != "Running":
        return "already stopped" if verb == "stop" and state == "Stopped" else "not running"
    if verb.startswith("net "):
        return f"already {container.network}"
    if verb == "git pull":
        return "no commits for the host"
    return "not offered"


def plan_bulk(
    groups: Sequence[RepoGroup],
    names: Sequence[str],
    verb: str,
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
    busy: frozenset[str] = frozenset(),
) -> BulkAction:
    """Split ``names`` into the containers that take ``verb`` and the rest, with reasons.

    ``names`` keeps its order; a repeated name counts once. A name in ``busy``
    (an operation on it is still running) is skipped as "busy".
    """
    eligible: list[str] = []
    skipped: list[tuple[str, str]] = []
    for name in dict.fromkeys(names):
        if name in busy:
            skipped.append((name, "busy"))
            continue
        if verb in _offered(groups, name, remote=remote, ssh_policy=ssh_policy, over_ssh=over_ssh):
            eligible.append(name)
            continue
        group = _find_group(list(groups), name)
        if group is not None and RepoTarget.of(group) is None:
            reason = "view-only"
        elif over_ssh and verb in _offered(
            groups, name, remote=remote, ssh_policy=ssh_policy, over_ssh=False
        ):
            reason = "not permitted by the SSH policy"
        else:
            reason = _skip_reason(_container(groups, name), verb)
        skipped.append((name, reason))
    return BulkAction(verb, tuple(eligible), tuple(skipped))


def bulk_actions(
    groups: Sequence[RepoGroup],
    names: Sequence[str],
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
    busy: frozenset[str] = frozenset(),
) -> list[BulkAction]:
    """Every bulk verb at least one of ``names`` takes, in :data:`BULK_VERBS` order."""
    plans = (
        plan_bulk(
            groups, names, verb, remote=remote, ssh_policy=ssh_policy, over_ssh=over_ssh, busy=busy
        )
        for verb in BULK_VERBS
    )
    return [plan for plan in plans if plan.eligible]


def nothing_to_do(action: BulkAction) -> str:
    """The notice for a bulk verb none of the marked containers takes."""
    reasons = "; ".join(f"{name}: {reason}" for name, reason in action.skipped)
    return f"{BULK_LABELS[action.verb].rstrip('…')}: nothing to do ({reasons})"


def bulk_argv(verb: str, name: str, extra: Sequence[str] = ()) -> list[str]:
    """One parallel child's canonical argv: no ``jailbee``, no ``--config``.

    ``destroy`` gets ``--force``: the dashboard has asked already, and a
    detached child has no stdin for the CLI's own question. ``extra`` carries
    the answers the dashboard collected (``--for <ttl>``).
    """
    return [*verb.split(), name, *(["--force"] if verb == "destroy" else []), *extra]


@dataclass(frozen=True)
class ForegroundRun:
    """One terminal run of a foreground bulk verb: one repo, its marked names."""

    prefix: str
    target: RepoTarget
    argv: tuple[str, ...]  # canonical: no ``jailbee``, no ``--config``
    names: tuple[str, ...]


def foreground_runs(groups: Sequence[RepoGroup], action: BulkAction) -> list[ForegroundRun]:
    """One CLI run per repo, in listing order: a ``--config`` addresses one repo.

    A single run over two repos' containers would load the first repo's config
    for the second repo's containers.
    """
    runs: list[ForegroundRun] = []
    for group in groups:
        target = RepoTarget.of(group)
        members = {c.name for c in group.containers}
        names = tuple(name for name in action.eligible if name in members)
        if target is None or not names:
            continue
        runs.append(ForegroundRun(group.prefix, target, (*action.verb.split(), *names), names))
    return runs


log = logging.getLogger(__name__)


@dataclass
class BulkBatch:
    """One parallel bulk run while its children finish; ``summary`` once ``done``."""

    verb: str
    pending: set[str] = field(default_factory=set)
    ok: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    def finish(self, name: str, result: JobResult) -> None:
        self.pending.discard(name)
        if result.returncode == 0:
            self.ok.append(name)
        elif needs_terminal(result):
            self.failed[name] = "needs a terminal; run it from its own menu"
        else:
            self.failed[name] = result.failure_line() or f"exited {result.returncode}"

    @property
    def done(self) -> bool:
        return not self.pending

    def summary(self) -> str:
        def listed(items: dict[str, str]) -> str:
            return "; ".join(f"{name}: {reason}" for name, reason in items.items())

        parts = [f"{len(self.ok)} ok"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed ({listed(self.failed)})")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped ({listed(self.skipped)})")
        return f"{self.verb}: " + ", ".join(parts)


def destroy_risk_lines(groups: Sequence[RepoGroup], names: Sequence[str]) -> tuple[str, ...]:
    """What destroying ``names`` would discard: one line per risky container.

    The same assessment `destroy --all` prints (`cli._warn_before_destroy`) and
    the Qt confirm shows. Each repo's config is loaded once, from its root (a
    repo with no config file still has the synthesized one). An unreadable
    config degrades to "no risk shown", as in the Qt dialog.
    """
    from jailbee.config import load_repo_config
    from jailbee.destroy_guard import assess, status_is_unknown, unknown_status_warning

    lines: list[str] = []
    unknown: list[str] = []
    configs: dict[str, Config | None] = {}
    for name in names:
        group = _find_group(list(groups), name)
        container = _container(groups, name)
        if group is None or container is None or group.repo_root is None:
            continue
        if status_is_unknown(container):
            unknown.append(container.name)
            continue
        if container.git_status is None:
            continue
        if group.prefix not in configs:
            try:
                configs[group.prefix] = load_repo_config(Path(group.repo_root))
            except Exception:
                log.debug("destroy guard: could not assess %s", group.repo_root, exc_info=True)
                configs[group.prefix] = None
        cfg = configs[group.prefix]
        if cfg is None:
            continue
        summary = assess(cfg, container)
        if summary is not None:
            lines.append(f"⚠ {summary.line}")
    if unknown:
        lines.append(f"⚠ {unknown_status_warning(unknown)}")
    return tuple(lines)


def bulk_loose_default(groups: Sequence[RepoGroup], names: Sequence[str]) -> str | None:
    """The TTL to offer first: the first named container's repo with a revert policy.

    None when no repo of ``names`` reverts loose at all: then nothing is asked
    and no ``--for`` is passed, as the single-container dashboards do.
    """
    for name in names:
        group = _find_group(list(groups), name)
        if group is not None and group.loose_ttl_default is not None:
            return group.loose_ttl_default
    return None


def loose_ttl_entries(default: str) -> tuple[PickerEntry, ...]:
    """The TTL picker: ``default`` first, the presets, then ``never``."""
    from jailbee.config import LOOSE_TTL_PRESETS

    values = [default, *(p for p in LOOSE_TTL_PRESETS if p != default)]
    return (
        *(PickerEntry(value, value) for value in values),
        PickerEntry("never (no auto-revert)", "never"),
    )
