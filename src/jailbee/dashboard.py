"""jailbee dashboard — live, auto-refreshing cross-repo container view.

Container state is read ONLY through the :class:`Incus` wrapper. The single
``subprocess`` use is dispatching ``jailbee <subcommand>`` for the action menu —
a NON-incus subprocess (it spawns jailbee's own CLI), in the same spirit as
``gui.py`` launching GUI processes, so each action reuses the real command's
behaviour and the target repo's own config.
"""

from __future__ import annotations

import logging
import os
import select
import shlex
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table

from jailbee import table_format
from jailbee.config import (
    DASHBOARD_DEFAULT_HIDE,
    ColumnConfig,
    format_loose_after,
    load_config,
)
from jailbee.dashboard_settings import (
    SettingsState,
    enabled_names,
    move_settings,
    open_settings,
    render_settings,
    switch_tab,
    toggle_current,
)
from jailbee.db.view_prefs import ViewState, load_view_state, save_view_state
from jailbee.global_config import (
    GlobalConfig,
    default_global_config_path,
    load_global_config,
)
from jailbee.lifecycle import (
    ContainerInfo,
    format_duration_short,
    list_containers,
    ls_field_specs,
)
from jailbee.tui import console, error

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from sqlalchemy.engine import Engine

    from jailbee.config import Config
    from jailbee.git_status import GitStatus
    from jailbee.incus import Incus

log = logging.getLogger(__name__)

FieldSpecCI = table_format.FieldSpec[ContainerInfo]


@dataclass
class RepoGroup:
    """One repo's containers. ``config_path`` is None for orphan groups
    (jailbee-managed containers whose repo config could not be loaded).
    ``ide_enabled``/``chrome_enabled`` mirror the repo's own
    ``jetbrains.enabled``/``chrome.enabled`` config and gate the
    corresponding action-menu entries; orphan groups keep both False.
    ``loose_ttl_default`` is the repo's effective ``loose_auto_revert.after``
    as prompt-ready text — what the GUI's duration dialog pre-selects — or
    None when auto-revert is disabled, which tells the GUI not to ask at all
    (there is no TTL to schedule). Orphan groups keep None.
    ``push_action_default``/``push_source_default`` mirror the repo's effective
    ``push.default_action``/``default_source``, so a front-end can tell whether
    `jailbee git push` would stop to ask a question its own child process
    cannot answer. Orphan groups keep ``PushConfig``'s defaults."""

    prefix: str
    repo_root: str | None
    config_path: Path | None
    containers: list[ContainerInfo]
    ide_enabled: bool = False
    chrome_enabled: bool = False
    loose_ttl_default: str | None = None
    push_action_default: str = "ask"
    push_source_default: str = "base"


def registered_repo_configs() -> list[Path]:
    """Existing ``.jailbee/config.yaml`` paths for all RegisteredRepo rows."""
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo
    from jailbee.paths import repo_config_path

    out: list[Path] = []
    with Session(get_engine()) as session:
        for repo in session.exec(select(RegisteredRepo)).all():
            # A stale registry row whose config file is gone is silently
            # skipped (the dashboard never prunes the registry).
            found = repo_config_path(Path(repo.repo_root))
            if found is not None:
                out.append(found)
    return out


def collect_config_paths(cwd_config: Path | None) -> list[Path]:
    """Registered repo configs plus the cwd config, deduped, cwd first."""
    candidates = ([cwd_config] if cwd_config is not None else []) + registered_repo_configs()
    seen: set[Path] = set()
    ordered: list[Path] = []
    for p in candidates:
        rp = p.resolve()
        # Dedupe by resolved path (handles symlinks / relative forms) but
        # return the caller's original Path object.
        if rp in seen:
            continue
        seen.add(rp)
        ordered.append(p)
    return ordered


def _loose_ttl_default(cfg: Config, gcfg: GlobalConfig) -> str | None:
    """The repo's effective loose TTL as prompt text, None when disabled."""
    policy = cfg.effective_loose_auto_revert(gcfg)
    return format_loose_after(policy.after) if policy is not None else None


def _global_config_or_defaults() -> GlobalConfig:
    """Load the global config, falling back to defaults on any error.

    The dashboard is a read-only viewer refreshed on a timer; an unreadable
    or invalid ``global.yaml`` must degrade to defaults rather than abort the
    gather (the CLI, which can report and exit, is stricter). A typo'd
    column name is no longer one of those errors — `load_global_config`
    recovers from it and hands back the sanitized config (valid names
    honoured, invalid ones dropped) rather than the whole block being lost;
    this only still degrades to `GlobalConfig()` on a genuine host-level
    schema problem. The dropped names are logged rather than surfaced in
    the UI — the dashboard has no confirmation-free place to print a
    warning on every refresh tick.
    """
    try:
        gcfg, dropped = load_global_config(default_global_config_path())
    except Exception:  # ConfigError, OSError — any of them means "use defaults"
        return GlobalConfig()
    if dropped:
        log.debug("global config: %s", "; ".join(dropped))
    return gcfg


def seed_view_state(engine: Engine, frontend: str) -> ViewState:
    """``frontend``'s view state, seeding its columns on first use.

    The ``dashboard:`` config block is deprecated. It is read exactly once
    per front-end — here — so that upgrading changes nobody's columns, and is
    inert afterwards: a later edit to the YAML must not reach back into a
    front-end the user has since configured through its own UI.

    Only the **global** layer is consulted. The seeded value becomes a
    personal setting that applies in every repo, so seeding it from whichever
    repo the user happened to launch from first would let one repo's block
    silently define their view everywhere. A repo-level block is reported as
    deprecated *and* as not seeded by ``Config.validate_runtime``.

    A stored column set is filtered against :func:`all_column_names` on the
    way out, falling back to :func:`default_columns` if nothing survives —
    ``decode_names`` only validates JSON shape, not column vocabulary, so a
    renamed or removed column would otherwise reach both front-ends raw. Each
    front-end's own last-column guard (``dashboard_settings.toggle_current``
    here, ``MainWindow._toggle_column`` in the Qt window) counts the *stored*
    length, so a phantom name inflates that count without ever being a real,
    keepable column — reaching zero real columns from a single ordinary
    toggle. Filtering here, before either guard sees the set, is what keeps
    that count honest.

    This function itself never writes: the filtered value is only returned,
    not saved back over the stored row. That does **not** mean an unknown
    name survives in storage, though — the filtered value becomes the
    long-lived ``enabled`` / ``self._enabled_columns`` each front-end holds
    for the rest of the session, and *unrelated* actions save that same
    value verbatim (folding a repo group, in both the TUI and the Qt
    window, saves a `ViewState` built from it). So the first save triggered
    by anything, not just a columns edit, drops the unknown name from
    storage for good. A column removed in one release and reintroduced in
    a later one will not come back for a user who reopens the dashboard and
    triggers any such save in between. This is accepted, not an oversight:
    preserving it would mean threading an unfiltered set through both
    front-ends' save sites, or teaching :mod:`jailbee.db.view_prefs` the
    column vocabulary it deliberately knows nothing about, for a narrow
    scenario not judged worth that machinery.
    """
    state = load_view_state(engine, frontend)
    if state.columns is not None:
        known = frozenset(all_column_names())
        filtered = tuple(n for n in state.columns if n in known)
        return replace(state, columns=filtered or default_columns())
    gcfg = _global_config_or_defaults()
    seeded = replace(state, columns=enabled_from_column_config(gcfg.dashboard))
    save_view_state(engine, frontend, seeded)
    return seeded


