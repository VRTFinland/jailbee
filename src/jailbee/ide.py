"""JetBrains IDE AppSpecs (stub; replaced in Task 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.apps import AppSpec

if TYPE_CHECKING:
    from jailbee.config import Config


def builtin_specs(cfg: Config) -> list[AppSpec]:
    """Placeholder; the real specs land in Task 9 (ide)."""
    return []
