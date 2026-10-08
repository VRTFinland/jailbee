"""The terminal dashboard as a Textual app around one :class:`DashboardSession`.

V1 shows the whole frame (:func:`render_view`) in one ``Static`` and feeds
keys to the session through the transitional key adapter; native widgets
replace both in V2-V3.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Size
from textual.widgets import Static

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard.hit import Hit
from jailbee.dashboard.tui.frame import DashboardView, render_view
from jailbee.dashboard.tui.key_adapter import legacy_bytes
from jailbee.dashboard.tui.session import (
    DOUBLE_CLICK_KINDS,
    DashboardSession,
    Outcome,
    Startup,
    open_dashboard,
)
from jailbee.dashboard.tui.terminal import terminal_title_scope, title_sequence
from jailbee.remote_ssh.repo_scope import RemoteRepoScope

if TYPE_CHECKING:
    from textual.driver import Driver

    from jailbee.incus import Incus

TICK_SECONDS = 0.25
FRAME_INSET_COLS = 4  # Panel border plus `padding=(0, 1)`.


def _can_suspend(driver: Driver | None) -> bool:
    """Whether ``App.suspend`` can hand the terminal over (not headless, not web)."""
    return driver is not None and driver.can_suspend


def _set_mouse_reporting(driver: Driver | None, on: bool) -> None:
    """Switch the terminal's mouse reporting, and keep it switched across hand-offs.

    Textual 8.2.8 has no public toggle. Its driver writes the reporting modes
    from the private ``_mouse`` flag at start and again on every resume after
    ``suspend()``, so the flag is flipped too: otherwise `jb tmux` and back
    would silently turn the mouse on again.
    """
    if driver is None:
        return
    if on:
        driver._mouse = True
        enable = getattr(driver, "_enable_mouse_support", None)
        if enable is not None:
            enable()
    else:
        disable = getattr(driver, "_disable_mouse_support", None)
        if disable is not None:
            disable()
        driver._mouse = False


class DashboardApp(App[int], inherit_bindings=False):
    """The dashboard's Textual frontend: one frame, keys, timer, hand-off, title.

    ``inherit_bindings=False`` drops Textual's own ``ctrl+c``/``ctrl+q``/
    ``ctrl+p`` bindings: every key belongs to the session, so Ctrl-C can
    cancel a prompt instead of quitting.
    """

    CSS = """
    Screen { background: ansi_default; }
    #frame { background: ansi_default; color: ansi_default; }
    """
    ENABLE_COMMAND_PALETTE = False
    # Textual's selection binds double-click (select all) and a copy key that
    # would compete with the dashboard's own; the terminal's Shift-drag stays.
    ALLOW_SELECT = False

    def __init__(
        self,
        startup: Startup,
        *,
        incus: Incus,
        cwd_root: Path | None,
        remote: bool = False,
        over_ssh: bool = False,
        ssh_policy: RemoteSSHConfig | None = None,
        scope: RemoteRepoScope | None = None,
        tick_seconds: float | None = TICK_SECONDS,
    ) -> None:
        super().__init__(ansi_color=True)
        self.session = DashboardSession(
            startup,
            incus=incus,
            cwd_root=cwd_root,
            terminal=self,
            remote=remote,
            over_ssh=over_ssh,
            ssh_policy=ssh_policy,
            scope=scope,
        )
        self._tick_seconds = tick_seconds
        self._painted: tuple[DashboardView, Size] | None = None
        self._last_title: str | None = None
        self.hover: Hit | None = None
        self.quit_requested = False
        self.mouse_on = startup.mouse
        self._last_click: Hit | None = None

    # --- Terminal protocol -------------------------------------------------

    @property
    def table_width(self) -> int:
        return max(0, self.size.width - FRAME_INSET_COLS)

    def hand_off(self, fn: Callable[[], int]) -> int:
        """Run ``fn`` on the real terminal with the dashboard suspended."""
        client = self.session.client
        # Nothing of this dashboard is on screen while `fn` runs: let the
        # shared service stop gathering on its behalf.
        client.set_active(False)
        suspended: AbstractContextManager[None] = (
            self.suspend() if _can_suspend(self._driver) else nullcontext()
        )
        try:
            with suspended:
                return fn()
        finally:
            # The snapshot is as old as the command was long.
            client.set_active(True)
            client.refresh()
            # `fn` may have set its own OSC 2 title: rewrite ours on the next frame.
            self._last_title = None
            self._painted = None

    # --- Textual -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        frame = Static(id="frame")
        # Textual restyles `@click` links; the frame tags its own targets and
        # paints its own hover (see `jailbee.dashboard.hit`).
        frame.auto_links = False
        yield frame

    def on_mount(self) -> None:
        self.refresh_frame()
        if self._tick_seconds is not None:
            self.set_interval(self._tick_seconds, self.refresh_frame)

    def on_resize(self, _event: events.Resize) -> None:
        self._painted = None
        self.refresh_frame()

    def on_key(self, event: events.Key) -> None:
        data = legacy_bytes(event.key, event.character)
        if data is None:
            return
        event.stop()
        event.prevent_default()
        self._after(self.session.handle_input(data))

    def on_paste(self, event: events.Paste) -> None:
        """A bracketed paste reaches the prompt or command line as one input.

        Line breaks are dropped (a trailing one, as copied lines carry, leaves
        no stray character): the single-line inputs would otherwise reject
        the whole chunk as non-printable. With no text input open the paste is
        ignored — fed to the table it would fire shortcuts (a pasted ``q``).
        """
        event.stop()
        if not self.session.text_input_open:
            return
        text = "".join(event.text.strip("\r\n").splitlines())
        if text:
            self._after(self.session.handle_input(text.encode()))

    # --- frame -------------------------------------------------------------

    def refresh_frame(self) -> None:
        """Refresh the session and repaint if anything visible changed."""
        self.session.tick()
        title = self.session.title()
        if title != self._last_title:
            # Only on change: an OSC 2 write on every frame makes some
            # terminals redraw their title bar continuously.
            self._write_terminal(title_sequence(title))
            self._last_title = title
        view = self.session.view(self.hover)
        painted = (view, self.size)
        if painted == self._painted:
            return
        self.query_one("#frame", Static).update(render_view(view, height=self.size.height))
        self._painted = painted

    def _after(self, outcome: Outcome) -> None:
        if outcome == "quit":
            self.quit_requested = True
            self.exit(0)
            return
        if outcome == "toggle-mouse":
            self.toggle_mouse()
            return
        self.refresh_frame()

    def toggle_mouse(self) -> None:
        """`m`: mouse reporting on or off for this session."""
        self.mouse_on = not self.mouse_on
        _set_mouse_reporting(self._driver, self.mouse_on)
        self.hover = None
        self.session.set_notice(
            "mouse on" if self.mouse_on else "mouse off — terminal text selection active"
        )
        self.refresh_frame()

    def on_click(self, event: events.Click) -> None:
        hit = Hit.of(event.style.meta)
        if event.chain > 1:
            # The first click of the pair already acted; only a row-like target
            # that the first click hit too, clicked twice, means Enter there.
            # A right-click never pairs: its first click already opened the menu.
            if (
                event.button == 1
                and hit is not None
                and hit == self._last_click
                and hit.kind in DOUBLE_CLICK_KINDS
            ):
                self._last_click = None  # a third click of the chain acts on nothing
                self.session.click(hit, double=True)
                self.refresh_frame()
            return
        self._last_click = hit
        self.session.click(hit, right=event.button == 3)
        self.refresh_frame()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        hit = Hit.of(event.style.meta)
        if hit == self.hover:
            return
        self.hover = hit
        self.session.hover(hit)
        self.refresh_frame()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.session.wheel(1, columns=event.shift)
        self.refresh_frame()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.session.wheel(-1, columns=event.shift)
        self.refresh_frame()

    def on_mouse_scroll_right(self, _event: events.MouseScrollRight) -> None:
        self.session.wheel(1, columns=True)
        self.refresh_frame()

    def on_mouse_scroll_left(self, _event: events.MouseScrollLeft) -> None:
        self.session.wheel(-1, columns=True)
        self.refresh_frame()

    def _write_terminal(self, sequence: str) -> None:
        """Write a raw control sequence in order with Textual's own output."""
        driver = self._driver
        if driver is not None:
            driver.write(sequence)
            driver.flush()


def run(
    incus: Incus,
    cwd_root: Path | None,
    *,
    remote: bool = False,
    over_ssh: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    scope: RemoteRepoScope | None = None,
) -> int:
    """Open the terminal dashboard; return its exit code."""
    startup = open_dashboard(cwd_root, scope=scope)
    if isinstance(startup, int):
        return startup
    try:
        app = DashboardApp(
            startup,
            incus=incus,
            cwd_root=cwd_root,
            remote=remote,
            over_ssh=over_ssh,
            ssh_policy=ssh_policy,
            scope=scope,
        )
        # Pushed before Textual takes the screen and popped after it gives it
        # back, so the terminal's own title is saved and restored intact.
        with terminal_title_scope(sys.stdout):
            rc = app.run(mouse=startup.mouse)
        # An unhandled handler exception ends Textual with return code 1 and no value.
        rc = app.return_code or rc or 0
    except KeyboardInterrupt:
        rc = 0
    finally:
        startup.client.close()
    return rc
