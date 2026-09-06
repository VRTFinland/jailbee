"""The GUI application registry: what a container can launch, and how.

Builtin applications (browsers, JetBrains IDEs) and user-defined `apps:`
entries resolve to one `AppSpec` shape, so every surface — the CLI, autostart,
the dashboards — reads one list instead of naming Chrome and the IDE.

This module calls no `subprocess` of its own: container commands go through
the `Incus` wrapper, and GUI launches through `gui.launch_detached`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

AppSource = Literal["builtin", "config"]


@dataclass(frozen=True)
class AppSpec:
    """One launchable application, whatever declared it."""

    name: str
    command: list[str]
    cwd: str = "repo"
    env: dict[str, str] = field(default_factory=dict)
    pool: str | None = None
    top_level: bool = False
    autostart: bool = False
    source: AppSource = "config"
    description: str = ""
    accepts_url: bool = False
    resolve_command: Callable[[Incus, str], list[str]] | None = None
    """Container-side lookup for a command whose path is not fixed.

    JetBrains Toolbox app-ids vary by version and edition, so the IDE's
    binary is found by searching the container at launch time. `None` means
    `command` is already the final argv.
    """


def app_log_path(name: str) -> str:
    """Where a launched app's stdout and stderr go, inside the container.

    A single naming scheme across every app: the user diagnosing "the window
    never appeared" needs to guess only the app's own name.
    """
    return f"/tmp/jailbee-app-{name}.log"


def resolve_apps(cfg: Config) -> list[AppSpec]:
    """Every app this config can launch: builtins first, then `apps:` by name.

    Order is deliberate and must not depend on YAML key order — `jailbee apps
    ls` and the dashboard action menu both render it.
    """
    from jailbee.browsers import builtin_specs as browser_specs
    from jailbee.ide import builtin_specs as ide_specs

    specs: list[AppSpec] = [*browser_specs(cfg), *ide_specs(cfg)]
    for name in sorted(cfg.apps):
        entry = cfg.apps[name]
        specs.append(
            AppSpec(
                name=name,
                command=[*entry.command, *entry.args],
                cwd=entry.cwd,
                env=dict(entry.env),
                top_level=entry.top_level,
                autostart=entry.autostart,
                source="config",
                description=entry.description,
            )
        )
    return specs


def get_app(cfg: Config, name: str) -> AppSpec:
    """The spec named `name`, or a `ValueError` naming what is available."""
    specs = resolve_apps(cfg)
    for spec in specs:
        if spec.name == name:
            return spec
    available = ", ".join(s.name for s in specs) or "none"
    raise ValueError(f"Unknown app: {name}. Available: {available}.")
