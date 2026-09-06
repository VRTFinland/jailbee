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
    default_url: str | None = None
    """The URL to open when `launch` is called with no explicit `args`.

    Kept out of `command` itself so a caller-supplied URL *replaces* it
    instead of both ending up on the argv — see `launch`.
    """
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


def _container_cwd(cfg: Config, incus: Incus, container: str, cwd: str) -> str:
    """Resolve a spec's `cwd` keyword to a container path."""
    from jailbee.config import CONTAINER_USERNAME

    if cwd == "home":
        return f"/home/{CONTAINER_USERNAME}"
    if cwd == "repo":
        from jailbee.lifecycle import container_repo_dir

        return container_repo_dir(cfg, incus, container)
    return cwd


def probe(
    cfg: Config, incus: Incus, container: str, spec: AppSpec
) -> Literal["present", "missing"]:
    """Is this app's binary actually in this container?

    The question `jailbee apps ls` exists to answer: a browser enabled in
    config but absent from an image built before it was enabled looks
    identical to a working one until you try to launch it.

    Runs as the container user, not root: `profiles.py` maps only the dev
    user's uid/gid identically between host and container
    (`raw.idmap: uid <uid> <uid>`), so container root is an unprivileged
    subuid with no rights over host-owned files. Browsers and the JetBrains
    Toolbox arrive as read-only bind mounts from the host, so a root-run
    check can report "missing" for an app that is present and working.

    Deliberately shaped so the container command always exits 0 — the
    answer is on stdout, not in the exit status — so an absent binary is
    reported as "missing" rather than raised as a nonzero exit. This does
    *not* guarantee `probe` never raises: a stopped container or a broken
    Incus daemon still surfaces as whatever `incus.exec` itself raises,
    and that is deliberately not swallowed here — turning "the daemon is
    down" into a cheerful "missing" would hide a real failure.

    A spec with `resolve_command` set (currently only the JetBrains IDE
    entry) never reaches the `command -v`/`test -x` check below: `command`
    on such a spec is a display placeholder (`ide.builtin_specs` sets it to
    the bare launcher name, e.g. `"idea"`), not a real container path — the
    Toolbox installs launchers under
    `/opt/jetbrains-toolbox/apps/<app-id>/bin/`, never on `PATH`, so the
    shell check would always answer "missing" for a working install. Run
    the resolver instead and treat its `ValueError` (raised by
    `resolve_launcher` when nothing matches) as "missing" — the same
    "answer on stdout, not on a raised failure" contract as the shell
    check, just enforced in Python instead of by the script's own always-0
    exit.
    """
    if spec.resolve_command is not None:
        try:
            spec.resolve_command(incus, container)
        except ValueError:
            return "missing"
        return "present"

    import shlex as _shlex

    binary = _shlex.quote(spec.command[0])
    script = (
        f"if command -v {binary} >/dev/null 2>&1 || test -x {binary}; "
        f"then echo present; else echo missing; fi"
    )
    out = incus.exec(
        container,
        ["bash", "-lc", script],
        uid=cfg.container_user.uid,
        gid=cfg.container_user.gid,
    ).strip()
    return "present" if out == "present" else "missing"


def launch(
    cfg: Config,
    incus: Incus,
    container: str,
    spec: AppSpec,
    args: list[str] | None = None,
) -> None:
    """Start `spec` in `container`, detached, logging inside the container."""
    import shlex as _shlex

    from jailbee.gui import gui_env, launch_detached
    from jailbee.tui import info

    if spec.pool is not None:
        from jailbee.pool import allocate as pool_allocate
        from jailbee.pool import ensure_pool_dirs
        from jailbee.pool import get as pool_get

        handle = pool_get(cfg, spec.pool)
        if handle is not None:
            ensure_pool_dirs(cfg, handle)
            pool_allocate(cfg, incus, handle, container)

    if spec.resolve_command is not None:
        argv = [*spec.resolve_command(incus, container), *spec.command[1:]]
    else:
        argv = list(spec.command)

    call_args = list(args or [])
    if call_args:
        argv += call_args
    elif spec.default_url:
        # No explicit args: fall back to the configured URL. Never both —
        # an explicit URL must replace the configured one, not join it.
        argv.append(spec.default_url)

    cwd = _container_cwd(cfg, incus, container, spec.cwd)
    log_path = app_log_path(spec.name)
    info(f"Launching {spec.name} in {container} (background, logs in container: {log_path})")
    launch_detached(
        container,
        cfg.container_user.uid,
        {**gui_env(cfg), **spec.env},
        " ".join(_shlex.quote(a) for a in argv),
        log_path,
        cwd=cwd,
    )


def launch_autostart_apps(cfg: Config, incus: Incus, container: str) -> None:
    """Start every app whose config asked to be started after autostart.

    Lives here rather than in `cli.py` so the two call sites (container
    create, container boot) share one implementation and `cli.py` stays a
    delegation layer.
    """
    for spec in resolve_apps(cfg):
        if spec.autostart:
            launch(cfg, incus, container, spec)
