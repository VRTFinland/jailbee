"""`jailbee remote web serve` (and `auth fido2 setup`) below the CLI: overrides, extra, server.

Imports no FastAPI at module level, so a missing `web` extra surfaces as
`WebDependencyError` rather than an import traceback. The CLI only maps the
errors raised here to an exit status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.web.errors import WebServeError, web_extra_required

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.config.models_remote import RemoteWebConfig


class InvalidOverrideError(WebServeError):
    """A `--listen` / `--port` value the config model refuses."""


def effective_config(
    configured: RemoteWebConfig, *, listen: str | None, port: int | None
) -> RemoteWebConfig:
    """``configured`` with this run's overrides; nothing is written back."""
    from pydantic import ValidationError

    from jailbee.config.models_remote import RemoteWebConfig

    overrides = {k: v for k, v in (("listen", listen), ("port", port)) if v is not None}
    try:
        return RemoteWebConfig.model_validate({**configured.model_dump(), **overrides})
    except ValidationError as exc:
        raise InvalidOverrideError(f"Invalid override: {exc}") from exc


def serve(
    configured: RemoteWebConfig,
    *,
    listen: str | None,
    port: int | None,
    setup: bool,
    echo: Callable[[str], None],
) -> None:
    """Run the dashboard (or FIDO2 setup mode) in the foreground until it stops.

    Raises `WebServeError` subclasses for every user-facing refusal:
    `InvalidOverrideError`, `WebDependencyError`, and whatever the server
    raises (`ServiceUpdatedError` after an upgrade).
    """
    effective = effective_config(configured, listen=listen, port=port)
    with web_extra_required():
        from jailbee.web import server, setup_server
    if setup:
        setup_server.serve_setup(effective, echo=echo)
    else:
        server.serve(effective, echo=echo)
