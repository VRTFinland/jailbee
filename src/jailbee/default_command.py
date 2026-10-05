"""What `jailbee` with no subcommand runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from jailbee.config import ConfigError

if TYPE_CHECKING:
    from jailbee.global_config import GlobalConfig

DefaultCommand = Literal["dashboard", "gui", "console", "help"]


def resolve(
    *, interactive: bool, load: Callable[[], GlobalConfig]
) -> tuple[DefaultCommand, str | None]:
    """Pick what bare `jailbee` opens; return it and a warning to print, if any.

    Off a terminal it is always help, without reading any config: a script
    must never land in a TUI. Only the global config is read — a broken or
    missing repo config must not stop the dashboard opening — and a broken
    global one falls back to the default rather than failing, because the
    dashboard is where the user would go to see what is wrong.
    """
    if not interactive:
        return "help", None
    try:
        return load().default_command, None
    except ConfigError as exc:
        return "dashboard", f"{exc} — opening the dashboard"
