"""The terminal dashboard's key map: one table for the key loop and the help text."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyBinding:
    """One dashboard key: how it is typed, what it does, how it is described.

    :data:`KEY_BINDINGS` is the single source for both — :func:`parse_key` is
    built from ``keys``, the help overlay from ``hint``/``label``/``group``.
    Two hand-maintained lists would drift.

    Only keys the *menus* do not have are here. A container's or repo's own
    actions are the menu's letters, which work in the table without Enter
    (see ``DashboardSession.open_menu_for_letter``); a letter here must not
    be one of a menu's root letters.

    ``hint`` is empty for a token whose sibling documents it (``down`` is
    covered by ``up``'s "↑/↓ (j/k)").
    """

    token: str
    keys: tuple[str, ...]  # Textual key names (`events.Key.key`)
    hint: str
    label: str
    group: str


KEY_BINDINGS: tuple[KeyBinding, ...] = (
    KeyBinding("up", ("up", "k"), "↑/↓ (j/k)", "move the highlight", "Navigate"),
    KeyBinding("down", ("down", "j"), "", "", "Navigate"),
    KeyBinding("scroll-left", ("left",), "←/→", "scroll columns", "Navigate"),
    KeyBinding("scroll-right", ("right",), "", "", "Navigate"),
    KeyBinding(
        "enter",
        ("enter", "ctrl+j", "ctrl+m"),
        "Enter",
        "open the row's menu (its letters work without Enter)",
        "Navigate",
    ),
    KeyBinding(
        "cancel",
        ("escape",),
        "Esc",
        "close a menu, panel or help; cancel; clear marks",
        "Navigate",
    ),
    KeyBinding(
        "space",
        ("space",),
        "Space",
        "mark a container, fold a repo (Settings: toggle)",
        "Navigate",
    ),
    KeyBinding("extend-up", ("shift+up",), "Shift+↑/↓", "mark while moving", "Navigate"),
    KeyBinding("extend-down", ("shift+down",), "", "", "Navigate"),
    # Repo-scoped, not container-scoped: `run`'s dispatch handles it directly,
    # with its own guard.
    KeyBinding("new", ("n",), "n", "create a container in this repo", "Actions"),
    # Repo-scoped like `new`: the config being edited belongs to the repo, not
    # to the highlighted container.
    KeyBinding(
        "config-edit",
        ("e",),
        "e / E",
        "edit this repo's config (E: the global one)",
        "Actions",
    ),
    KeyBinding("config-edit-global", ("E",), "", "", "Actions"),
    # Host-wide, not row-scoped: the selected row only picks which repo the
    # `jailbee account …` children are run in.
    KeyBinding(
        "accounts",
        ("A",),
        "A",
        "credential groups and stored logins",
        "Actions",
    ),
    KeyBinding("optimize", ("O",), "O", "optimize column widths once", "View"),
    KeyBinding("refresh", ("f5", "ctrl+r"), "F5 / Ctrl-R", "force a full refresh", "View"),
    KeyBinding("details", ("v",), "v", "show/hide the details panel", "View"),
    KeyBinding("mouse", ("M",), "M", "mouse on/off (off: select text with the terminal)", "View"),
    KeyBinding(
        "settings",
        ("f2", "S"),
        "F2 / S",
        "columns and repo folding",
        "View",
    ),
    KeyBinding(
        "sort-prev", ("less_than_sign",), "< / >", "sort by the previous/next column", "View"
    ),
    KeyBinding("sort-next", ("greater_than_sign",), "", "", "View"),
    KeyBinding("sort-invert", ("I",), "I", "reverse the sort", "View"),
    KeyBinding("tab", ("tab",), "", "", "View"),
    KeyBinding("help", ("h", "question_mark"), "h / ?", "this help", "View"),
    KeyBinding("command", ("exclamation_mark",), "!", "run a jailbee command", "Actions"),
    KeyBinding("quit", ("q",), "q", "quit (closes an overlay first)", "View"),
    KeyBinding("interrupt", ("ctrl+c",), "Ctrl-C", "quit immediately", "View"),
)

_KEY_TOKENS: dict[str, str] = {k: b.token for b in KEY_BINDINGS for k in b.keys}


def parse_key(key: str) -> str:
    """Map a Textual key name to a dashboard key token ('' if unmapped).

    A pure lookup into :data:`KEY_BINDINGS`, so a key cannot be readable
    without also being documented in the help overlay.

    Note the three ways out: ``cancel`` (Esc) and ``quit`` (``q``) close an
    open overlay first, while ``interrupt`` (Ctrl-C) always ends the
    dashboard. Folding them into
    one token would leave Ctrl-C unable to do anything but shut the menu.
    """
    return _KEY_TOKENS.get(key, "")
