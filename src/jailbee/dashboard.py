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
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich import box
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table

from jailbee import table_format
from jailbee.config import (
    DASHBOARD_DEFAULT_HIDE,
    ColumnConfig,
    format_loose_after,
    load_config,
)
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
from jailbee.tui import console, error, warn

if TYPE_CHECKING:
    from jailbee.config import Config
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
    (there is no TTL to schedule). Orphan groups keep None."""

    prefix: str
    repo_root: str | None
    config_path: Path | None
    containers: list[ContainerInfo]
    ide_enabled: bool = False
    chrome_enabled: bool = False
    loose_ttl_default: str | None = None


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


def resolve_dashboard_columns(cwd_config: Path | None) -> ColumnConfig:
    """The dashboard's effective column preference, resolved once per launch.

    Both dashboards render one shared table across every registered repo
    (`render`, `qtui/window.py`'s `set_groups`), so a per-repo-group answer
    is impossible — this resolves against the directory the user is
    standing in (``cwd_config``), falling back to the global ``dashboard:``
    block when there is no cwd repo or its config fails to load. Callers
    (the TUI's ``run`` loop, the Qt ``AppController``) call this once at
    startup and reuse the result on every refresh — never per frame.
    """
    gcfg = _global_config_or_defaults()
    if cwd_config is None:
        return gcfg.dashboard
    try:
        cfg = load_config(cwd_config)
    except Exception:  # same broad catch as _global_config_or_defaults: degrade, don't abort
        return gcfg.dashboard
    return cfg.effective_dashboard_columns(gcfg)


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


def selectable_names(groups: list[RepoGroup]) -> list[str]:
    """Flat list of container names in display order (headers excluded)."""
    return [c.name for g in groups for c in g.containers]


def move_selection(names: list[str], current: str | None, delta: int) -> str | None:
    """Move the highlight by ``delta`` rows, clamped at both ends."""
    if not names:
        return None
    if current not in names:
        return names[0]
    idx = names.index(current)
    return names[max(0, min(len(names) - 1, idx + delta))]


def reconcile_selection(names: list[str], current: str | None, last_index: int) -> str | None:
    """Keep ``current`` if still present; else clamp ``last_index`` into the
    refreshed list (nearest remaining row). None when the list is empty."""
    if not names:
        return None
    if current in names:
        return current
    return names[min(last_index, len(names) - 1)]


_NETWORK_MODES: tuple[str, ...] = ("strict", "loose")


def menu_actions(
    state: str,
    *,
    has_config: bool,
    ide_enabled: bool = False,
    chrome_enabled: bool = False,
    current_network: str | None = None,
    pr_number: int | None = None,
    job_clearable: bool = False,
) -> list[tuple[str, str]]:
    """(label, jailbee-subcommand) options for the highlighted container.

    Empty for orphan rows (no loadable config ⇒ no safe dispatch). "Launch
    IDE"/"Launch Chrome" only appear when the repo's own config enables
    `jetbrains`/`chrome` respectively (``ide_enabled``/``chrome_enabled``,
    sourced from ``RepoGroup.ide_enabled``/``chrome_enabled``) — dispatching
    `jailbee ide`/`jailbee chrome` when the feature is disabled would just fail.

    For running containers, one "Network: <mode>" entry appears per mode
    other than ``current_network`` (sourced from ``ContainerInfo.network``),
    dispatching the two-token ``jailbee net <mode>`` subcommand.

    "Clear failed job" (verb "job clear") heads the list when
    ``job_clearable`` — it is the corrective action, and it belongs far from
    "Destroy" at the bottom. "Open PR" (verb "pr --open") follows it when
    pr_number is not None.
    """
    if not has_config:
        return []
    prefix: list[tuple[str, str]] = []
    if job_clearable:
        prefix.append(("Clear failed job", "job clear"))
    if pr_number is not None:
        prefix.append(("Open PR", "pr --open"))
    if state == "Running":
        actions = [
            ("Attach tmux", "tmux"),
            ("Open shell", "shell"),
        ]
        if ide_enabled:
            actions.append(("Launch IDE", "ide"))
        if chrome_enabled:
            actions.append(("Launch Chrome", "chrome"))
        for mode in _NETWORK_MODES:
            if mode != current_network:
                actions.append((f"Network: {mode}", f"net {mode}"))
        actions += [
            ("Restart", "restart"),
            ("Stop", "stop"),
            ("Destroy", "destroy"),
        ]
        return prefix + actions
    if state == "Stopped":
        return [*prefix, ("Start", "start"), ("Destroy", "destroy")]
    return [*prefix, ("Destroy", "destroy")]


def visible_fields(
    now: datetime,
    all_containers: list[ContainerInfo],
    columns: ColumnConfig | None = None,
) -> list[FieldSpecCI]:
    """The dashboard's visible columns, honouring each field's ``show_if``.

    ``columns`` is the repo's effective ``dashboard:`` block. ``None`` means
    the built-in default — today's hidden set — so callers that have no
    config in hand still render what they always did.

    An explicit ``columns.fields`` list already has ``show_if`` cleared by
    :func:`table_format.apply_column_config` — naming a column is a request
    for that exact column, so it renders even when no container would
    otherwise justify it. That's the same rule ``jailbee ls``'s configured
    ``fields:`` list gets (see ``cli.py``'s ``ls`` command), applied once in
    the resolver rather than copied here: this function's ``if f.show_if is
    None or ...`` below is a no-op for those fields, and still prunes the
    built-in default set (with or without ``hide``) exactly as before.

    The ``network`` field is swapped for a dashboard-specific one whose cell
    folds the loose TTL inline (e.g. ``"loose (12m)"``); that is why the
    standalone TTL column is hidden by default.

    Shared by the TUI ``render`` and the Qt model so both show the same set.
    """

    def _network_cell(c: ContainerInfo) -> str:
        if c.network != "loose":
            return c.network or "-"
        if c.loose_until is None:
            return f"{c.network} (—)"
        return f"{c.network} ({format_duration_short(c.loose_until - now)})"

    if columns is None:
        columns = ColumnConfig(hide=list(DASHBOARD_DEFAULT_HIDE))
    candidates = table_format.apply_column_config(
        ls_field_specs(now=now, all_repos=False),
        fields=columns.fields,
        hide=columns.hide,
    )
    fields = [
        f
        for f in candidates
        if table_format.shows_by_default_in_dashboard(f)
        and (f.show_if is None or f.show_if(all_containers))
    ]
    return [replace(f, cell=_network_cell) if f.name == "network" else f for f in fields]


_FOOTER = (
    "[bold]↑/↓[/bold] (j/k) move  ·  [bold]Enter[/bold] actions  ·  "
    "[bold]r[/bold] refresh  ·  [bold]q[/bold] quit"
)

_KEY_READ_BYTES = 8  # covers all standard arrow/function-key CSI sequences


def render(
    groups: list[RepoGroup],
    selected: str | None,
    *,
    now: datetime,
    last_refresh_age: float,
    interval: float,
    git_enabled: bool,
    refreshing: bool = False,
    columns: ColumnConfig | None = None,
) -> RenderableType:
    """Build the Rich renderable for one dashboard frame.

    One shared table (columns aligned across all repos); each repo is a
    section header row inside it. The selected container is marked with a
    ``▸`` gutter arrow and bold styling. Wrapped in a rounded Panel with a
    title (summary + refresh indicator) and a footer (keybindings).
    """
    all_containers = [c for g in groups for c in g.containers]
    # `mem` (used / limit) is a default-table field and `memory_limit` is not,
    # so the plain default-table filter already yields the MEM column.
    fields = visible_fields(now, all_containers, columns)

    table = Table(box=None, pad_edge=False, expand=False, show_edge=False)
    for f in fields:
        # The NAME column carries a 2-char arrow gutter on data rows; pad its
        # header so the column lines up.
        header = ("  " + f.header) if f.name == "name" else f.header
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
            is_orphan = g.repo_root is None
            label = f"{g.prefix}  (orphan)" if is_orphan else g.prefix
            label_style = "bold yellow" if is_orphan else "bold cyan"
            table.add_row(f"[{label_style}]{label}[/]", *([""] * (len(fields) - 1)))
            for c in g.containers:
                is_sel = c.name == selected
                cells: list[str] = []
                for f in fields:
                    if f.name == "name":
                        gutter = "[bold cyan]▸[/] " if is_sel else "  "
                        name_val = c.name if is_orphan else f.cell(c)
                        cells.append(gutter + name_val)
                    else:
                        cells.append(f.cell(c))
                table.add_row(*cells, style="bold bright_white" if is_sel else None)

    n_repos = len({g.prefix for g in groups})
    n_ctr = len(all_containers)
    mark = "  [yellow]⟳[/]" if refreshing else ""
    title = (
        f"[bold]jailbee dashboard[/]   {n_repos} repos · {n_ctr} containers   {now:%H:%M:%S}{mark}"
    )
    git_note = "" if git_enabled else "  ·  [dim](no-git)[/dim]"
    subtitle = (
        f"{_FOOTER}  ·  refreshed {last_refresh_age:.0f}s ago · every {interval:.0f}s{git_note}"
    )
    return Panel(table, title=title, subtitle=subtitle, box=box.ROUNDED, padding=(0, 1))


def parse_key(data: bytes) -> str:
    """Map a raw stdin read to a dashboard key token ('' if unmapped)."""
    if data in (b"\x1b[A", b"k"):
        return "up"
    if data in (b"\x1b[B", b"j"):
        return "down"
    if data in (b"\r", b"\n"):
        return "enter"
    if data == b"r":
        return "refresh"
    if data in (b"q", b"\x03"):
        return "quit"
    return "quit" if data == b"" else ""  # EOF -> quit


def _find_group(groups: list[RepoGroup], name: str | None) -> RepoGroup | None:
    for g in groups:
        for c in g.containers:
            if c.name == name:
                return g
    return None


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
        container.state,
        has_config=group.config_path is not None,
        ide_enabled=group.ide_enabled,
        chrome_enabled=group.chrome_enabled,
        current_network=container.network,
        pr_number=container.pr_number,
        job_clearable=job_clearable,
    )


def _open_action_menu(groups: list[RepoGroup], selected: str | None) -> None:
    """Show the questionary action menu and dispatch the chosen ``jailbee`` command.

    ``selected is None`` (or no group found for it — e.g. nothing was
    highlighted) is silent: there's nothing to show a menu for. Only a
    genuine view-only case — a group IS found but has no actions, i.e. a
    config-less/orphan repo — warns and waits for the user to acknowledge.
    """
    import questionary

    if selected is None:
        return
    group = _find_group(groups, selected)
    if group is None:
        return
    actions = actions_for_container(groups, selected)
    if not actions:
        warn(f"No config loaded for repo '{group.prefix}'; '{selected}' is view-only.")
        input("Press Enter to continue…")
        return

    # Explicit sentinel: `questionary.Choice` treats `value=None` as *unset*
    # and falls back to the title, so a cancel entry with `value=None` would
    # answer the string "cancel" and dispatch `jailbee cancel <name>`.
    cancel = "__cancel__"
    choices = [questionary.Choice(title=label, value=verb) for label, verb in actions]
    choices.append(questionary.Choice(title="cancel", value=cancel))
    verb = questionary.select(f"{selected} →", choices=choices).ask()
    # `None` is Ctrl-C; `cancel` is the menu entry. Both abort.
    if verb is None or verb == cancel:
        return
    assert group.config_path is not None  # has_config guaranteed actions non-empty
    subprocess.run(
        ["jailbee", *verb.split(), selected, "--config", str(group.config_path)], check=False
    )


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

    config_paths = collect_config_paths(cwd_config)
    if not config_paths:
        error("No repos registered and no .jailbee/config.yaml in the current directory.")
        return 1

    # Resolved once for the whole run — a live-refreshing dashboard must not
    # re-merge config on every frame.
    columns = resolve_dashboard_columns(cwd_config)

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
    shared_refreshing = True  # first frame shows the spinner until the initial gather lands
    worker_error: list[BaseException] = []

    def refresher() -> None:
        nonlocal shared_groups, shared_last_full, shared_refreshing
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
                with lock:
                    shared_refreshing = True
                try:
                    groups = gather_rows(
                        incus, config_paths, cwd_config=cwd_config, with_git=do_git
                    )
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
                    shared_refreshing = False
                prev_groups = groups
                last_base = ts
                if do_git:
                    last_full = ts
                first = False
            # sleep until the next tick, waking early on a forced refresh or stop
            force.wait(timeout=0.1)
        with lock:
            shared_refreshing = False

    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    selected: str | None = None
    sel_index = 0
    worker = threading.Thread(target=refresher, name="jailbee-dashboard-refresh", daemon=True)
    try:
        tty.setcbreak(fd)
        worker.start()
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while not stop.is_set():
                with lock:
                    groups = shared_groups
                    last_full = shared_last_full
                    refreshing = shared_refreshing
                names = selectable_names(groups)
                selected = reconcile_selection(names, selected, sel_index)
                if selected in names:
                    sel_index = names.index(selected)
                age = (time.monotonic() - last_full) if last_full else 0.0
                live.update(
                    render(
                        groups,
                        selected,
                        now=now(),
                        last_refresh_age=age,
                        interval=interval,
                        git_enabled=git_enabled,
                        refreshing=refreshing,
                        columns=columns,
                    ),
                    refresh=True,
                )
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not ready:
                    continue
                key = parse_key(os.read(fd, _KEY_READ_BYTES))
                if key == "quit":
                    break
                if key in ("up", "down"):
                    selected = move_selection(names, selected, -1 if key == "up" else 1)
                    if selected in names:
                        sel_index = names.index(selected)
                elif key == "enter":
                    live.stop()
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
                    try:
                        _open_action_menu(groups, selected)
                    finally:
                        tty.setcbreak(fd)
                        live.start(refresh=True)
                    force.set()  # an action likely changed state — refresh ASAP
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
