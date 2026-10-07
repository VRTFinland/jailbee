"""Short-lived scoped egress row loader for the dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlmodel import Session

from jailbee.config import load_repo_config
from jailbee.db import get_engine
from jailbee.egress_scope import EntryRow, classify_sources

if TYPE_CHECKING:
    from jailbee.incus import Incus


def load_egress_rows(root: Path, incus: Incus, container: str | None) -> tuple[EntryRow, ...]:
    """Load config and classify its applicable rows in a short-lived session."""
    cfg = load_repo_config(root)
    with Session(get_engine()) as session:
        return tuple(classify_sources(cfg, session, incus, container=container))
