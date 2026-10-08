"""The dashboards' shared data model: repo groups, targets, rows and selection.

Frontend-agnostic: the state service, the Qt window and the terminal
dashboard all build on it. Must not import ``jailbee.dashboard.tui``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from jailbee import agent_status
from jailbee.config import (
    format_loose_after,
    load_repo_config,
)
from jailbee.global_config import (
    GlobalConfig,
    default_global_config_path,
    load_global_config,
)
from jailbee.lifecycle import (
    ContainerInfo,
    agent_config_homes,
    agent_homes,
    annotate_activity,
    annotate_agent_status,
    list_containers,
)
from jailbee.paths import repo_config_path
from jailbee.remote_ssh.repo_scope import RemoteRepoScope

if TYPE_CHECKING:
    from jailbee.agent_activity import ActivityReader
    from jailbee.apps import AppSpec
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.procstat import ActivitySampler


log = logging.getLogger(__name__)

NOTHING_TO_SHOW = (
    "No repos registered, and no jailbee config could be loaded for the current "
    "directory. Run `jailbee config init` here, `jailbee config validate` if a "
    "config file already exists, or start the dashboard from a registered repo."
)
"""Launch-time guard message shared by the TUI, the Qt window and `cli`.

Deliberately does not name a cause: the current directory resolves to nothing
whether it has no `.jailbee/config.yaml` and `scratch.enabled` is false, or it
is `$HOME`/the filesystem root (which scratch refuses), or its name slugifies
to nothing, or its config file exists but does not parse.

