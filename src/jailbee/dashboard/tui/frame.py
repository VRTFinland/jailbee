"""The overlays' Rich renderers and frame border texts until V3 makes overlays native."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from rich import box
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.panel import Panel
from rich.text import Text

from jailbee.dashboard import accounts as da
from jailbee.dashboard import hit as dhit
from jailbee.dashboard.egress import (
    EgressState,
    removable_entry,
    render_egress,
)
from jailbee.dashboard.menus import MenuGroup
from jailbee.dashboard.model import RepoGroup, Row
from jailbee.dashboard.overlays import (
    PICKER_HINT,
    PROMPT_HINT,
    SUGGEST_HINT,
    Picker,
    TextPrompt,
    render_picker,
    render_prompt,
    window_lines,
)
from jailbee.dashboard.settings import (
    CURSOR_STYLE,
    SettingsState,
    render_settings,
)
from jailbee.dashboard.tui.keys import _GATE_NOTE, KEY_BINDINGS
from jailbee.dashboard.tui.menu_state import MenuState, RepoMenuState, _menu_entries, menu_hotkeys
from jailbee.dashboard.tui.overlay import CommandState, Overlay

INLINE_NOTICE_MAX = 80  # longer notices wrap below the table instead of the border


def frame_title(
    groups: Sequence[RepoGroup], folded: frozenset[str], *, git_enabled: bool, now: datetime
) -> Text:
    """The frame border's summary and clock, independent of its body."""
    n_repos = len({g.prefix for g in groups})
    n_ctr = sum(len(g.containers) for g in groups)
    n_folded = len({g.prefix for g in groups if g.prefix in folded and g.containers})
    folded_note = f" · {n_folded} folded" if n_folded else ""
    git_note = "" if git_enabled else "  ·  [dim](no-git)[/dim]"
    return Text.from_markup(
        f"[bold]🐝 jailbee dashboard[/]  ·  [dim]h/? help[/]"
        f"  ·  {n_repos} repos · {n_ctr} containers{folded_note}{git_note}  ·  {now:%H:%M:%S}"
    )


def notice_parts(notice: str | None) -> tuple[Text | None, Text | None]:
    """Plain yellow notice as (border subtitle, wrapped inline text)."""
    # Long refusals keep their remedy intact below the table. Never parse CLI markup.
    if not notice:
        return None, None
    if len(notice) > INLINE_NOTICE_MAX:
        return None, Text(notice, style="yellow")
    # Keep the verdict at the start when the border is narrower than the notice.
    return Text(notice, style="yellow", no_wrap=True, overflow="ellipsis"), None


def _render_menu(menu: MenuState | RepoMenuState, max_rows: int | None = None) -> RenderableType:
    """The action menu as a bordered panel: one row per action, cursor on the
    highlighted one, windowed to ``max_rows`` around the cursor."""
    entries = _menu_entries(menu)
    lines = []
    for i, (item, key) in enumerate(zip(entries, menu_hotkeys(entries), strict=True)):
        label = item.label if isinstance(item, MenuGroup) else item[0]
        tag = f"[bold]\\[{key}][/]" if key else "   "
        line = (
            f"[bold cyan]▸[/] {tag} [{CURSOR_STYLE}]{label}[/]"
            if i == menu.index
            else f"  {tag} {label}"
        )
        lines.append(dhit.hit_markup(line, "menu", i))
    if isinstance(menu, RepoMenuState):
        title = (
            f"{menu.repo} → {menu.active_group.removesuffix(' →')}"
            if menu.active_group
            else f"{menu.repo} →"
        )
    elif menu.active_group:
        title = f"{menu.container} → {menu.active_group.removesuffix(' →')}"
    else:
        title = f"{menu.container} →"
    return Panel(
        "\n".join(window_lines(lines, menu.index, max_rows)),
        title=f"[bold]{title}[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        expand=False,
    )


