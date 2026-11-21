"""Why `jailbee remote web serve` cannot start. Standard library only."""

from __future__ import annotations

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
    """The optional `web` extra is not installed."""

    def __init__(self) -> None:
        super().__init__(
            "The web dashboard requires the optional 'web' extra. "
            "Install it with: uv tool install 'jailbee[web]'"
        )


class FrontendNotBuiltError(WebServeError):
    """The package carries no built browser app (an editable install before `make web`)."""

    def __init__(self, directory: object) -> None:
        super().__init__(
            f"The web dashboard's browser app is not built (no index.html in {directory}). "
            "Run `make web` in the jailbee checkout first."
        )


def is_missing_web_extra(exc: ModuleNotFoundError) -> bool:
    """Whether ``exc`` is a module of the `web` extra failing to import."""
    return (exc.name or "").split(".")[0] in WEB_EXTRA_MODULES


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
