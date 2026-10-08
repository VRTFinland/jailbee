"""The terminal dashboard's overlay union and the ``!`` command line state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from jailbee.dashboard import accounts as da
from jailbee.dashboard.commands import (
    apply_completion,
)
from jailbee.dashboard.egress import (
    EgressState,
)
from jailbee.dashboard.overlays import (
    Picker,
    TextPrompt,
    decode_input,
)
from jailbee.dashboard.settings import (
    SettingsState,
)
from jailbee.dashboard.tui.menu_state import MenuState, RepoMenuState


# What occupies the slot under the table. Overlays are mutually exclusive by
# construction — no combination of them is a representable state.
@dataclass(frozen=True)
class CommandState:
    """Inline command editor state, independent of terminal/input handling."""

    text: str
    suggestions: tuple[str, ...] = ()
    index: int = -1
    pending_utf8: bytes = b""


def edit_command(state: CommandState, key: bytes) -> CommandState:
    """Apply one editor key, keeping ordinary dashboard shortcuts as text."""
    if key in (b"\x7f", b"\x08"):
        if state.pending_utf8:
            return replace(state, pending_utf8=b"")
        return replace(state, text=state.text[:-1], index=-1)
    if key == b"\t":
        if not state.suggestions:
            return state
        index = (state.index + 1) % len(state.suggestions)
        return replace(
            state,
            text=apply_completion(state.text, state.suggestions[index]),
            index=index,
        )
    if key in (b"\r", b"\n", b"\x1b", b"\x03", b""):
        return state
    appended, pending = decode_input(state.pending_utf8, key)
    if not appended:
        return replace(state, pending_utf8=pending)
    return replace(state, text=state.text + appended, index=-1, pending_utf8=pending)


Overlay = (
    MenuState
    | RepoMenuState
    | EgressState
    | SettingsState
    | CommandState
    | TextPrompt
    | Picker
    | da.AccountsState
    | Literal["help"]
)


def _egress_panel(overlay: Overlay | None) -> EgressState | None:
    """The Egress panel on screen: itself, or the one behind its question."""
    if isinstance(overlay, EgressState):
        return overlay
    if isinstance(overlay, (TextPrompt, Picker)) and isinstance(overlay.back, EgressState):
        return overlay.back
    return None


def _with_egress_panel(overlay: Overlay | None, panel: EgressState) -> Overlay | None:
    """``overlay`` with the Egress panel it shows (or sits over) swapped for ``panel``."""
    if isinstance(overlay, EgressState):
        return panel
    if isinstance(overlay, (TextPrompt, Picker)):
        return replace(overlay, back=panel)
    return overlay


@dataclass(frozen=True)
class NativeState:
    """What an open native overlay shows now: its cursor, a menu's level, settings' tab.

    Read by the tests and the PTY rig; the session never sees it.
    """

    kind: str
    cursor: int | None
    level: str | None = None
    tab: str | None = None


def is_native(overlay: Overlay | None) -> bool:
    """Whether ``overlay`` is drawn by a native box (the rest by `_render_overlay`)."""
    return overlay == "help" or isinstance(
        overlay, (Picker, MenuState, RepoMenuState, SettingsState, EgressState, da.AccountsState)
    )


def overlay_key(overlay: Overlay | None) -> tuple[object, ...] | None:
    """Which overlay this is, ignoring its data and initial cursor.

    The frame keeps a mounted box while the key is unchanged (new data goes to
    `OverlayBox.show`), so a tick that rebuilds a panel's rows never resets the
    cursor; a box's message is acted on only while its key is still the open one.
    """
    if overlay is None:
        return None
    if overlay == "help":
        return ("help",)
    if isinstance(overlay, MenuState):
        return ("menu", overlay.container)
    if isinstance(overlay, RepoMenuState):
        return ("repo-menu", overlay.repo)
    if isinstance(overlay, Picker):
        return ("picker", overlay.purpose, overlay.target, overlay.title, overlay.entries)
    if isinstance(overlay, SettingsState):
        return ("settings",)
    if isinstance(overlay, EgressState):
        return ("egress", overlay.prefix, overlay.container)
    if isinstance(overlay, da.AccountsState):
        return ("accounts", overlay.prefix)
    if isinstance(overlay, TextPrompt):
        return ("prompt", overlay.purpose, overlay.target)
    return ("command",)
