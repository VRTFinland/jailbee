"""The frame's border texts, the help and hint lines, and the view the frame shows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from rich.markup import escape
from rich.style import Style
from rich.text import Text

from jailbee.config.models_remote import RemotePolicy
from jailbee.dashboard import accounts as da
from jailbee.dashboard import hit as dhit
from jailbee.dashboard.commands import Transport
from jailbee.dashboard.egress import EgressState
from jailbee.dashboard.model import RepoGroup, Row
from jailbee.dashboard.notifications import QueueCounts
from jailbee.dashboard.outbox import OutboxState, outbox_hint
from jailbee.dashboard.overlays import (
    PICKER_HINT,
    PROMPT_HINT,
    SUGGEST_HINT,
    Picker,
    TextPrompt,
)
from jailbee.dashboard.settings import SettingsState
from jailbee.dashboard.sorting import DEFAULT_SORT, SortSpec
from jailbee.dashboard.system import SystemStatus
from jailbee.dashboard.tui.keys import KEY_BINDINGS
from jailbee.dashboard.tui.menu_state import GlobalMenuState, MenuState, RepoMenuState
from jailbee.dashboard.tui.overlay import CommandState, Overlay
from jailbee.update_check import UpdateNotice

INLINE_NOTICE_MAX = 80  # longer notices wrap below the table instead of the border


def frame_title(
    groups: Sequence[RepoGroup],
    folded: frozenset[str],
    *,
    git_enabled: bool,
    now: datetime,
    marked: int = 0,
) -> Text:
    """The frame border's summary and clock, independent of its body."""
    n_repos = len({g.prefix for g in groups})
    n_ctr = sum(len(g.containers) for g in groups)
    n_folded = len({g.prefix for g in groups if g.prefix in folded and g.containers})
    folded_note = f" · {n_folded} folded" if n_folded else ""
    folded_note += f" · {marked} selected" if marked else ""
    git_note = "" if git_enabled else "  ·  [dim](no-git)[/dim]"
    title = Text()
    title.append("☰", style=Style(bold=True) + dhit.hit_style("global-menu"))
    title.append(" ")
    title.append_text(
        Text.from_markup(
            f"[bold]🐝 jailbee dashboard[/] · [dim]h/? help[/]"
            f"  ·  {n_repos} repos · {n_ctr} containers{folded_note}{git_note}  ·  {now:%H:%M:%S}"
        )
    )
    return title


def notice_parts(notice: str | None, *, wraps: bool = True) -> tuple[Text | None, Text | None]:
    """Plain yellow notice as (border subtitle, wrapped inline text).

    ``wraps`` False keeps even a long notice on the border: a standing status
    line (a running job, a stale target) has no remedy to lose to the ellipsis.
    """
    # Long refusals keep their remedy intact below the table. Never parse CLI markup.
    if not notice:
        return None, None
    if wraps and len(notice) > INLINE_NOTICE_MAX:
        return None, Text(notice, style="yellow")
    # Keep the verdict at the start when the border is narrower than the notice.
    return Text(notice, style="yellow", no_wrap=True, overflow="ellipsis"), None


def update_line(notice: UpdateNotice, *, hover: dhit.Hit | None = None) -> Text:
    """The standing "newer jailbee" row, its `[dismiss]` label a click target.

    A row of its own rather than the notice border: the border is one slot
    shared with refusals and running jobs, and cannot carry a click target.
    """
    dismiss = dhit.Hit("dismiss-update")
    label = Style(
        bold=True,
        underline=hover == dismiss,
        meta={dhit.HIT_KEY: dismiss.meta_value()},
    )
    line = Text(
        f"jailbee {notice.latest} available (running {notice.current}) — {notice.command}   ",
        style="yellow",
    )
    line.append("[dismiss]", style=label)
    return line