def gather_rows(
    incus: Incus,
    config_paths: list[Path],
    *,
    cwd_config: Path | None,
    with_git: bool,
) -> list[RepoGroup]:
    """Build per-repo groups, then append orphan groups.

    Each path's own config drives accurate git-status/base/background-jobs.
    An unloadable config is skipped (read-only — the registry is never
    pruned here). A final ``all_repos=True`` scan surfaces jailbee-managed
    containers whose repo we could not load, as view-only orphan groups.
    """
    groups: list[RepoGroup] = []
    covered: set[str] = set()
    base_cfg = None
    gcfg = _global_config_or_defaults()
    for path in config_paths:
        try:
            cfg = load_config(path)
        except Exception:  # many failure modes: OSError, YAML parse, Pydantic validation
            continue
        if base_cfg is None:
            base_cfg = cfg
        containers = list_containers(
            cfg,
            incus,
            all_repos=False,
            with_git_status=with_git,
            with_background=True,
        )
        covered.add(cfg.container_prefix)
        if containers:
            groups.append(
                RepoGroup(
                    cfg.container_prefix,
                    str(cfg.repo_root),
                    path,
                    containers,
                    ide_enabled=cfg.jetbrains.enabled,
                    chrome_enabled=cfg.chrome.enabled,
                    loose_ttl_default=_loose_ttl_default(cfg, gcfg),
                    push_action_default=cfg.push.default_action,
                    push_source_default=cfg.push.default_source,
                )
            )

    if base_cfg is not None:
        all_rows = list_containers(
            base_cfg,
            incus,
            all_repos=True,
            with_git_status=False,
            with_background=False,
        )
        orphans: dict[str, list[ContainerInfo]] = {}
        for c in all_rows:
            # `c.repo is None` is defensive AND narrows str|None -> str for
            # the dict key below (list_containers in practice always sets it).
            if c.repo is None or c.repo in covered:
                continue
            orphans.setdefault(c.repo, []).append(c)
        for prefix in sorted(orphans):
            groups.append(RepoGroup(prefix, None, None, orphans[prefix]))

    def _sort_key(g: RepoGroup) -> tuple[bool, bool, str]:
        is_cwd = cwd_config is not None and g.config_path == cwd_config
        return (not is_cwd, g.config_path is None, g.prefix)

    groups.sort(key=_sort_key)
    return groups


def gather_live(incus: Incus, cwd_config: Path | None, *, with_git: bool) -> list[RepoGroup]:
    """One snapshot for a *live* dashboard: config paths re-resolved per gather.

    Both dashboards refresh on a timer, and the set of registered repos moves
    underneath them: `jailbee new` registers a repo the first time it is used
    (`cli.py`), and `egress_pool.refresh_all` unregisters — then a later
    command re-registers — a repo whose config file momentarily disappeared.
    A path list captured at launch therefore goes stale, and a repo missing
    from it is not merely absent: `gather_rows`'s ``all_repos`` scan still
    finds its containers and files them under a view-only orphan group, where
    ``actions_for_container`` yields no actions and the right-click menu never
    opens. Re-resolving here is what keeps that self-healing instead of
    requiring a dashboard restart.

    The registry read is a single indexed SQLite select against a WAL
    database — cheap next to the `incus list` (and git probes) in the gather
    it precedes.
    """
    return gather_rows(
        incus, collect_config_paths(cwd_config), cwd_config=cwd_config, with_git=with_git
    )


def carry_forward_git_status(new_groups: list[RepoGroup], prev_groups: list[RepoGroup]) -> None:
    """Copy last-known git_status into a fresh base-refresh snapshot.

    A base (non-git) gather leaves every ContainerInfo.git_status None. To
    avoid the git columns flickering blank between git-tier refreshes, fill
    each still-None git_status from the container of the same name in the
    previous snapshot (if it had one). Mutates new_groups in place.
    """
    prev_status = {
        c.name: c.git_status for g in prev_groups for c in g.containers if c.git_status is not None
    }
    for g in new_groups:
        for c in g.containers:
            if c.git_status is None and c.name in prev_status:
                c.git_status = prev_status[c.name]


@dataclass(frozen=True)
class Row:
    """One cursor stop in the dashboard: a repo header or a container.

    Headers are selectable so a folded group can be reached and unfolded, and
    so the cursor behaves like the tree it is drawing. ``key`` is the repo
    prefix for a header and the container name for a container — the two
    namespaces are kept apart by ``kind`` rather than by a sentinel prefix,
    which would break the moment a container name looked like a repo one.
    """

    kind: Literal["repo", "container"]
    key: str


def selectable_rows(groups: list[RepoGroup], folded: frozenset[str] = frozenset()) -> list[Row]:
    """Cursor stops in display order.

    Every non-empty group contributes its header, folded or not; a folded
    group contributes none of its containers. An empty group contributes
    nothing at all, because :func:`render` draws no header for one and a
    cursor stop on an invisible row is a dead keypress.
    """
    rows: list[Row] = []
    for g in groups:
        if not g.containers:
            continue
        rows.append(Row("repo", g.prefix))
        if g.prefix in folded:
            continue
        rows += [Row("container", c.name) for c in g.containers]
    return rows


def move_selection(rows: list[Row], current: Row | None, delta: int) -> Row | None:
    """Move the highlight by ``delta`` rows, clamped at both ends."""
    if not rows:
        return None
    if current not in rows:
        return rows[0]
    idx = rows.index(current)
    return rows[max(0, min(len(rows) - 1, idx + delta))]


def reconcile_selection(rows: list[Row], current: Row | None, last_index: int) -> Row | None:
    """Keep ``current`` if still present; else clamp ``last_index`` into the
    refreshed list (nearest remaining row). None when the list is empty."""
    if not rows:
        return None
    if current in rows:
        return current
    return rows[min(last_index, len(rows) - 1)]


def container_of(row: Row | None) -> str | None:
    """The container a row acts on, or None for a header or no selection.

    Every action path (``open_menu``, ``quick_verb``, ``view_only_note``,
    ``dispatch``) takes a container name, so a header row narrows to None
    here and falls into their existing "nothing selected" handling rather
    than each of them learning about rows.
    """
    return row.key if row is not None and row.kind == "container" else None


def fold_target(groups: list[RepoGroup], row: Row | None) -> str | None:
    """The repo prefix a fold key should act on for ``row``, else None.

    Accepts either kind of row: folding from inside a group is the common
    gesture ("get this out of my way"), and requiring the cursor to be on the
    header first would make the key feel arbitrary.
    """
    if row is None:
        return None
    if row.kind == "repo":
        return row.key
    group = _find_group(groups, row.key)
    return group.prefix if group is not None else None


def toggle_folded(folded: frozenset[str], prefix: str) -> frozenset[str]:
    """``folded`` with ``prefix`` flipped."""
    return folded - {prefix} if prefix in folded else folded | {prefix}


_NETWORK_MODES: tuple[str, ...] = ("strict", "loose")


@dataclass(frozen=True)
class MenuContext:
    """Everything :func:`menu_actions` needs to know about one row.

    Assembled by :func:`actions_for_container` from a ``ContainerInfo`` +
    ``RepoGroup`` pair. A dataclass rather than a tenth keyword argument: the
    call sites had already stopped being readable, and every field here is a
    plain fact about the row rather than an option.

    ``has_job`` is "there is a background-job row at all" (what makes the log
    worth offering); ``job_running`` is "its worker is still alive" (what makes
    ``--follow`` the right form); ``job_clearable`` is the failed/stale case
    that "Clear failed job" corrects.

    ``pr_author`` splits the PR containers the way the PR column's ``↓`` marker
    already does: False is a container built from someone else's PR (a review),
    True one whose PR jailbee opened from the container's own branch.
    """

    state: str
    has_config: bool
    mode: str = "clone"
    ide_enabled: bool = False
    chrome_enabled: bool = False
    current_network: str | None = None
    pr_number: int | None = None
    pr_author: bool = False
    job_clearable: bool = False
    has_job: bool = False
    job_running: bool = False
    git_status: GitStatus | None = None


# The GitStatus cell values that mean "there is provably nothing to do". Every
# other value — including "—" and "?" — means unknown, and an unknown answer
# never hides an entry.
_NO_COMMITS = "0"
_NO_CHANGES = "clean"


def _bridge_possible(ctx: MenuContext) -> bool:
    """Whether the PR and git-bridge verbs can run for this row at all.

    They all read the container's own clone, so they need a running container
    that has one: ``sync.assert_container_publishable`` rejects a stopped or
    mount-mode container up front, and offering an entry whose only outcome is
    that error is worse than not offering it.
    """
    return ctx.state == "Running" and ctx.mode != "mount"


