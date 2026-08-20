"""Compile `agents:` config into the lifecycle's five agent hooks.

Pure: no `subprocess`, no Incus, no filesystem. `ensure_agents` (Task 5) is
the only impure part and goes through the `Incus` wrapper.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.config import ClaudeAgentConfig, SharedCache, device_name
from jailbee.constants import CLAUDE_PLUGIN_HOSTS
from jailbee.tui import warn

if TYPE_CHECKING:
    from jailbee.config import AgentConfig, Config
    from jailbee.incus import Incus


@dataclass(frozen=True)
class AgentSpec:
    name: str
    command: str
    autostart: bool
    shared: tuple[SharedCache, ...]
    dir_subpaths: tuple[str, ...]
    seed_files: tuple[tuple[str, str], ...]
    egress: tuple[str, ...]
    install: str | None
    install_check: str
    update: str | None
    install_network: str
    env: tuple[tuple[str, str], ...] = ()


def _spec(name: str, agent: AgentConfig) -> AgentSpec:
    shared = tuple(
        SharedCache(
            name=device_name(m.subpath),
            host_subpath=m.subpath,
            container_path=m.path,
        )
        for m in agent.shared
    )
    dirs = tuple(m.subpath for m in agent.shared if m.type == "dir")
    seeds = tuple((m.subpath, m.seed or "") for m in agent.shared if m.type == "file")
    egress = tuple(agent.egress_allow)
    if isinstance(agent, ClaudeAgentConfig) and agent.plugins_enabled:
        egress = egress + CLAUDE_PLUGIN_HOSTS
    return AgentSpec(
        name=name,
        command=agent.command,
        autostart=agent.autostart,
        shared=shared,
        dir_subpaths=dirs,
        seed_files=seeds,
        egress=egress,
        install=agent.install,
        install_check=agent.effective_install_check(),
        update=agent.update,
        install_network=agent.install_network,
        env=tuple(agent.env.items()),
    )


def enabled_agent_specs(cfg: Config) -> list[AgentSpec]:
    """Specs for every enabled agent: others by name, then `claude` last.

    `claude` last preserves today's tmux behaviour — its window is the
    most-recently-created one, which is what `jailbee tmux` focuses.
    """
    names = sorted(n for n, a in cfg.agents.items() if a.enabled and n != "claude")
    specs = [_spec(n, cfg.agents[n]) for n in names]
    claude = cfg.agents.get("claude")
    if claude is not None and claude.enabled:
        specs.append(_spec("claude", claude))
    return specs


_BUNDLED_PREFIX = "__bundled__:"


def _resolve_bundled(command: str) -> str:
    """Turn a `__bundled__:<script>` sentinel into the script's text.

    Only `claude_preset()` uses this today: `ensure-claude.sh` keeps its
    `versions/` symlink logic as a real script file rather than being
    inlined as a one-line shell command like every other agent's
    install/update. Any other command line passes through unchanged.
    """
    if not command.startswith(_BUNDLED_PREFIX):
        return command
    script_name = command[len(_BUNDLED_PREFIX) :]
    return importlib.resources.files("jailbee.provision").joinpath(script_name).read_text()


def _check_installed(incus: Incus, cfg: Config, container: str, spec: AgentSpec) -> bool:
    """Probe whether `spec` is already installed inside `container`.

    Runs `spec.install_check` (e.g. `command -v codex`) as the dev user via
    `incus.exec`. Catches bare `Exception`, not just `IncusError`: this probe
    only decides install-vs-update, so it must never itself be fatal to
    `jailbee new` — a narrower catch would turn an unrelated wrapper error
    into a failed container creation instead of the safe fallback
    (treat as "not installed", attempt install).
    """
    try:
        incus.exec(
            container,
            ["bash", "-lc", spec.install_check],
            uid=cfg.container_user.uid,
            gid=cfg.container_user.gid,
        )
        return True
    except Exception:
        return False


def _auto_update(cfg: Config, name: str) -> bool:
    agent = cfg.agents.get(name)
    return agent is not None and agent.auto_update


def ensure_agents(
    cfg: Config,
    incus: Incus,
    container: str,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Install or update every enabled agent inside `container`.

    Runs in the `_ensure_claude_in_container` slot: after mounts are attached
    (a shared install store must be present), after the network ACL warm-up
    (installers need egress), before autostart execs the binaries.

    Each command goes through `autostart._apply_step`, which gives it a fresh
    `bash -lc` login shell. That is load-bearing rather than incidental:
    `~/.local/bin` and `~/.npm-global/bin` reach PATH only via
    /etc/profile.d, so `command -v <agent>` in a non-login shell would fail
    even with the binary installed — surfacing later as an opaque exit-127 in
    the autostart window.

    A host-side flock serialises parallel `jailbee new` runs sharing a shared
    dir, since a shared install prefix is written by these commands. Failures
    are warnings: an agent is optional and the container must stay usable.
    """
    import fcntl

    from jailbee.tmux import ensure_session

    specs = [s for s in enabled_agent_specs(cfg) if s.install or s.update]
    if not specs:
        return

    # `_apply_step` runs each command in a tmux window (see its docstring on
    # PATH), which requires the container's autostart tmux session to
    # already exist. At this point in the lifecycle no other caller has
    # created it yet (`inject_github_token` / `run_autostart` do so later),
    # so this is the first thing that needs it. Best-effort: a failure here
    # degrades to the same per-agent warning every install step already
    # gets, rather than aborting `jailbee new`.
    try:
        ensure_session(incus, container, start_dir=repo_dir)
    except Exception as e:
        warn(f"Could not start the autostart tmux session for agent install (continuing): {e}")

    def _install_all() -> None:
        for spec in specs:
            _ensure_one(cfg, incus, container, repo_dir, spec, mirror_endpoint)

    assert cfg.shared_dir is not None  # set by load_config
    lock_path = cfg.shared_dir / ".agent-install.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch(exist_ok=True)
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _install_all()
    except OSError as e:
        # The lock itself is infrastructure, not an agent: a missing/
        # unwritable shared dir must degrade to "install without
        # serialisation", never to a failed `jailbee new`.
        warn(f"Could not acquire the agent-install lock (continuing without it): {e}")
        _install_all()


def _ensure_one(
    cfg: Config,
    incus: Incus,
    container: str,
    repo_dir: str,
    spec: AgentSpec,
    mirror_endpoint: tuple[str, int] | None,
) -> None:
    installed = _check_installed(incus, cfg, container, spec)
    command = spec.update if installed else spec.install
    if command is None:
        return
    if installed and not _auto_update(cfg, spec.name):
        return

    from jailbee.autostart import _apply_step
    from jailbee.config import AutostartStep

    env = dict(spec.env)
    if spec.name == "claude":
        # ensure-claude.sh (see its "store already populated" branch) keys
        # `claude update` off this exact env var itself, independent of
        # which command line (install vs. update) invoked it — both map to
        # the same bundled script. The generic dispatch above only decided
        # whether to run the script at all; this tells the script what to
        # do once it's running.
        env["JAILBEE_CLAUDE_AUTO_UPDATE"] = "true" if _auto_update(cfg, spec.name) else "false"

    step = AutostartStep(
        name=f"install-{spec.name}",
        run=_resolve_bundled(command),
        network="loose" if spec.install_network == "loose" else None,
        env=env,
        continue_on_error=True,
    )
    try:
        _apply_step(cfg, incus, container, step, repo_dir, mirror_endpoint=mirror_endpoint)
    except Exception as e:  # non-fatal: never block container creation
        warn(f"{spec.name} install/update step failed (continuing): {e}")