def queue_badge(counts: QueueCounts, *, accent: str, frame: int = 0) -> Text:
    """The queue's running, pending-question and failure counts, one click target.

    Non-zero counts are bold: running and pending use the theme accent, failures red.
    Only a running queue cycles through frames; an idle one keeps its static glyph.
    """
    hit = Style(meta={dhit.HIT_KEY: dhit.Hit("queue").meta_value()})
    badge = Text()
    glyph = ("⟳", "◷", "◶", "◵")[frame % 4] if counts.running else "⟳"
    badge.append(
        f"{glyph} {counts.running}",
        style=(Style(color=accent, bold=True) if counts.running else Style(dim=True)) + hit,
    )
    if counts.pending:
        badge.append(" · ", style=Style(dim=True) + hit)
        badge.append(f"? {counts.pending}", style=Style(bold=True, color=accent) + hit)
    badge.append(" · ", style=Style(dim=True) + hit)
    badge.append(
        f"✗ {counts.failed}",
        style=(Style(bold=True, color="red") if counts.failed else Style(dim=True)) + hit,
    )
    return badge


_MENU_LETTERS_NOTE = (
    "A container's or repo's actions are its menu's letters, and they work here "
    "without Enter (l Lifecycle, …); Enter shows the whole list. "
    "F10 or ☰ opens Global; g opens Git (repo: Agent). A letter "
    "does nothing when its entry is not offered for the highlighted row: a stopped "
    "container has no tmux or shell, and orphan rows are view-only."
)