The remedy is cause-neutral for the same reason: both branches out of it work
whichever of those fired, and naming no cause must not also mean leaving the
user with no next step — which is what the earlier, single-cause wording
carried by implication and this one has to say outright.
"""

# How long a dashboard waits for its first snapshot — a cold service's first
# gather included — before giving up on the user's own terminal.
STARTUP_TIMEOUT_SECONDS = 30.0


class AppMenuEntry(NamedTuple):
    """One registry app as the action menu needs it: a dispatch verb plus
    the text to show for it.

    ``verb`` is what :func:`_dispatch_action` splits and inserts the
    container name after — see :func:`_app_menu_verb` for why it is the bare
    `AppSpec.name` for a builtin (``ide``, ``chrome``, ``firefox``: each a
    real top-level ``jailbee`` command taking the container as a plain
    positional) but ``"apps run <name> --container"`` for a config-sourced
    `apps:` entry. ``label`` is `AppSpec.description` when the repo's config
    set one (JetBrains sets ``"JetBrains idea"``, a browser sets
    ``"Chrome"``); it falls back to the bare app name for a user's ``apps:``
    entry that left ``description`` empty, so the menu never renders a blank
    label.
    """

    verb: str
    label: str


def _app_menu_verb(spec: AppSpec) -> str:
    """The :func:`_dispatch_action` verb for one registry app.

    A builtin (``chrome``, ``firefox``, ``ide``) is a real top-level
    ``jailbee`` command that takes the container as a plain positional, so
    its bare name dispatches correctly, keeps ``action:ide``/``action:chrome``
    quick keys matching (:data:`KEY_BINDINGS`), and keeps `--force` working
    through the unmodified :data:`ATTACH_VERBS` check.

    A config-sourced ``apps:`` entry has no such command — dispatching its
    bare name only works at all when the entry set ``top_level: true``
    (`entry._top_level_app_names`), and even then `entry.rewrite_app_argv`
    turns ``jailbee <app> <name>`` into ``apps run <app> <name>``, where
    Ruling 24 made the container an *option* (``--container``), not a second
    positional — ``<name>`` would land in the app's own variadic ``args``
    instead of naming the container, silently launching in the default one.
    Routing explicitly through ``apps run <name> --container`` here — rather
    than through the top-level command and its rewrite — dispatches
    correctly whether or not the entry declared ``top_level``, and works for
    one that did not (which the bare-name form cannot reach at all: Typer has
    no such command to rewrite).
    """
    if spec.source == "builtin":
        return spec.name
    return f"apps run {spec.name} --container"


@dataclass
class RepoGroup:
    """One repo's containers. ``repo_root`` is None for orphan groups
    (jailbee-managed containers whose repo config could not be loaded) and is
    what distinguishes them. ``config_path`` is None for those *and* for a repo
    that has no ``.jailbee/config.yaml`` of its own, whose config is
    synthesized — so it gates nothing on its own: it says only whether a child
    is addressed with ``--config`` or by its cwd (see :class:`RepoTarget`).
    ``apps`` mirrors the repo's GUI app registry (`apps.resolve_apps`,
    builtins and `apps:` entries alike) in registry order, and drives the
    corresponding action-menu entries; orphan groups keep it empty. It
    replaces what used to be two separate booleans (``ide_enabled``,
    ``chrome_enabled``) — the registry can hold any number of apps, not just
    those two, and each carries its own display label (see
    :class:`AppMenuEntry`).
    ``loose_ttl_default`` is the repo's effective ``loose_auto_revert.after``
    as prompt-ready text — what the GUI's duration dialog pre-selects — or
    None when auto-revert is disabled, which tells the GUI not to ask at all
    (there is no TTL to schedule). Orphan groups keep None.
    ``push_action_default``/``push_source_default`` mirror the repo's effective
    ``push.default_action``/``default_source``, so a front-end can tell whether
    `jailbee git push` would stop to ask a question its own child process
    cannot answer. Orphan groups keep ``PushConfig``'s defaults.
    ``agent_homes`` are ``(container, agent, session home)`` for this group's
    containers and the repo's pooled agents (``lifecycle.agent_homes``);
    `sample_activity` matches each container against its own. Orphan groups
    keep ``()``.
    ``agent_config_homes`` are the matching shared config homes
    (``lifecycle.agent_config_homes``), where an agent's transcripts live;
    orphan groups keep ``()``.
    ``optional_mounts`` lists the repo config's `optional_mounts:` kinds, which
    the terminal menu's Mount…/Unmount… pickers choose from. Orphan groups keep
    it empty."""

    prefix: str
    repo_root: str | None
    config_path: Path | None
    containers: list[ContainerInfo]
    apps: list[AppMenuEntry] = field(default_factory=list)
    loose_ttl_default: str | None = None
    push_action_default: str = "ask"
    push_source_default: str = "base"
    column_notice: str | None = None
    agent_homes: tuple[tuple[str, str, Path], ...] = ()
    agent_config_homes: tuple[tuple[str, str, Path], ...] = ()
    optional_mounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepoTarget:
    """How a spawned `jailbee` child is pointed at one repo.

    A configured repo is addressed with ``--config <path>``, which works from
    any cwd and is what both front-ends have always done. A repo with no config
    file has no path to point at, so it is addressed by running the child in
    the repo root and letting the ordinary cwd resolution synthesize the config
    (`config.load_repo_config`). Setting the cwd for both cases keeps one code
    path; only the flag differs, and an explicit ``--config`` wins over cwd
    resolution anyway, so a configured repo's behaviour is unchanged.
    """

    repo_root: Path
    config_path: Path | None

    @classmethod
    def of(cls, group: RepoGroup) -> RepoTarget | None:
        """The target for ``group``, or None for an orphan group (no repo root).

        This is the actionability test both front-ends gate on. It is *not*
        ``config_path is not None``: that would refuse a repo whose config was
        synthesized rather than read, which is a real repo with real containers
        and a root to run in.
        """
        if group.repo_root is None:
            return None
        return cls(Path(group.repo_root), group.config_path)

    def flags(self) -> list[str]:
        """The ``--config`` argv fragment, empty when there is no file."""
        return ["--config", str(self.config_path)] if self.config_path is not None else []

    def cwd(self) -> Path:
        """The directory to run the child in — always the repo root."""
        return self.repo_root


def registered_repo_roots(*, scope: RemoteRepoScope | None = None) -> list[Path]:
    """Repo roots for all ``RegisteredRepo`` rows whose directory still exists.

    This used to return config-file paths, which silently dropped a repo that
    has no config file — the scratch case, which is a real repo with real
    containers. A row whose directory is gone is skipped; the dashboard never
    prunes the registry (``refresh_all`` does).
    """
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    out: list[Path] = []
    with Session(get_engine()) as session:
        for repo in session.exec(select(RegisteredRepo)).all():
            if scope is not None and not scope.allows(repo.container_prefix):
                continue
            root = Path(repo.repo_root)
            if root.is_dir():
                out.append(root)
    return out


def _dedupe_roots(candidates: Iterable[Path]) -> list[Path]:
    """Dedupe by resolved path (symlinks, relative forms), keeping the caller's
    original Path objects and their order."""
    seen: set[Path] = set()
    ordered: list[Path] = []
    for p in candidates:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        ordered.append(p)
    return ordered


def collect_repo_roots(
    cwd_root: Path | None, *, scope: RemoteRepoScope | None = None
) -> list[Path]:
    """Registered repo roots plus the cwd's, deduped, cwd first."""
    registered = registered_repo_roots() if scope is None else registered_repo_roots(scope=scope)
    return _dedupe_roots(([cwd_root] if cwd_root is not None else []) + registered)


