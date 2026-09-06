"""The JetBrains IDE as a registry entry.

Toolbox apps live at ``/opt/jetbrains-toolbox/apps/<app-id>/bin/<launcher>``.
The <app-id> varies across Toolbox versions and edition flavours
(``intellij-idea-ultimate``, ``pycharm-professional``), but the launcher
binary is always the IDE's short name — so the search matches by launcher
name, not by app-id. That search runs inside the container, which is why the
spec carries a `resolve_command` hook instead of a fixed path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

from jailbee.apps import AppSpec
from jailbee.config import IdeName

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

SUPPORTED_LAUNCHERS: frozenset[str] = frozenset(get_args(IdeName))
"""Allowed launcher names — the `IdeName` literal is the source of truth.

Resolved once at import time so the two cannot drift.
"""


def resolve_launcher(incus: Incus, container: str, app: str) -> list[str]:
    """The container-side path to `app`'s launcher, as a one-element argv."""
    if app not in SUPPORTED_LAUNCHERS:
        supported = ", ".join(sorted(SUPPORTED_LAUNCHERS))
        raise ValueError(f"Unknown IDE app: {app} (must be one of: {supported})")
    find_cmd = (
        f"find /opt/jetbrains-toolbox/apps -maxdepth 4 -type f -name '{app}' -executable | head -1"
    )
    found = incus.exec(container, ["bash", "-c", find_cmd]).strip()
    if not found:
        raise ValueError(f"No {app} launcher found in /opt/jetbrains-toolbox/apps")
    return [found]


def builtin_specs(cfg: Config) -> list[AppSpec]:
    """The configured JetBrains IDE, when the integration is enabled."""
    if not cfg.jetbrains.enabled:
        return []
    app = cfg.jetbrains.ide
    return [
        AppSpec(
            name="ide",
            command=[app],
            cwd="repo",
            top_level=True,
            autostart=cfg.jetbrains.autostart,
            source="builtin",
            description=f"JetBrains {app}",
            resolve_command=lambda incus, container: resolve_launcher(incus, container, app),
        )
    ]
