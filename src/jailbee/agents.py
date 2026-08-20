"""Compile `agents:` config into the lifecycle's five agent hooks.

Pure: no `subprocess`, no Incus, no filesystem. `ensure_agents` (Task 5) is
the only impure part and goes through the `Incus` wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jailbee.config import ClaudeAgentConfig, SharedCache, device_name
from jailbee.constants import CLAUDE_PLUGIN_HOSTS

if TYPE_CHECKING:
    from jailbee.config import AgentConfig, Config


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
    env: dict[str, str] = field(default_factory=dict)


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
        env=dict(agent.env),
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