def _has_commits_for_host(git: GitStatus | None) -> bool:
    """Whether `jailbee git pull` has commits to send to the host."""
    return git is None or git.ahead_count != _NO_COMMITS


def _has_diff_to_show(git: GitStatus | None) -> bool:
    """Whether `jailbee git diff` would print anything."""
    if git is None:
        return True
    return not (git.wt == _NO_CHANGES and git.ahead_count == _NO_COMMITS)


def menu_actions(ctx: MenuContext) -> list[tuple[str, str]]:
    """(label, jailbee-subcommand) options for the highlighted container.

    Empty for orphan rows (no loadable config ⇒ no safe dispatch). "Launch
    IDE"/"Launch Chrome" only appear when the repo's own config enables
    `jetbrains`/`chrome` respectively (``ide_enabled``/``chrome_enabled``,
    sourced from ``RepoGroup.ide_enabled``/``chrome_enabled``) — dispatching
    `jailbee ide`/`jailbee chrome` when the feature is disabled would just fail.

    For running containers, one "Network: <mode>" entry appears per mode
    other than ``ctx.current_network`` (sourced from ``ContainerInfo.network``),
    dispatching the two-token ``jailbee net <mode>`` subcommand.

    The head of the list is the diagnostic/workflow block, deliberately far
    from "Destroy" at the bottom: "Clear failed job" (the corrective action),
    "Job log", "Open PR", then the four verbs that carry the actual workflow —
    create/update the PR, update the container from its base, send its commits
    back to the host, and read its diff. The last two are hidden when the git
    status proves they would do nothing; an unknown status (base-tier refresh,
    ``--no-git``, failed probe) still offers them, because a missing column is
    not evidence of a clean tree.

    A review container — one carrying a PR that jailbee did not open from its
    own branch (``pr_number`` set, ``pr_author`` false) — gains "Refresh from
    PR head" beside the base update: the same `git push`, sourced from the PR
    instead of the base branch. It is withheld from an authored PR, whose head
    the container's branch is upstream of, so the refresh could only be a
    no-op.

    Verbs may carry flags (``"pr --open"``, ``"job log --follow"``): every
    front-end splits them into argv, and Typer accepts options before the
    positional container name.
    """
    if not ctx.has_config:
        return []
    prefix: list[tuple[str, str]] = []
    if ctx.job_clearable:
        prefix.append(("Clear failed job", "job clear"))
    if ctx.has_job:
        prefix.append(("Job log", "job log --follow" if ctx.job_running else "job log"))
    if ctx.pr_number is not None:
        prefix.append(("Open PR", "pr --open"))
    if _bridge_possible(ctx):
        prefix.append(("Create/update PR", "pr"))
        prefix.append(("Update from base (git push)", "git push"))
        if ctx.pr_number is not None and not ctx.pr_author:
            prefix.append(("Refresh from PR head (git push --pr)", "git push --pr"))
        if _has_commits_for_host(ctx.git_status):
            prefix.append(("Send commits to host (git pull)", "git pull"))
        if _has_diff_to_show(ctx.git_status):
            prefix.append(("Show diff (git diff)", "git diff"))
    if ctx.state == "Running":
        actions = [
            ("Attach tmux", "tmux"),
            ("Open shell", "shell"),
        ]
        if ctx.ide_enabled:
            actions.append(("Launch IDE", "ide"))
        if ctx.chrome_enabled:
            actions.append(("Launch Chrome", "chrome"))
        for mode in _NETWORK_MODES:
            if mode != ctx.current_network:
                actions.append((f"Network: {mode}", f"net {mode}"))
        actions += [
            ("Restart", "restart"),
            ("Stop", "stop"),
            ("Destroy", "destroy"),
        ]
        return prefix + actions
    if ctx.state == "Stopped":
        return [*prefix, ("Start", "start"), ("Destroy", "destroy")]
    return [*prefix, ("Destroy", "destroy")]


def default_columns() -> tuple[str, ...]:
    """The built-in dashboard column set, in canonical field-spec order.

    What a front-end renders before anyone has touched its settings, and the
    reset target. `DASHBOARD_DEFAULT_HIDE` names the columns the dashboards
    drop from the `ls` set: REPO is redundant under per-repo grouping, the
    wide GIT STATUS combo and the JSON-only full_name add noise, and TTL is
    folded into the NETWORK cell.
    """
    specs = ls_field_specs(now=datetime.now(UTC), all_repos=False)
    return tuple(
        f.name
        for f in specs
        if table_format.shows_by_default_in_dashboard(f) and f.name not in DASHBOARD_DEFAULT_HIDE
    )


def enabled_from_column_config(columns: ColumnConfig) -> tuple[str, ...]:
    """Resolve a legacy ``dashboard:`` block into an enabled-name tuple.

    The one remaining dashboard use of ``table_format.apply_column_config``,
    confined to seeding a front-end's `view_prefs` row from the deprecated
    config block (see ``seed_view_state``). Going through the old resolver is
    what guarantees the seeded set is *exactly* what that block used to
    render, including its two quirks: an explicit ``fields`` list wins
    outright, and ``hide`` replaces the built-in list rather than extending
    it.

    That guarantee holds fully for a ``fields:`` block — naming a column
    forces ``default_dashboard=True`` on it (see
    ``table_format.apply_column_config``), overriding whatever the current
    built-in default says. It does **not** hold for a ``hide:``-shaped
    block (``fields`` empty/absent): a column *not* named in ``hide``
    passes through with its current spec unchanged, so its inclusion here
    is decided by :func:`table_format.shows_by_default_in_dashboard` as it
    stands *today* — not as it stood when the block was written. IP left
    the dashboard defaults in this same release (Part 1), so a ``hide:``
    block that never mentioned ``ip`` seeds a set without it, even though
    that block used to render IP for its user.
    """
    resolved = table_format.apply_column_config(
        ls_field_specs(now=datetime.now(UTC), all_repos=False),
        fields=columns.fields,
        hide=columns.hide,
    )
    return tuple(f.name for f in resolved if table_format.shows_by_default_in_dashboard(f))


def all_column_names() -> tuple[str, ...]:
    """Every real column name, in canonical order — the Fields tab's list.

    The same vocabulary ``jailbee ls --fields`` accepts, including columns off
    by default in both views (``full_name``, ``git_status``, ``ip``, …): an
    enabled set decides inclusion by membership, so any of them can be turned
    on. ``repo`` is redundant under per-repo grouping but is not special-cased
    — the user may want it.
    """
    return tuple(f.name for f in ls_field_specs(now=datetime.now(UTC), all_repos=False))


def dynamic_column_names() -> frozenset[str]:
    """Columns whose ``show_if`` can prune them even when enabled.

    The settings overlay marks these so that an enabled column which does not
    appear reads as the emptiness heuristic working, not as a bug.
    """
    specs = ls_field_specs(now=datetime.now(UTC), all_repos=False)
    return frozenset(f.name for f in specs if f.show_if is not None)


def settings_repo_prefixes(groups: list[RepoGroup], folded: frozenset[str]) -> tuple[str, ...]:
    """The Repos tab's list: what is on screen, plus what is folded away.

    A folded repo whose containers have since gone draws no group at all, so
    listing only ``groups`` would leave it folded forever with no way back.
    Deduped, on-screen groups first, absent folded prefixes sorted after them.

    This is a snapshot taken once, when the overlay opens (see
    ``open_settings_overlay`` in ``run()``) — a repo registered or a
    container created/destroyed while the Repos tab is open does not appear
    or disappear from the list until the overlay is closed and reopened.
    """
    on_screen = [g.prefix for g in groups if g.containers]
    return tuple(dict.fromkeys(on_screen + sorted(folded)))