def help_lines() -> list[str]:
    """The keybinding help as markup lines, grouped as the key table declares.

    Rows come from :data:`KEY_BINDINGS`, so a new key documents itself. The
    closing note says where the rest of the keys are: a row's actions are its
    menu's letters, which the menu itself lists.
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
    return [
        *lines,
        "",
        "System line: the bare number is the 1-minute load average; yellow ≥0.7, red ≥1.0 per CPU.",
        "ST: ▶ running, ■ stopped, Ⅱ frozen; LOOSE: ● loose + time left, ∞ no revert.",
        "MOUNT: a letter per repo mount kind; [dim]off[/dim]; on, by time left:",
        "  [bold red]∞[/bold red] [bold dark_orange]>4h[/bold dark_orange]"
        " [bold yellow]<4h[/bold yellow] [bold green]<1h[/bold green]"
        " [bold cyan]<15m[/bold cyan]; names and times in the details panel.",
        "AGE: container age; AI: agent status (✖ API error, ◆ waiting, ● busy, ◐ shell, ○ idle;",
        "  age: latest event; bright ○: activity <30 min ago). OUTBOX ✉N: staged PR/issue actions.",
        "ROLE: orch orchestrator, child (child! orchestrator not running);"
        " CHILDREN running/limit +queued; INBOX ✉N actionable unread.",
        "BASE ↗: remote-tracking base; MODE: cln clone, mnt mount.",
        "WT / SYNC diff / L DIFF: ✓ clean; L DIFF vs host HEAD.",
        "SYNC: target marker, ↑ahead, ↓behind, diff; MERGE uses the same target.",
        "  pr = PR head; pr? = PR head not fetched, host branch;",
        "  ⑂ = parent container (↑ not imported); ⑂? = parent not seen running, host branch.",
        "DOING ×N: process count; JOB auto:stage: autostart stage.",  # noqa: RUF001 - intentional multiplication sign
        "Menus: the key in brackets picks that entry, like Enter on it.",
        "Outbox box: Tab switches Outbox/Sent; in Sent, o opens the URL (or shows it), v preview.",
        "Repo menu Sent…: the repo's sent timeline; † marks a destroyed or recreated container.",
        "Egress panel: a adds, r removes a scoped override; Esc backs to its menu.",
        "Accounts panel: Enter acts on a login or group, n creates a group.",
        "Repo menu: Apply config…, Diagnostics →, Prune stale containers…",
        "Global menu (F10 or ☰): Stop all…, Resume…, Accounts, Notifications,",
        "  Global config, Settings, Help, Quit. Stop all/Resume use CLI prompt modals.",
        "Container menu: Snapshots…, Mounts…, autostart status/cancel.",
        "Mouse: click selects; Ctrl-click marks; double- or right-click opens the menu;",
        "▾/▸ folds, ‹ › step columns; the wheel scrolls, Shift+wheel steps columns;",  # noqa: RUF001 - the arrows the frame draws
        "Shift-drag selects text.",
        "",
        f"[dim]{_MENU_LETTERS_NOTE}[/dim]",
    ]


_MENU_PICK_HINT = "[bold]\\[key][/bold] pick"


def _hint_line(overlay: Overlay | None) -> str:
    """Contextual controls shown only while an overlay is open."""
    if isinstance(overlay, GlobalMenuState):
        return (
            f"[bold]↑/↓[/bold] move  ·  {_MENU_PICK_HINT}  ·  [bold]Enter[/bold] run  ·  "
            "[bold]Esc[/bold] close  ·  [bold]q[/bold] quit"
        )
    if isinstance(overlay, (MenuState, RepoMenuState)):
        return (
            f"[bold]↑/↓[/bold] move  ·  {_MENU_PICK_HINT}  ·  [bold]Enter[/bold] open/run  ·  "
            "[bold]Esc[/bold] back  ·  [bold]q[/bold] close"
        )
    if isinstance(overlay, EgressState):
        parts = ["[bold]↑/↓[/bold] move"]
        if overlay.can_add:
            parts.append("[bold]a[/bold] add")
        if overlay.can_rm:
            parts.append("[bold]r[/bold] remove")
        parts.append("[bold]Esc[/bold] back")
        return "  ·  ".join(parts)
    if isinstance(overlay, SettingsState):
        return (
            "[bold]↑/↓[/bold] move  ·  [bold]Space[/bold] toggle  ·  "
            "Fields: [bold]Shift+↑/↓[/bold] reorder  ·  [bold]Tab[/bold] switch  ·  "
            "[bold]Esc[/bold] close"
        )
    if isinstance(overlay, CommandState):
        return "[bold]Enter[/bold] run  ·  [bold]Tab[/bold] complete  ·  [bold]Esc[/bold] cancel"
    if isinstance(overlay, TextPrompt):
        return SUGGEST_HINT if overlay.suggestions else PROMPT_HINT
    if isinstance(overlay, Picker):
        return PICKER_HINT
    if isinstance(overlay, da.AccountsState):
        return da.ACCOUNTS_HINT
    if isinstance(overlay, OutboxState):
        return escape(outbox_hint(overlay, standalone=False))
    if overlay is not None:  # "help"
        return "[bold]Esc[/bold] / [bold]h[/bold] close"
    return ""


@dataclass(frozen=True)
class DashboardView:
    """Everything the native frame shows, produced after each refresh and key.

    Equality is cheap enough to skip unchanged repaints.
    """

    groups: list[RepoGroup]
    selected: Row | None
    now: datetime
    git_enabled: bool
    enabled: Sequence[str] | None
    overlay: Overlay | None
    notice: str | None
    folded: frozenset[str]
    column_offset: int
    hidden_by_preferences: bool
    show_details: bool
    column_widths: Mapping[str, int] | None
    shown_columns: Sequence[str] | None
    hover: dhit.Hit | None = None  # the hovered table target
    sort: SortSpec = DEFAULT_SORT
    marked: frozenset[str] = frozenset()
    running: frozenset[str] = frozenset()
    # The merge-target mode's roles (see `fleet.table_model`); empty outside it.
    sources: frozenset[str] = frozenset()
    ineligible: frozenset[str] = frozenset()
    # False for a standing status line, kept on the border however long (`notice_parts`).
    notice_wraps: bool = True
    # A newer jailbee release to advertise on a row of its own (`update_line`).
    update: UpdateNotice | None = None
    # Remote SSH session facts, so the details panel offers only permitted links.
    transport: Transport = "none"
    ssh_policy: RemotePolicy | None = None
    # Every repo, hidden ones too (None: `groups`), for the upgrade-all decision.
    all_groups: list[RepoGroup] | None = None
    # The host status line above the table; drawn only while `show_system` and known.
    system: SystemStatus | None = None
    show_system: bool = True
    # The notification queue's badge counts; None while the queue is empty.
    queue: QueueCounts | None = None
    queue_frame: int = 0
    # Content rows scrolled off the top of the details panel.
    details_scroll: int = 0
    # The details panel's chosen content height; None: the built-in one.
    details_rows: int | None = None