def _loose_ttl_default(cfg: Config, gcfg: GlobalConfig) -> str | None:
    """The repo's effective loose TTL as prompt text, None when disabled."""
    policy = cfg.effective_loose_auto_revert(gcfg)
    return format_loose_after(policy.after) if policy is not None else None


def global_config_or_defaults() -> GlobalConfig:
    """Load the global config, falling back to defaults on any error.

    The dashboard is a read-only viewer refreshed on a timer; an unreadable
    or invalid ``global.yaml`` must degrade to defaults rather than abort the
    gather (the CLI, which can report and exit, is stricter). A typo'd
    column name is no longer one of those errors — `load_global_config`
    recovers from it and hands back the sanitized config (valid names
    honoured, invalid ones dropped) rather than the whole block being lost;
    this only still degrades to `GlobalConfig()` on a genuine host-level
    schema problem. The dropped names are logged rather than surfaced in
    the UI on every refresh tick; retired-column warnings are instead shown
    once at startup or on the grouped repo's status line.
    """
    try:
        gcfg, dropped = load_global_config(default_global_config_path())
    except Exception:  # ConfigError, OSError — any of them means "use defaults"
        return GlobalConfig()
    if dropped:
        log.debug("global config: %s", "; ".join(dropped))
    return gcfg


def dashboard_config_migration_notice() -> str | None:
    """Load-time migration warning for the retired global dashboard column."""
    try:
        _, warnings = load_global_config(default_global_config_path())
    except Exception:
        warnings = []  # the ordinary loader reports unrelated config failures
    retired = [warning for warning in warnings if "ahead_diff" in warning]
    return "; ".join(retired) if retired else None


def dashboard_group_notices(groups: Sequence[RepoGroup]) -> list[str]:
    """Config migration notices from the already gathered repo configurations."""
    return [f"{group.prefix}: {group.column_notice}" for group in groups if group.column_notice]