def visible_fields(
    now: datetime,
    all_containers: list[ContainerInfo],
    enabled: Sequence[str] | None = None,
) -> list[FieldSpecCI]:
    """The dashboard's visible columns, honouring each field's ``show_if``.

    ``enabled`` is the front-end's enabled-name set; ``None`` means
    :func:`default_columns`. Membership decides inclusion — not
    ``default_table``, which is why a column off by default everywhere can
    be turned on here — and the field-spec list's own order decides
    rendering order, so a stored list's order is not significant.

    ``show_if`` applies to every column, enabled or not. This is the
    deliberate difference from ``jailbee ls --fields``, where naming a column
    clears its ``show_if`` (see ``table_format.apply_column_config``): there,
    a name is a one-shot request; here it is a standing preference, and the
    four dynamic columns (``job``, ``ttl``, ``pr``, ``mode``) would otherwise
    render permanently empty for anyone who enabled them. The settings UI
    marks those rows so the pruning does not read as a bug.

    Unknown names are skipped rather than rejected — a stored set can outlive
    a renamed column, and view state must not break the view.

    The ``network`` field is swapped for a dashboard-specific one whose cell
    folds the loose TTL inline (e.g. ``"loose (12m)"``); that is why the
    standalone TTL column is not in the default set.

    Shared by the TUI ``render`` and both Qt views, so all three show the
    same columns for the same enabled set.
    """

    def _network_cell(c: ContainerInfo) -> str:
        if c.network != "loose":
            return c.network or "-"
        if c.loose_until is None:
            return f"{c.network} (—)"
        return f"{c.network} ({format_duration_short(c.loose_until - now)})"

    wanted = frozenset(default_columns() if enabled is None else enabled)
    fields = [
        f
        for f in ls_field_specs(now=now, all_repos=False)
        if f.name in wanted and (f.show_if is None or f.show_if(all_containers))
    ]
    return [replace(f, cell=_network_cell) if f.name == "network" else f for f in fields]


_KEY_READ_BYTES = 8  # covers all standard arrow/function-key CSI sequences
_NOTICE_SECONDS = 2.5  # how long a transient subtitle message stays up


@dataclass(frozen=True)
class KeyBinding:
    """One dashboard key: how it is typed, what it does, how it is described.

    :data:`KEY_BINDINGS` is the single source for all three — :func:`parse_key`
    is built from ``keys``, the quick-action gate from ``verb``, the help
    overlay from ``hint``/``label``/``group``, and the always-visible hint line
    from ``brief``. Three hand-maintained lists would drift.

    ``hint`` is empty for a token whose sibling documents it (``down`` is
    covered by ``up``'s "↑/↓ (j/k)"). ``brief`` is the terse word used in the
    hint line, or None to keep the key in the help overlay only.
    """

    token: str
    keys: tuple[bytes, ...]
    hint: str
    label: str
    group: str
    verb: str | None = None
    brief: str | None = None


KEY_BINDINGS: tuple[KeyBinding, ...] = (
    KeyBinding(
        "up", (b"\x1b[A", b"k"), "↑/↓ (j/k)", "move the highlight", "Navigate", brief="move"
    ),
    KeyBinding("down", (b"\x1b[B", b"j"), "", "", "Navigate"),
    KeyBinding("enter", (b"\r", b"\n"), "Enter", "open the action menu", "Navigate", brief="menu"),
    KeyBinding("cancel", (b"\x1b",), "Esc", "close the menu or help", "Navigate"),
    KeyBinding(
        "fold",
        (b" ",),
        "Space",
        "fold/unfold the repo group",
        "Navigate",
        brief="fold",
    ),
    KeyBinding("action:tmux", (b"t",), "t", "attach tmux", "Actions", verb="tmux", brief="tmux"),
    KeyBinding(
        "action:shell", (b"s",), "s", "open a shell", "Actions", verb="shell", brief="shell"
    ),
    KeyBinding("action:ide", (b"i",), "i", "launch the IDE", "Actions", verb="ide"),
    KeyBinding("action:chrome", (b"c",), "c", "launch Chrome", "Actions", verb="chrome"),
    KeyBinding("action:pr", (b"p",), "p", "open the PR", "Actions", verb="pr --open"),
    KeyBinding("action:pr-update", (b"P",), "P", "create or update the PR", "Actions", verb="pr"),
    KeyBinding("action:push", (b"u",), "u", "update from base", "Actions", verb="git push"),
    KeyBinding("action:diff", (b"d",), "d", "show the diff", "Actions", verb="git diff"),
    # Repo-scoped, not container-scoped: no `verb`, so it never reaches
    # `quick_verb`/`actions_for_container` (those gate on a container's state).
    # `run`'s dispatch handles it directly, with its own guard.
    KeyBinding("new", (b"n",), "n", "create a container in this repo", "Actions", brief="new"),
    KeyBinding("refresh", (b"r",), "r", "force a full refresh", "View", brief="refresh"),
    KeyBinding(
        "settings",
        (b"\x1bOQ", b"\x1b[12~", b"S"),
        "F2 / S",
        "columns and repo folding",
        "View",
        brief="settings",
    ),
    KeyBinding("tab", (b"\t",), "", "", "View"),
    KeyBinding("help", (b"h", b"?"), "h / ?", "this help", "View", brief="help"),
    KeyBinding("quit", (b"q",), "q", "quit (closes an overlay first)", "View", brief="quit"),
    # b"" is a zero-length read: stdin hit EOF, so there is nothing left to quit to.
    KeyBinding("interrupt", (b"\x03", b""), "Ctrl-C", "quit immediately", "View"),
)

_KEY_TOKENS: dict[bytes, str] = {k: b.token for b in KEY_BINDINGS for k in b.keys}

_GATE_NOTE = (
    "Action keys only fire when that action is offered for the highlighted "
    "container: a stopped container has no tmux or shell, the IDE and Chrome "
    "need the repo's own jetbrains/chrome config, the PR key needs a known PR, "
    "the workflow keys need a running clone-mode container (and the diff key "
    "needs something to show), and orphan rows are view-only."
)


def binding_for_token(token: str) -> KeyBinding | None:
    """The binding a :func:`parse_key` token came from (None if unmapped)."""
    return next((b for b in KEY_BINDINGS if b.token == token), None)


def quick_verb(groups: list[RepoGroup], name: str | None, token: str) -> str | None:
    """The verb a quick-action key should dispatch for ``name``, else None.

    None covers both "not an action key" and "that action isn't offered here".
    The gate is :func:`actions_for_container`, so ``menu_actions`` stays the
    only place that decides what a container allows — a quick key can never
    reach an action its own menu would not show.
    """
    binding = binding_for_token(token)
    if binding is None or binding.verb is None:
        return None
    offered = {verb for _label, verb in actions_for_container(groups, name)}
    return binding.verb if binding.verb in offered else None


@dataclass
class MenuState:
    """An open action menu, rendered inline under the dashboard table.

    ``actions`` is captured when the menu opens rather than recomputed per
    frame: the dashboard keeps refreshing behind the menu, and a list that
    re-derived itself from live state would reorder rows under the cursor
    mid-keystroke. The staleness that buys is bounded — dispatching a verb
    the container has since outgrown just lets the real ``jailbee`` command
    report the problem, exactly as the previous questionary menu did.
    """

    container: str
    actions: list[tuple[str, str]]
    index: int = 0


# What occupies the slot under the table. All three overlays are mutually
# exclusive by construction — no combination of them is a representable state.
Overlay = MenuState | SettingsState | Literal["help"]


def open_menu(groups: list[RepoGroup], name: str | None) -> MenuState | None:
    """The menu for ``name``, or None when there is nothing to show.

    None covers every no-actions case — unknown container, nothing selected,
    or a view-only (config-less) group. Callers surface :func:`view_only_note`
    instead, because an empty menu frame is indistinguishable from a broken one.
    """
    actions = actions_for_container(groups, name)
    if name is None or not actions:
        return None
    return MenuState(name, actions)


def move_menu(menu: MenuState, delta: int) -> MenuState:
    """Move the menu cursor by ``delta``, clamped at both ends."""
    last = max(0, len(menu.actions) - 1)
    return replace(menu, index=max(0, min(last, menu.index + delta)))


def menu_verb(menu: MenuState) -> str | None:
    """The highlighted entry's ``jailbee`` verb (None for an empty menu)."""
    if not menu.actions:
        return None
    return menu.actions[menu.index][1]


