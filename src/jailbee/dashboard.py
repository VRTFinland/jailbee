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
from typing import TYPE_CHECKING, Literal

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
    from collections.abc import Callable

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
    KeyBinding("up", (b"\x1b[A", b"k"), "↑/↓ (j/k)", "move the highlight", "Navigate", brief="move"),
    KeyBinding("down", (b"\x1b[B", b"j"), "", "", "Navigate"),
    KeyBinding("enter", (b"\r", b"\n"), "Enter", "open the action menu", "Navigate", brief="menu"),
    KeyBinding("cancel", (b"\x1b",), "Esc", "close the menu or help", "Navigate"),
    KeyBinding("action:tmux", (b"t",), "t", "attach tmux", "Actions", verb="tmux", brief="tmux"),
    KeyBinding("action:shell", (b"s",), "s", "open a shell", "Actions", verb="shell", brief="shell"),
    KeyBinding("action:ide", (b"i",), "i", "launch the IDE", "Actions", verb="ide"),
    KeyBinding("action:chrome", (b"c",), "c", "launch Chrome", "Actions", verb="chrome"),
    KeyBinding("action:pr", (b"p",), "p", "open the PR", "Actions", verb="pr --open"),
    KeyBinding("refresh", (b"r",), "r", "force a full refresh", "View", brief="refresh"),
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
    "and orphan rows are view-only."
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


# What occupies the slot under the table. Both overlays are mutually exclusive
# by construction — "menu and help open at once" is not a representable state.
Overlay = MenuState | Literal["help"]


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
    if overlay is not None:  # "help"
        return "[bold]Esc[/bold] / [bold]h[/bold] close"
    return "  ·  ".join(
        f"[bold]{b.hint}[/bold] {b.brief}" for b in KEY_BINDINGS if b.brief is not None
    )


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
    overlay: Overlay | None = None,
    notice: str | None = None,
) -> RenderableType:
    """Build the Rich renderable for one dashboard frame.

    One shared table (columns aligned across all repos); each repo is a
    section header row inside it. The selected container is marked with a
    ``▸`` gutter arrow and bold styling. Wrapped in a rounded Panel with a
    title (summary + refresh indicator) and a subtitle (refresh timing).

    ``overlay`` is an open action menu or the keybinding help, drawn *below*
    the table so the dashboard it acts on stays on screen. ``notice`` is a
    transient message
    (a rejected key, a view-only row) shown in the subtitle; the keybinding
    hint lives in the panel body, where Rich can wrap it instead of the
    subtitle silently clipping it.
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

    body: list[RenderableType] = [table, ""]
    if overlay is not None:
        panel = _render_menu(overlay) if isinstance(overlay, MenuState) else _render_help()
        body += [panel, ""]
    body.append(_hint_line(overlay))

    n_repos = len({g.prefix for g in groups})
    n_ctr = len(all_containers)
    mark = "  [yellow]⟳[/]" if refreshing else ""
    title = (
        f"[bold]jailbee dashboard[/]   {n_repos} repos · {n_ctr} containers   {now:%H:%M:%S}{mark}"
    )
    git_note = "" if git_enabled else "  ·  [dim](no-git)[/dim]"
    note = f"[yellow]{notice}[/yellow]  ·  " if notice else ""
    subtitle = f"{note}refreshed {last_refresh_age:.0f}s ago · every {interval:.0f}s{git_note}"
    return Panel(Group(*body), title=title, subtitle=subtitle, box=box.ROUNDED, padding=(0, 1))


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


def _dispatch_action(config_path: Path, verb: str, name: str) -> int:
    """Run ``jailbee <verb> <name> --config <config_path>``; return its exit code.

    The single dispatch point shared by the inline action menu and the
    quick-action keys, so both reuse the real command's behaviour and the
    target repo's own config. ``verb`` may be multi-token (``"net loose"``,
    ``"pr --open"``).
    """
    proc = subprocess.run(
        ["jailbee", *verb.split(), name, "--config", str(config_path)], check=False
    )
    return proc.returncode


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

    worker = threading.Thread(target=refresher, name="jailbee-dashboard-refresh", daemon=True)
    try:
        tty.setcbreak(fd)
        worker.start()
        with Live(console=console, screen=True, auto_refresh=False) as live:

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

            while not stop.is_set():
                with lock:
                    groups = shared_groups
                    last_full = shared_last_full
                    refreshing = shared_refreshing
                names = selectable_names(groups)
                if isinstance(overlay, MenuState) and overlay.container not in names:
                    # The menu's container vanished under it (destroyed, or its
                    # repo dropped out of the registry) — close rather than
                    # dispatch at a name that is no longer there.
                    set_notice(f"'{overlay.container}' is gone — menu closed")
                    overlay = None
                if isinstance(overlay, MenuState):
                    selected = overlay.container  # pinned while the menu is open
                else:
                    selected = reconcile_selection(names, selected, sel_index)
                if selected in names:
                    sel_index = names.index(selected)
                if notice is not None and time.monotonic() >= notice_until:
                    notice = None
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
                        overlay=overlay,
                        notice=notice,
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
                    selected = move_selection(names, selected, -1 if key == "up" else 1)
                    if selected in names:
                        sel_index = names.index(selected)
                elif key == "enter":
                    overlay = open_menu(groups, selected)
                    if overlay is None and selected is not None:
                        note = view_only_note(groups, selected)
                        set_notice(note or f"No actions available for '{selected}'")
                elif key == "help":
                    overlay = "help"
                elif key.startswith("action:"):
                    verb = quick_verb(groups, selected, key)
                    if verb is not None and selected is not None:
                        dispatch(selected, verb)
                    else:
                        set_notice(quick_reject_note(groups, selected, key))
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
