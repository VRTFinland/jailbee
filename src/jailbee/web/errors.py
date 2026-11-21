"""Why `jailbee remote web serve` cannot start. Standard library only."""

from __future__ import annotations

WEB_EXTRA_MODULES = frozenset({"fastapi", "starlette", "uvicorn", "websockets"})


class WebServeError(Exception):
    """A user-facing reason the web server cannot start; the message is shown as is."""


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
