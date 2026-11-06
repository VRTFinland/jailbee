"""Shared-golden ownership checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sqlmodel import Session, select

from jailbee.config import SCRATCH_BASE_ALIAS
from jailbee.db import get_engine

if TYPE_CHECKING:
    from collections.abc import Iterable

    from jailbee.config import Config
    from jailbee.db.models import RegisteredRepo


class _ConfigLoader(Protocol):
    def __call__(self, row: RegisteredRepo | None, /) -> Config | None: ...


def _load_registered_config(row: RegisteredRepo | None) -> Config | None:
    # Local import avoids a cycle: repo_remove also delegates its loader here.
    from jailbee.repo_remove import _load_registered_config as load

    return load(row)


def shared_golden_reason(
    prefix: str,
    alias: str,
    rows: Iterable[RegisteredRepo],
    load: _ConfigLoader = _load_registered_config,
) -> str | None:
    """Return why a golden must be retained rather than exclusively managed."""
    if alias == SCRATCH_BASE_ALIAS:
        return "shared scratch base"
    for row in rows:
        if row.container_prefix == prefix:
            continue
        cfg = load(row)
        if cfg is None:
            return f"config unavailable for {row.container_prefix}"
        if cfg.golden.alias == alias:
            return f"shared with {row.container_prefix}"
    return None


def shared_golden_reason_for(cfg: Config) -> str | None:
    """Check whether this repo's golden is shared with another registration."""
    from jailbee.db.models import RegisteredRepo

    with Session(get_engine()) as session:
        rows = session.exec(select(RegisteredRepo)).all()
    return shared_golden_reason(cfg.container_prefix, cfg.golden.alias, rows)