def gather_rows(
    incus: Incus,
    repo_roots: list[Path],
    *,
    with_git: bool,
) -> list[RepoGroup]:
    """Build per-repo groups, then append orphan groups.

    Unscoped and unpinned: named repos first, alphabetically, orphans last.
    `present` applies a client's scope and cwd pin.

    Each root's own config drives accurate git-status/base/background-jobs.
    Repos are identified by their root, not by their config file, so a repo
    with no ``.jailbee/config.yaml`` — whose config ``load_repo_config``
    synthesizes — groups its own containers instead of falling through to the
    orphan bucket. An unloadable config is skipped (read-only — the registry
    is never pruned here). A final ``all_repos=True`` scan surfaces
    jailbee-managed containers whose repo we could not load, as view-only
    orphan groups.
    """
    from jailbee.apps import resolve_apps

    groups: list[RepoGroup] = []
    covered: set[str] = set()
    base_cfg = None
    gcfg = global_config_or_defaults()
    # One `incus list` per gather, shared by every repo and the orphan scan:
    # each listing makes the daemon build every instance's full state, and
    # the dashboards gather every few seconds. Fetched on first use, so a
    # gather with no loadable repo still never calls Incus.
    instances: list[dict[str, Any]] | None = None
    for root in repo_roots:
        try:
            cfg = load_repo_config(root)
        except Exception:  # OSError, YAML parse, Pydantic validation, no scratch
            continue
        if base_cfg is None:
            base_cfg = cfg
        if instances is None:
            instances = incus.list_containers()
        containers = list_containers(
            cfg,
            incus,
            all_repos=False,
            with_git_status=with_git,
            with_background=True,
            instances=instances,
        )
        covered.add(cfg.container_prefix)
        groups.append(
            RepoGroup(
                cfg.container_prefix,
                str(cfg.repo_root),
                repo_config_path(root),
                containers,
                apps=[
                    AppMenuEntry(_app_menu_verb(spec), spec.description or spec.name)
                    for spec in resolve_apps(cfg)
                ],
                loose_ttl_default=_loose_ttl_default(cfg, gcfg),
                push_action_default=cfg.push.default_action,
                push_source_default=cfg.push.default_source,
                column_notice="; ".join(
                    warning for warning in cfg.column_warnings() if "ahead_diff" in warning
                )
                or None,
                agent_homes=agent_homes(cfg, [c.name for c in containers]),
                agent_config_homes=agent_config_homes(cfg, [c.name for c in containers]),
                optional_mounts=tuple(cfg.optional_mounts),
            )
        )

    if base_cfg is not None:
        all_rows = list_containers(
            base_cfg,
            incus,
            all_repos=True,
            with_git_status=False,
            with_background=False,
            instances=instances,
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

    def _sort_key(g: RepoGroup) -> tuple[bool, str]:
        # Orphan groups are the ones with no repo root — `config_path` is no
        # longer the discriminator, since a scratch repo has a root but no file.
        return (g.repo_root is None, g.prefix)

    groups.sort(key=_sort_key)
    return groups


def gather_live(incus: Incus, extra_roots: Sequence[Path], *, with_git: bool) -> list[RepoGroup]:
    """One snapshot for the state service: repo roots re-resolved per gather.

    Both dashboards refresh on a timer, and the set of registered repos moves
    underneath them: `jailbee new` registers a repo the first time it is used
    (`cli.py`), and `egress_pool.refresh_all` unregisters — then a later
    command re-registers — a repo whose config file momentarily disappeared.
    A root list captured at launch therefore goes stale, and a repo missing
    from it is not merely absent: `gather_rows`'s ``all_repos`` scan still
    finds its containers and files them under a view-only orphan group, where
    ``actions_for_container`` yields no actions and the right-click menu never
    opens. Re-resolving here is what keeps that self-healing instead of
    requiring a dashboard restart.

    The registry read is a single indexed SQLite select against a WAL
    database — cheap next to the `incus list` (and git probes) in the gather
    it precedes.

    ``extra_roots`` are the connected dashboards' cwd repos, which may not be
    registered. The result is unscoped and unpinned; each dashboard applies
    its own `present`.
    """
    return gather_rows(
        incus, _dedupe_roots([*extra_roots, *registered_repo_roots()]), with_git=with_git
    )


def present(
    groups: Sequence[RepoGroup],
    cwd_root: Path | None,
    scope: RemoteRepoScope | None = None,
) -> list[RepoGroup]:
    """A snapshot as one dashboard shows it: its scope applied, its cwd repo first.

    The state service gathers for every dashboard at once, so neither can be
    baked into the snapshot. Filtering by prefix matches what `gather_rows`
    used to do with a scope: excluded registered repos and orphan groups are
    both keyed by their container prefix.
    """
    shown = [g for g in groups if scope is None or scope.allows(g.prefix)]
    if cwd_root is not None:
        shown.sort(key=lambda g: g.repo_root != str(cwd_root))
    return shown


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


def sample_activity(
    groups: list[RepoGroup],
    sampler: ActivitySampler,
    reader: ActivityReader | None = None,
) -> None:
    """Fill every container's CPU/DOING/AGENT fields from one sampler reading.

    One reading per screen, not one per repo group: the sampler stamps the
    elapsed time itself, so splitting a frame across several calls would
    measure several different windows.

    AGENT is then read per group from its containers' own session homes,
    from the same reading. One group's failure clears that group only.
    With a `reader`, each group's AGENT summaries also carry what the
    agent is doing, read from the repo's shared config home; the reader
    forgets sessions that were not live this tick.

    Shared with the Qt worker, which owns its own sampler.
    """
    annotate_activity([c for g in groups for c in g.containers], sampler)
    if reader is not None:
        reader.begin()
    try:
        for g in groups:
            lookup = (
                None
                if reader is None
                else reader.lookup_for(
                    {(name, agent): home for name, agent, home in g.agent_config_homes},
                    sampler.processes,
                )
            )
            try:
                annotate_agent_status(
                    g.containers,
                    agent_status.read_sessions(g.agent_homes),
                    sampler,
                    activity=lookup,
                )
            except Exception:  # one group's reading must not end the tick for the rest
                log.debug("failed to read agent state for %s", g.prefix, exc_info=True)
                for c in g.containers:
                    c.agent_status = ()
    finally:
        if reader is not None:
            reader.finish()


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

    Every group contributes its header, folded or not; a folded group
    contributes none of its containers.
    """
    rows: list[Row] = []
    for g in groups:
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

    Accepts either kind of row so callers can resolve a group's prefix from
    its header or one of its container rows. The live-table fold action is
    currently triggered from a repo header; settings provide the other route.
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


def _find_group(groups: list[RepoGroup], name: str | None) -> RepoGroup | None:
    for g in groups:
        for c in g.containers:
            if c.name == name:
                return g
    return None


def prompt_target_kind(purpose: str) -> Literal["repo", "container"]:
    """What a prompt's or picker's ``target`` names, read from its ``purpose``.

    Only the ``container-*`` questions are about a container; every other one
    targets a repo prefix. A container may share its name with another repo's
    prefix (container ``alpha-x`` of repo ``alpha`` beside repo ``alpha-x``),
    so a target is never looked up as both.
    """
    return "container" if purpose.startswith("container-") else "repo"


def target_group(
    groups: list[RepoGroup], target: str, kind: Literal["repo", "container"]
) -> RepoGroup | None:
    """The listed repo that ``target`` — a prefix or a container name — belongs to."""
    if kind == "container":
        return _find_group(groups, target)
    return next((g for g in groups if g.prefix == target), None)