def help_lines() -> list[str]:
    """The keybinding help as markup lines, grouped as the key table declares.

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
    return [
        *lines,
        "",
        "ST: ▶ running, ■ stopped, Ⅱ frozen; NET: ● strict, ○ loose.",
        "AGE: container age; AI: agent status (◆ waiting, ● busy, ◐ shell, ○ idle).",
        "BASE ↗: remote-tracking base; MODE: cln clone, mnt mount.",
        "WT / DIFF / L DIFF: ✓ clean; DIFF vs host target, L DIFF vs host HEAD.",
        "DOING ×N: process count; JOB auto:stage: autostart stage.",  # noqa: RUF001 - intentional multiplication sign
        "Menus: the key in brackets picks that entry, like Enter on it.",
        "Egress panel: a adds, r removes a scoped override; Esc backs to its menu.",
        "Accounts panel: Enter acts on a login or group, n creates a group.",
        "Repo menu: Apply config…, Diagnostics →, Prune stale containers…",
        "Container menu: Snapshots…, Mount…/Unmount…, autostart status/cancel.",
        "Mouse: click selects; double- or right-click opens the menu;",
        "▾/▸ folds, ‹ › step columns; the wheel scrolls, Shift+wheel steps columns;",  # noqa: RUF001 - the arrows the frame draws
        "Shift-drag selects text.",
        "",
        f"[dim]{_GATE_NOTE}[/dim]",
    ]


_MENU_PICK_HINT = "[bold]\\[key][/bold] pick"


def _hint_line(overlay: Overlay | None) -> str:
    """Contextual controls shown only while an overlay is open."""
    if isinstance(overlay, MenuState):
        if overlay.active_group is not None:
            return (
                f"[bold]↑/↓[/bold] move  ·  {_MENU_PICK_HINT}  ·  [bold]Enter[/bold] run  ·  "
                "[bold]Esc[/bold] back  ·  [bold]q[/bold] close"
            )
        return (
            f"[bold]↑/↓[/bold] move  ·  {_MENU_PICK_HINT}  ·  "
            "[bold]Enter[/bold] open/run  ·  [bold]Esc[/bold] cancel"
        )
    if isinstance(overlay, RepoMenuState):
        return (
            f"[bold]↑/↓[/bold] move  ·  {_MENU_PICK_HINT}  ·  "
            "[bold]Enter[/bold] run  ·  [bold]Esc[/bold] cancel"
        )
    if isinstance(overlay, EgressState):
        return (
            "[bold]↑/↓[/bold] move  ·  [bold]a[/bold] add  ·  "
            "[bold]r[/bold] remove  ·  [bold]Esc[/bold] back"
        )
    if isinstance(overlay, SettingsState):
        return (
            "[bold]↑/↓[/bold] move  ·  [bold]Space[/bold] toggle  ·  "
            "[bold]Tab[/bold] switch  ·  [bold]Esc[/bold] close"
        )
    if isinstance(overlay, CommandState):
        return "[bold]Enter[/bold] run  ·  [bold]Tab[/bold] complete  ·  [bold]Esc[/bold] cancel"
    if isinstance(overlay, TextPrompt):
        return SUGGEST_HINT if overlay.suggestions else PROMPT_HINT
    if isinstance(overlay, Picker):
        return PICKER_HINT
    if isinstance(overlay, da.AccountsState):
        return da.ACCOUNTS_HINT
    if overlay is not None:  # "help"
        return "[bold]Esc[/bold] / [bold]h[/bold] close"
    return ""


def _render_overlay(overlay: Overlay, max_rows: int | None = None) -> RenderableType:
    """The overlay's panel; ``max_rows`` windows the scrollable list overlays."""
    if isinstance(overlay, EgressState):
        return render_egress(
            overlay,
            can_add=overlay.can_add,
            can_rm=overlay.can_rm and removable_entry(overlay) is not None,
        )
    if isinstance(overlay, (MenuState, RepoMenuState)):
        return _render_menu(overlay, max_rows)
    if isinstance(overlay, CommandState):
        lines = [f"> {overlay.text}▏"]
        if overlay.suggestions:
            lines.append("  " + "   ".join(overlay.suggestions))
        return Panel("\n".join(lines), title="command", box=box.ROUNDED, expand=False)
    if isinstance(overlay, SettingsState):
        return render_settings(overlay, dynamic=frozenset())
    if isinstance(overlay, TextPrompt):
        return render_prompt(overlay)
    if isinstance(overlay, Picker):
        return render_picker(overlay, max_rows)
    if isinstance(overlay, da.AccountsState):
        return da.render_accounts(overlay)
    raise ValueError(f"{overlay!r} is drawn by a native box")  # see is_native


@dataclass(frozen=True)
class HoverHighlight:
    """``renderable`` with the hovered click target given :data:`HOVER_STYLE`."""

    renderable: RenderableType
    hover: dhit.Hit | None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from dhit.hover_segments(console.render(self.renderable, options), self.hover)


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
    hover: dhit.Hit | None = None