def _render_menu(menu: MenuState) -> RenderableType:
    """The action menu as a bordered panel: one row per action, cursor on the
    highlighted one."""
    lines = [
        f"[bold cyan]▸[/] [bold bright_white]{label}[/]" if i == menu.index else f"  {label}"
        for i, (label, _verb) in enumerate(menu.actions)
    ]
    return Panel(
        "\n".join(lines),
        title=f"[bold]{menu.container}[/] →",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        expand=False,
    )


def _render_help() -> RenderableType:
    """The keybinding help as a bordered panel, grouped as the table declares.

    Rows come from :data:`KEY_BINDINGS`, so a new key documents itself. The
    closing note explains why an action key can decline to fire — without it
    a correctly-gated key looks broken.
    """
    width = max((len(b.hint) for b in KEY_BINDINGS if b.hint), default=0)
    lines: list[str] = []
    for group in dict.fromkeys(b.group for b in KEY_BINDINGS):
        if lines:
            lines.append("")
        lines.append(f"[bold cyan]{group}[/]")
        lines += [
            f"  [bold]{b.hint:<{width}}[/]  {b.label}"
            for b in KEY_BINDINGS
            if b.group == group and b.hint
        ]
    lines += ["", f"[dim]{_GATE_NOTE}[/dim]"]
    return Panel(
        "\n".join(lines),
        title="[bold]keys[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        width=72,
    )


def quick_reject_note(groups: list[RepoGroup], name: str | None, token: str) -> str:
    """Why a quick-action key did nothing, as one user-facing sentence.

    A key that silently declines is indistinguishable from a broken one, and
    the reason matters: a view-only row explains itself differently from a
    stopped container or a repo with the IDE turned off.
    """
    if name is None:
        return "No container is selected"
    note = view_only_note(groups, name)
    if note is not None:
        return note
    binding = binding_for_token(token)
    what = f"'{binding.hint}' ({binding.label})" if binding is not None else f"'{token}'"
    return f"{what} is not available for '{name}'"


def _hint_line(overlay: Overlay | None) -> str:
    """The keybinding hint shown on the last line of the panel body."""
    if isinstance(overlay, MenuState):
        return "[bold]↑/↓[/bold] move  ·  [bold]Enter[/bold] run  ·  [bold]Esc[/bold] cancel"
    if isinstance(overlay, SettingsState):
        return (
            "[bold]↑/↓[/bold] move  ·  [bold]Space[/bold] toggle  ·  "
            "[bold]Tab[/bold] switch  ·  [bold]Esc[/bold] close"
        )
    if overlay is not None:  # "help"
        return "[bold]Esc[/bold] / [bold]h[/bold] close"
    return "  ·  ".join(
        f"[bold]{b.hint}[/bold] {b.brief}" for b in KEY_BINDINGS if b.brief is not None
    )


