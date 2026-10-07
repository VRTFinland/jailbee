"""jailbee dashboard — the Rich ``Live`` frontend over :class:`DashboardSession`.

Transitional (Textual migration V1): the Textual app in ``tui.app`` replaces
it, and the module goes once the loop tests have moved to Pilot.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard.tui.frame import render
from jailbee.dashboard.tui.session import DashboardSession, open_dashboard
from jailbee.dashboard.tui.terminal import set_terminal_title, terminal_title_scope
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.tui import console

if TYPE_CHECKING:
    from rich.live import Live

    from jailbee.incus import Incus
    from jailbee.state_service.client import StateClient

_KEY_READ_BYTES = 8  # covers all standard arrow/function-key CSI sequences


class _LiveTerminal:
    """:class:`Terminal` for a Rich ``Live`` screen in cbreak mode."""

    def __init__(self, live: Live, fd: int, old_term: list[Any], client: StateClient) -> None:
        self._live = live
        self._fd = fd
        self._old_term = old_term
        self._client = client
        self.last_title: str | None = None

    @property
    def width(self) -> int:
        return console.width

    def hand_off(self, fn: Callable[[], int]) -> int:
        """Hand the terminal to a real ``jailbee`` command, then take it back.

        Interactive verbs (``tmux``, ``shell``) need the raw terminal
        and the normal screen, so Live is stopped for the duration —
        but only for the *dispatch*. Opening the menu no longer
        touches the terminal at all, which is what keeps the
        dashboard on screen behind it.
        """
        # Nothing of this dashboard is on screen while `fn` runs: let the
        # shared service stop gathering on its behalf.
        self._client.set_active(False)
        self._live.stop()
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_term)
        try:
            return fn()
        finally:
            tty.setcbreak(self._fd)
            self._live.start(refresh=True)
            # The snapshot is as old as the command was long.
            self._client.set_active(True)
            self._client.refresh()
            # `fn` (jailbee shell / tmux) may have set its own OSC 2
            # title; forget the last one we wrote so the next frame's
            # title-changed check doesn't compare against it and skip
            # the rewrite, leaving the child's title on screen forever.
            self.last_title = None


def run(
    incus: Incus,
    cwd_root: Path | None,
    *,
    remote: bool = False,
    over_ssh: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    scope: RemoteRepoScope | None = None,
) -> int:
    """Main dashboard loop (see :class:`DashboardSession` for what ``remote`` withholds).

    Container state comes from the shared state service
    (`jailbee.state_service`), which gathers once for every open dashboard;
    this thread only renders the latest snapshot it pushed — with this
    dashboard's own scope and cwd pin applied — and handles input on a fast
    timer, so keystrokes stay responsive while a gather is in flight.
    """
    from rich.live import Live

    startup = open_dashboard(cwd_root, scope=scope)
    if isinstance(startup, int):
        return startup
    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        # Pushed before Live takes the screen and popped after it gives it
        # back, so the terminal's own title is saved and restored intact.
        with (
            terminal_title_scope(sys.stdout),
            Live(console=console, screen=True, auto_refresh=False) as live,
        ):
            terminal = _LiveTerminal(live, fd, old_term, startup.client)
            session = DashboardSession(
                startup,
                incus=incus,
                cwd_root=cwd_root,
                terminal=terminal,
                remote=remote,
                over_ssh=over_ssh,
                ssh_policy=ssh_policy,
                scope=scope,
            )
            while True:
                session.tick()
                # Only on change: an OSC 2 write on every frame makes some
                # terminals redraw their title bar continuously.
                title = session.title()
                if title != terminal.last_title:
                    set_terminal_title(title, stream=sys.stdout)
                    terminal.last_title = title
                frame = session.view().render_kwargs()
                groups, selected = frame.pop("groups"), frame.pop("selected")
                live.update(render(groups, selected, **frame, height=console.height), refresh=True)
                try:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                    if not ready:
                        continue
                    data = os.read(fd, _KEY_READ_BYTES)
                except KeyboardInterrupt:
                    # cbreak mode leaves ISIG on, so on a real terminal Ctrl-C
                    # arrives as SIGINT here, never as a b"\x03" byte. Turn it
                    # into that byte so the session is the one place that
                    # decides what Ctrl-C means: a text input (prompt, command
                    # line) cancels just itself, anything else quits.
                    data = b"\x03"
                if session.handle_input(data) == "quit":
                    break
    except KeyboardInterrupt:
        pass
    finally:
        startup.client.close()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
    return 0
