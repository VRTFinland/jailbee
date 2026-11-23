"""Why `jailbee remote web serve` cannot start. Standard library only."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

WEB_EXTRA_MODULES = frozenset({"fastapi", "starlette", "uvicorn", "websockets", "pyotp", "qrcode"})


class WebServeError(Exception):
    """A user-facing reason the web server cannot start; the message is shown as is."""


class ServiceUpdatedError(WebServeError):
    """The server stopped because the installed JailBee version changed.

    The unit's `Restart=on-failure` starts it again on the new code, so the
    CLI exits non-zero (`SERVICE_UPDATED_EXIT`, EX_TEMPFAIL) on purpose.
    """

    def __init__(self, running: str, installed: str) -> None:
        super().__init__(f"JailBee was updated from {running} to {installed}; restarting")
        self.running = running
        self.installed = installed


SERVICE_UPDATED_EXIT = 75


class WebDependencyError(WebServeError):
    """The web dashboard's dependencies are missing (a partial or broken install)."""

    def __init__(self) -> None:
        super().__init__(
            "The web dashboard's dependencies are missing. "
            "Reinstall JailBee: uv tool install --reinstall jailbee"
        )


class FrontendNotBuiltError(WebServeError):
    """The package carries no built browser app (a checkout install without `make web`)."""

    def __init__(self, directory: object) -> None:
        super().__init__(
            f"The web dashboard's browser app is not built (no index.html in {directory}). "
            "In the jailbee checkout, run `make install-web` (or `make web` for an "
            "editable install)."
        )


def is_missing_web_extra(exc: ModuleNotFoundError) -> bool:
    """Whether ``exc`` is a web dashboard dependency failing to import."""
    return (exc.name or "").split(".")[0] in WEB_EXTRA_MODULES


@contextmanager
def web_extra_required() -> Iterator[None]:
    """Turn a web dashboard dependency missing inside the block into `WebDependencyError`."""
    try:
        yield
    except ModuleNotFoundError as exc:
        if not is_missing_web_extra(exc):
            raise
        raise WebDependencyError() from exc


class UnauthenticatedBindError(WebServeError):
    """Auth is off and the listen address is not loopback, without the explicit opt-in."""

    def __init__(self, listen: str) -> None:
        super().__init__(
            f"remote.web.auth.enabled is false and remote.web.listen is {listen}, which is not "
            "a loopback address: anyone who can reach the port would see the dashboard. "
            "Listen on 127.0.0.1 or ::1, turn auth on, or set "
            "remote.web.auth.allow_unauthenticated_remote: true to accept that."
        )


class ServerNotRunningError(WebServeError):
    """`jailbee remote web open` found nothing listening on the configured address."""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"The web dashboard server is not running (nothing answers at {url}). "
            "Start it with `jailbee remote web serve` or `jailbee remote web enable`."
        )