def render(
    groups: list[RepoGroup],
    selected: Row | None,
    *,
    now: datetime,
    last_refresh_age: float,
    interval: float,
    git_enabled: bool,
    enabled: Sequence[str] | None = None,
    overlay: Overlay | None = None,
    notice: str | None = None,
    folded: frozenset[str] = frozenset(),
) -> RenderableType:
    """Build the Rich renderable for one dashboard frame.

    One shared table (columns aligned across all repos); each repo is a
    section header row inside it. The selected container is marked with a
    ``▸`` gutter arrow and bold styling. Wrapped in a rounded Panel whose
    left-aligned title carries the summary, the clock and a fixed-width
    refresh field; the subtitle carries a transient notice and nothing else.

    ``overlay`` is an open action menu or the keybinding help, drawn *below*
    the table so the dashboard it acts on stays on screen. ``notice`` is a
    transient message
    (a rejected key, a view-only row) shown in the subtitle; the keybinding
    hint lives in the panel body, where Rich can wrap it instead of the
    subtitle silently clipping it.
    """
    all_containers = [c for g in groups for c in g.containers]
    visible = [c for g in groups if g.prefix not in folded for c in g.containers]
    fields = visible_fields(now, visible, enabled)

    table = Table(box=None, pad_edge=False, expand=False, show_edge=False)
    for i, f in enumerate(fields):
        # The *first* column carries a 2-char arrow gutter on data rows,
        # whichever field that happens to be — the settings overlay lets
        # `name` be disabled, so the gutter cannot be pinned to that field
        # by name. Pad its header so the column lines up.
        header = ("  " + f.header) if i == 0 else f.header
        table.add_column(header, justify=f.justify)

    if not all_containers:
        table.add_row("(no containers found)", *([""] * (len(fields) - 1)))
    else:
        first_group = True
        for g in groups:
            if not g.containers:
                continue
            if not first_group:
                table.add_row(*([""] * len(fields)))  # blank spacer between groups
            first_group = False
            is_folded = g.prefix in folded
            marker = "▸" if is_folded else "▾"
            is_orphan = g.repo_root is None
            label = f"{marker} {g.prefix}  ({len(g.containers)})"
            if is_orphan:
                label += "  (orphan)"
            label_style = "bold yellow" if is_orphan else "bold cyan"
            header_sel = selected is not None and selected == Row("repo", g.prefix)
            gutter = "[bold cyan]▸[/] " if header_sel else "  "
            table.add_row(
                gutter + f"[{label_style}]{label}[/]",
                *([""] * (len(fields) - 1)),
                style="bold bright_white" if header_sel else None,
            )
            if is_folded:
                continue
            for c in g.containers:
                is_sel = (
                    selected is not None and selected.kind == "container" and selected.key == c.name
                )
                cells: list[str] = []
                for i, f in enumerate(fields):
                    # `name` shows the full container name (not the
                    # repo-prefix-stripped display name) for an orphan row,
                    # since there is no known repo to have stripped a prefix
                    # from — independent of whether `name` happens to be the
                    # first column.
                    value = c.name if (f.name == "name" and is_orphan) else f.cell(c)
                    if i == 0:
                        gutter = "[bold cyan]▸[/] " if is_sel else "  "
                        value = gutter + value
                    cells.append(value)
                table.add_row(*cells, style="bold bright_white" if is_sel else None)

    body: list[RenderableType] = [table, ""]
    if overlay is not None:
        if isinstance(overlay, MenuState):
            panel = _render_menu(overlay)
        elif isinstance(overlay, SettingsState):
            panel = render_settings(overlay, dynamic=dynamic_column_names())
        else:
            panel = _render_help()
        body += [panel, ""]
    body.append(_hint_line(overlay))

    n_repos = len({g.prefix for g in groups})
    n_ctr = len(all_containers)
    n_folded = len({g.prefix for g in groups if g.prefix in folded and g.containers})
    folded_note = f" · {n_folded} folded" if n_folded else ""
    git_note = "" if git_enabled else "  ·  [dim](no-git)[/dim]"
    # Fixed-width refresh field: the age is clamped to two digits and the
    # interval is constant for the run, so the title never changes width as
    # the age ticks — a title that resizes drags the whole line with it.
    age_field = f"{min(last_refresh_age, 99.0):>2.0f}s/{interval:.0f}s"
    title = (
        f"[bold]jailbee dashboard[/]  ·  {n_repos} repos · {n_ctr} containers"
        f"{folded_note}{git_note}  ·  {now:%H:%M:%S}  ·  [dim]↻[/dim] {age_field}"
    )
    # Subtitle is notice-only: a transient message on the bottom border cannot
    # push the table around, and the refresh timing now lives in the title.
    subtitle = f"[yellow]{notice}[/yellow]" if notice else None
    return Panel(
        Group(*body),
        title=title,
        title_align="left",
        subtitle=subtitle,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def parse_key(data: bytes) -> str:
    """Map a raw stdin read to a dashboard key token ('' if unmapped).

    A pure lookup into :data:`KEY_BINDINGS`, so a key cannot be readable
    without also being documented in the help overlay.

    Note the three ways out: ``cancel`` (bare Esc — arrows arrive as
    ``\\x1b[…``) and ``quit`` (``q``) close an open overlay first, while
    ``interrupt`` (Ctrl-C, EOF) always ends the dashboard. Folding them into
    one token would leave Ctrl-C unable to do anything but shut the menu.
    """
    return _KEY_TOKENS.get(data, "")


def _find_group(groups: list[RepoGroup], name: str | None) -> RepoGroup | None:
    for g in groups:
        for c in g.containers:
            if c.name == name:
                return g
    return None


_TERMINAL_TITLE_FALLBACK = "🐝 jailbee"


def terminal_title(groups: list[RepoGroup], selected: Row | None) -> str:
    """The xterm/tmux window title for the current selection.

    ``🐝 <repo>/<container>`` on a container row, ``🐝 <repo>`` on a repo
    header, and the bare tool name when nothing is selected or the selected
    container has vanished under the cursor. An orphan group's container shows
    its *full* name, matching the NAME column — there is no known repo prefix
    to have stripped.
    """
    if selected is None:
        return _TERMINAL_TITLE_FALLBACK
    if selected.kind == "repo":
        return f"🐝 {selected.key}"
    group = _find_group(groups, selected.key)
    if group is None:
        return _TERMINAL_TITLE_FALLBACK
    container = next((c for c in group.containers if c.name == selected.key), None)
    if container is None:
        return _TERMINAL_TITLE_FALLBACK
    name = container.name if group.repo_root is None else container.display_name
    return f"🐝 {group.prefix}/{name}"


def set_terminal_title(text: str, *, stream: TextIO) -> None:
    """Write one OSC 2 window-title sequence.

    Best-effort: a terminal that does not implement it drops the sequence
    silently, so there is nothing to detect or guard against.
    """
    stream.write(f"\x1b]2;{text}\x07")
    stream.flush()


@contextmanager
def terminal_title_scope(stream: TextIO) -> Iterator[None]:
    """Save the terminal's own title on entry, restore it on exit.

    Uses the xterm title stack (``CSI 22;2t`` / ``CSI 23;2t``), implemented by
    xterm and tmux and ignored elsewhere. Without the pop the terminal would
    keep jailbee's title after the dashboard quits, since there is no way to
    read the old one back.
    """
    stream.write("\x1b[22;2t")
    stream.flush()
    try:
        yield
    finally:
        stream.write("\x1b[23;2t")
        stream.flush()


def actions_for_container(groups: list[RepoGroup], name: str | None) -> list[tuple[str, str]]:
    """Resolve the ``(label, verb)`` action list for a container by name.

    Single source of truth shared by the TUI action menu, the Qt table view,
    and the Qt card view. Returns ``[]`` for an unknown container or a
    view-only (config-less) group.
    """
    group = _find_group(groups, name)
    if group is None or name is None:
        return []
    container = next((c for c in group.containers if c.name == name), None)
    if container is None:
        return []
    from jailbee import background

    job_clearable = (
        container.job_phase is not None
        and container.job_pid is not None
        and background.clearable(container.job_phase, container.job_pid)
    )
    return menu_actions(
        MenuContext(
            state=container.state,
            has_config=group.config_path is not None,
            mode=container.mode,
            ide_enabled=group.ide_enabled,
            chrome_enabled=group.chrome_enabled,
            current_network=container.network,
            pr_number=container.pr_number,
            pr_author=container.pr_author,
            job_clearable=job_clearable,
            has_job=container.job_phase is not None,
            # A job row that is not clearable is one whose worker is still
            # alive — that is what makes `--follow` the right form.
            job_running=container.job_phase is not None and not job_clearable,
            git_status=container.git_status,
        )
    )


def view_only_note(groups: list[RepoGroup], name: str | None) -> str | None:
    """Why ``name`` offers no actions, as one user-facing sentence.

    ``None`` when there is nothing to explain: the container has actions, or
    it isn't on screen at all (a stale selection). Every front-end shows this
    the way its medium allows — a transient subtitle notice in the TUI, a
    disabled entry in the Qt menus — because an action menu that silently
    declines to open
    is indistinguishable from a broken one.
    """
    group = _find_group(groups, name)
    if group is None or group.config_path is not None:
        return None
    return f"No config loaded for repo '{group.prefix}' — '{name}' is view-only"


def new_container_target(groups: list[RepoGroup], selected: Row | None) -> RepoGroup | None:
    """The repo a new container would be created in, for the current selection.

    A container row yields its own group; a repo header yields that group.
    None when nothing is selected, when the selection is stale (the row moved
    out from under the cursor between frames), or when the group has no config
    to create against — an orphan group is jailbee-managed containers whose
    repo config could not be loaded, the same reason it gets no action menu.
    """
    if selected is None:
        return None
    if selected.kind == "repo":
        group = next((g for g in groups if g.prefix == selected.key), None)
    else:
        group = _find_group(groups, selected.key)
    if group is None or group.config_path is None:
        return None
    return group


def new_container_reject_note(groups: list[RepoGroup], selected: Row | None) -> str | None:
    """Why a container cannot be created here, or None when it can.

    The counterpart to :func:`view_only_note`: a front-end that silently does
    nothing is indistinguishable from a broken one, so every refusal has a
    sentence naming its own cause.
    """
    if new_container_target(groups, selected) is not None:
        return None
    if selected is None:
        return "Select a repo or a container first"
    group = (
        next((g for g in groups if g.prefix == selected.key), None)
        if selected.kind == "repo"
        else _find_group(groups, selected.key)
    )
    if group is None:
        return f"'{selected.key}' is no longer listed"
    return f"'{group.prefix}' has no jailbee config — nothing to create against"


def new_container_base_default(repo_root: str | None) -> str | None:
    """The branch ``repo_root``'s checkout is on, for the base field's default.

    Read from the *group's* repo, not the process's cwd: both dashboards are
    cross-repo, so the branch offered has to belong to the repo the row is in.
    None for a null root (an orphan group) or a detached HEAD — an empty field
    beats a guess, and `jailbee new` would fork off the wrong branch.
    """
    if repo_root is None:
        return None
    from jailbee import git

    return git.get_current_branch(Path(repo_root))


def new_container_argv(config_path: Path, branch: str, base: str) -> list[str]:
    """``jailbee new <branch> <base> --config <path>``.

    ``base`` is positional, not a flag: `jailbee new`'s second positional is
    the branch a *new* branch forks off (`lifecycle.resolve_clone_ref`).
    Omitted, a new branch forks off `cfg.default_branch` instead — which is
    not what someone picking their current branch means. (`--from-base` is the
    golden-image alias and has nothing to do with git.)

    No `--yes`: `jailbee new` asks about reusing an existing branch and about
    the branch-autostart escalation, and both front-ends give it a terminal to
    ask in rather than answering for the user.
    """
    return ["jailbee", "new", branch, base, "--config", str(config_path)]


# Verbs routed through the CLI's attach guard, which asks "continue anyway?"
# when the container's background job failed or is unfinished. Both dashboards
# have already shown that state in the JOB column, so the question would only
# ask the operator to re-read what they were looking at when they acted on the
# row — hence both dispatch these with `--force`. Shared rather than copied, for
# the same reason as :data:`PRINTING_VERBS` (`qtui/actions.py` imports this).
ATTACH_VERBS: frozenset[str] = frozenset({"shell", "tmux", "ide", "chrome"})

# Verbs whose whole point is the text they print, rather than the state they
# change. Both front-ends need to know which those are — the TUI to keep their
# output on screen, the Qt dashboard to route it into a window of its own
# (`qtui/actions.py` imports this) — so the list lives here, beside
# :func:`menu_actions`, which is where the verb vocabulary is defined.
#
# Matched exactly, not by leading token: `pr --open` only opens a browser, and
# `job log` and `git push` each appear in two forms.
PRINTING_VERBS: frozenset[str] = frozenset(
    {
        "pr",
        "git push",
        "git push --pr",
        "git pull",
        "git diff",
        "job log",
        "job log --follow",
    }
)

# The printing verbs long enough to want a pager instead of a pause. `Live`
# repaints the moment the dashboard resumes, so the rest get the pause: without
# one their output is gone before it can be read.
_PAGED_VERBS: frozenset[str] = frozenset({"git diff"})
_OUTPUT_VERBS: frozenset[str] = PRINTING_VERBS - _PAGED_VERBS

DispatchStyle = Literal["paged", "output", "plain"]


def dispatch_style(verb: str) -> DispatchStyle:
    """How the TUI should run ``verb``: through a pager, with a pause, or bare."""
    if verb in _PAGED_VERBS:
        return "paged"
    if verb in _OUTPUT_VERBS:
        return "output"
    return "plain"


def pager_argv() -> list[str] | None:
    """The pager to page long output through, or None when the host has none.

    ``$PAGER`` wins (split as a shell word list, so ``PAGER="bat -p"`` works);
    otherwise ``less -R``, which renders the ANSI colour the diff is asked to
    emit, then ``more``.
    """
    env = os.environ.get("PAGER")
    if env:
        return shlex.split(env)
    for candidate in (["less", "-R"], ["more"]):
        if shutil.which(candidate[0]):
            return candidate
    return None


def _wait_for_return() -> None:
    """Hold the terminal until the user has read the output.

    Called only from inside ``run``'s ``foreground`` helper, where ``Live`` is
    stopped and the terminal is back in cooked mode — so a plain read is
    enough. EOF (piped stdin, Ctrl-D) and Ctrl-C return immediately rather
    than propagating: neither is a reason to take the dashboard down.
    """
    console.print("\n[dim]── press Enter to return to the dashboard ──[/dim]")
    try:
        sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        pass


def _run_paged(argv: list[str], pager: list[str]) -> int:
    """Pipe ``argv``'s stdout into ``pager``; return the command's exit code.

    Two processes rather than a shell string, so there is no quoting to get
    wrong. Stderr stays attached to the terminal: an error message must not be
    swallowed by the pager. The pager owns the terminal until the user quits
    it, which is why the paged path needs no keypress pause of its own.
    """
    producer = subprocess.Popen(argv, stdout=subprocess.PIPE)
    out = producer.stdout
    if out is None:  # unreachable with stdout=PIPE; keeps mypy honest
        return producer.wait()
    try:
        viewer = subprocess.Popen(pager, stdin=out)
    except OSError:
        # The pager vanished between which() and exec. Nothing will ever read
        # the pipe, so kill the producer rather than leaving it blocked on a
        # full one, and let the caller fall back to the unpaged path.
        producer.kill()
        producer.wait()
        raise
    finally:
        # The viewer owns the read end now. Keeping this copy open would stop
        # the pager ever seeing EOF, so it would hang on a finished command.
        out.close()
    viewer.wait()
    return producer.wait()


def _dispatch_action(config_path: Path, verb: str, name: str) -> int:
    """Run ``jailbee <verb> <name> --config <config_path>``; return its exit code.

    The single dispatch point shared by the inline action menu and the
    quick-action keys, so both reuse the real command's behaviour and the
    target repo's own config. ``verb`` may be multi-token (``"net loose"``,
    ``"pr --open"``, ``"job log --follow"``).

    Verbs in :data:`ATTACH_VERBS` gain ``--force``; ``--force`` means
    something different on every other command (and most don't accept it),
    so nothing else gets it.

    The verb's :func:`dispatch_style` decides what happens to its output: a
    pager for the diff (with ``--color`` forced, because the pipe would
    otherwise turn colour off), a keypress pause for the other printing verbs,
    and nothing at all for the rest. A missing or unstartable pager degrades to
    the pause rather than losing the output.
    """
    argv = ["jailbee", *verb.split(), name, "--config", str(config_path)]
    if verb in ATTACH_VERBS:
        argv.append("--force")
    style = dispatch_style(verb)
    if style == "paged":
        pager = pager_argv()
        if pager is not None:
            try:
                return _run_paged([*argv, "--color"], pager)
            except OSError as exc:
                log.debug("pager %s failed: %s", pager, exc)
    rc = subprocess.run(argv, check=False).returncode
    if style != "plain":
        _wait_for_return()
    return rc


def _refresh_due(
    *,
    now: float,
    last_base: float,
    last_full: float,
    interval: float,
    git_interval: float,
    git_enabled: bool,
    first: bool,
    forced: bool,
) -> tuple[bool, bool]:
    """Decide whether to gather now and whether to include git status.

    Returns ``(do_base, do_git)``. ``do_base`` is whether to gather at all;
    ``do_git`` is whether this gather should include the (expensive) git tier.
    ``now`` is a monotonic timestamp. ``first``/``forced`` force an immediate
    git-inclusive gather.
    """
    do_git = git_enabled and (first or forced or now >= last_full + git_interval)
    do_base = first or forced or do_git or now >= last_base + interval
    return do_base, do_git


def run(
    incus: Incus,
    cwd_config: Path | None,
    *,
    interval: float,
    git_interval: float,
    no_git: bool,
) -> int:
    """Main dashboard loop.

    A daemon thread gathers container state on the two-tier schedule and
    publishes it under a lock; this (main) thread only renders the latest
    snapshot and handles input on a fast timer, so keystrokes stay responsive
    even while a gather (which blocks on incus/git) is in flight.
    """
    from rich.live import Live

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        error("jailbee dashboard requires an interactive terminal.")
        return 1

    # Launch-time guard only; `gather_live` re-resolves the list per gather.
    if not collect_config_paths(cwd_config):
        error("No repos registered and no .jailbee/config.yaml in the current directory.")
        return 1

    from jailbee.db import get_engine
    from jailbee.db.view_prefs import FRONTEND_TUI

    # Resolved once for the whole run — a live-refreshing dashboard must not
    # re-merge config on every frame.
    engine = get_engine()
    view_state = seed_view_state(engine, FRONTEND_TUI)
    enabled: tuple[str, ...] | None = view_state.columns
    folded: frozenset[str] = view_state.folded

    interval = max(0.5, interval)
    git_interval = max(git_interval, interval)
    git_enabled = not no_git

    def now() -> datetime:
        return datetime.now().astimezone()

    lock = threading.Lock()
    stop = threading.Event()
    force = threading.Event()
    shared_groups: list[RepoGroup] = []
    shared_last_full = 0.0
    worker_error: list[BaseException] = []

    def refresher() -> None:
        nonlocal shared_groups, shared_last_full
        last_base = 0.0
        last_full = 0.0
        first = True
        prev_groups: list[RepoGroup] = []
        while not stop.is_set():
            forced = force.is_set()
            do_base, do_git = _refresh_due(
                now=time.monotonic(),
                last_base=last_base,
                last_full=last_full,
                interval=interval,
                git_interval=git_interval,
                git_enabled=git_enabled,
                first=first,
                forced=forced,
            )
            if do_base:
                if forced:
                    force.clear()
                try:
                    groups = gather_live(incus, cwd_config, with_git=do_git)
                except Exception as exc:  # surface any gather failure to the main thread
                    worker_error.append(exc)
                    stop.set()
                    break
                if not do_git:
                    # A base gather has no git status; fill it in from the
                    # last git-tier snapshot so the columns don't flicker
                    # blank until the next git-tier refresh lands.
                    carry_forward_git_status(groups, prev_groups)
                ts = time.monotonic()
                with lock:
                    shared_groups = groups
                    shared_last_full = ts
                prev_groups = groups
                last_base = ts
                if do_git:
                    last_full = ts
                first = False
            # sleep until the next tick, waking early on a forced refresh or stop
            force.wait(timeout=0.1)

    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    selected: Row | None = None
    sel_index = 0
    overlay: Overlay | None = None
    notice: str | None = None
    notice_until = 0.0

    def set_notice(text: str) -> None:
        """Show ``text`` in the panel subtitle for a few seconds.

        The dashboard owns the whole screen while Live is running, so a
        rejected key or a view-only row has nowhere to print — but staying
        silent is indistinguishable from being broken, hence this.
        """
        nonlocal notice, notice_until
        notice = text
        notice_until = time.monotonic() + _NOTICE_SECONDS

    def persist_view_state(state: ViewState) -> None:
        """Write ``state`` to ``view_prefs``, degrading instead of crashing.

        Three call sites in this loop commit to SQLite straight from a
        keypress (the fold key, Enter on a header, the overlay toggle).
        ``run()``'s own ``try`` only catches ``KeyboardInterrupt``, so a
        write failure here (``database is locked`` against a concurrent
        background worker, a read-only state dir) would otherwise end the
        whole session with a traceback. The fold/toggle already took effect
        on screen by the time this runs — only persistence is lost.
        """
        try:
            save_view_state(engine, FRONTEND_TUI, state)
        except Exception:
            log.debug("failed to save dashboard view state", exc_info=True)
            set_notice("could not save view settings")

    def open_settings_overlay() -> SettingsState:
        """A fresh settings overlay over the current ``groups``/``folded``.

        A small closure rather than inlining twice: opening from the plain
        table and switching in from another overlay (see the `F2` handling
        below) both need it.
        """
        return open_settings(
            field_names=all_column_names(),
            enabled=frozenset(enabled if enabled is not None else default_columns()),
            repo_prefixes=settings_repo_prefixes(groups, folded),
            folded=folded,
        )

    worker = threading.Thread(target=refresher, name="jailbee-dashboard-refresh", daemon=True)
    last_title: str | None = None
    try:
        tty.setcbreak(fd)
        worker.start()
        # Pushed before Live takes the screen and popped after it gives it
        # back, so the terminal's own title is saved and restored intact.
        with (
            terminal_title_scope(sys.stdout),
            Live(console=console, screen=True, auto_refresh=False) as live,
        ):

            def foreground(fn: Callable[[], int]) -> int:
                """Hand the terminal to a real ``jailbee`` command, then take it back.

                Interactive verbs (``tmux``, ``shell``) need the raw terminal
                and the normal screen, so Live is stopped for the duration —
                but only for the *dispatch*. Opening the menu no longer
                touches the terminal at all, which is what keeps the
                dashboard on screen behind it.
                """
                live.stop()
                termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
                try:
                    return fn()
                finally:
                    tty.setcbreak(fd)
                    live.start(refresh=True)

            def dispatch(target: str, verb: str) -> None:
                nonlocal notice, notice_until
                group = _find_group(groups, target)
                if group is None or group.config_path is None:
                    return
                config_path = group.config_path
                rc = foreground(lambda: _dispatch_action(config_path, verb, target))
                if rc != 0:
                    set_notice(f"'jailbee {verb} {target}' exited {rc}")
                force.set()  # an action likely changed state — refresh ASAP

            def create_container() -> None:
                """Ask for a branch and a base, then run `jailbee new` here.

                The terminal is handed over rather than the command dispatched
                detached, because `jailbee new` asks its own questions:
                confirming reuse of an existing branch, and the branch-autostart
                escalation gate. `--background` does not avoid that — the
                escalation question is asked by the foreground parent before it
                detaches (`lifecycle._autostart_approved`). The only other
                option is `--yes`, i.e. accepting a network-widening branch
                config unseen.
                """
                note = new_container_reject_note(groups, selected)
                if note is not None:
                    set_notice(note)
                    return
                group = new_container_target(groups, selected)
                assert group is not None  # guaranteed by the note being None
                config_path = group.config_path
                assert config_path is not None  # ditto
                base_default = new_container_base_default(group.repo_root)

                def ask_and_run() -> int:
                    import typer

                    try:
                        branch = typer.prompt("New branch").strip()
                        base = typer.prompt("Base branch", default=base_default or "").strip()
                    except (typer.Abort, EOFError, KeyboardInterrupt):
                        # Ctrl-C answers the prompt, not the dashboard: `run`'s
                        # own KeyboardInterrupt handler would quit outright.
                        return 0
                    if not branch or not base:
                        return 0
                    rc = subprocess.run(
                        new_container_argv(config_path, branch, base), check=False
                    ).returncode
                    _wait_for_return()
                    return rc

                rc = foreground(ask_and_run)
                if rc != 0:
                    set_notice(f"'jailbee new' exited {rc}")
                force.set()  # the new container should appear on the next frame

            while not stop.is_set():
                with lock:
                    groups = shared_groups
                    last_full = shared_last_full
                rows = selectable_rows(groups, folded)
                if (
                    isinstance(overlay, MenuState)
                    and Row("container", overlay.container) not in rows
                ):
                    # The menu's container vanished under it (destroyed, or its
                    # repo dropped out of the registry) — close rather than
                    # dispatch at a name that is no longer there.
                    set_notice(f"'{overlay.container}' is gone — menu closed")
                    overlay = None
                if isinstance(overlay, MenuState):
                    selected = Row("container", overlay.container)  # pinned while the menu is open
                else:
                    selected = reconcile_selection(rows, selected, sel_index)
                if selected in rows:
                    sel_index = rows.index(selected)
                if notice is not None and time.monotonic() >= notice_until:
                    notice = None
                # Only on change: an OSC 2 write on every frame makes some
                # terminals redraw their title bar continuously.
                title = terminal_title(groups, selected)
                if title != last_title:
                    set_terminal_title(title, stream=sys.stdout)
                    last_title = title
                age = (time.monotonic() - last_full) if last_full else 0.0
                live.update(
                    render(
                        groups,
                        selected,
                        now=now(),
                        last_refresh_age=age,
                        interval=interval,
                        git_enabled=git_enabled,
                        enabled=enabled,
                        overlay=overlay,
                        notice=notice,
                        folded=folded,
                    ),
                    refresh=True,
                )
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not ready:
                    continue
                key = parse_key(os.read(fd, _KEY_READ_BYTES))
                if key == "interrupt":
                    break
                if overlay is not None:
                    if key in ("cancel", "quit"):
                        overlay = None
                    elif key == "help":
                        # One slot, so help replaces the menu rather than
                        # stacking on it — and toggles itself shut.
                        overlay = None if overlay == "help" else "help"
                    elif key == "settings":
                        # Mirrors help's own toggle, one line up: F2/S
                        # switches to settings from any other overlay (the
                        # action menu, help) instead of just closing it, and
                        # toggles itself shut when settings is already open.
                        if isinstance(overlay, SettingsState):
                            overlay = None
                        else:
                            overlay = open_settings_overlay()
                    elif isinstance(overlay, SettingsState):
                        if key in ("up", "down"):
                            overlay = move_settings(overlay, -1 if key == "up" else 1)
                        elif key == "tab":
                            overlay = switch_tab(overlay)
                        elif key == "fold":
                            overlay = toggle_current(overlay)
                            enabled = enabled_names(overlay)
                            folded = overlay.folded
                            persist_view_state(ViewState(enabled, folded))
                    elif isinstance(overlay, MenuState):
                        if key in ("up", "down"):
                            overlay = move_menu(overlay, -1 if key == "up" else 1)
                        elif key == "enter":
                            verb = menu_verb(overlay)
                            target = overlay.container
                            overlay = None
                            if verb is not None:
                                dispatch(target, verb)
                    continue
                if key == "quit":
                    break
                if key in ("up", "down"):
                    selected = move_selection(rows, selected, -1 if key == "up" else 1)
                    if selected in rows:
                        sel_index = rows.index(selected)
                elif key == "enter":
                    if selected is not None and selected.kind == "repo":
                        folded = toggle_folded(folded, selected.key)
                        persist_view_state(ViewState(enabled, folded))
                    else:
                        container = container_of(selected)
                        overlay = open_menu(groups, container)
                        if overlay is None and container is not None:
                            note = view_only_note(groups, container)
                            set_notice(note or f"No actions available for '{container}'")
                elif key == "fold":
                    prefix = fold_target(groups, selected)
                    if prefix is None:
                        set_notice("No repo group is selected")
                    else:
                        folded = toggle_folded(folded, prefix)
                        persist_view_state(ViewState(enabled, folded))
                elif key == "help":
                    overlay = "help"
                elif key == "settings":
                    overlay = open_settings_overlay()
                elif key.startswith("action:"):
                    container = container_of(selected)
                    verb = quick_verb(groups, container, key)
                    if verb is not None and container is not None:
                        dispatch(container, verb)
                    else:
                        set_notice(quick_reject_note(groups, container, key))
                elif key == "new":
                    create_container()
                elif key == "refresh":
                    force.set()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        force.set()  # wake the worker so it notices stop and exits promptly
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
    worker.join(timeout=2.0)
    if worker_error:
        error(f"dashboard refresh failed: {worker_error[0]}")
        return 1
    return 0
