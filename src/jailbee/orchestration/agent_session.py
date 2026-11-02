"""Relaunch only the prompted agent, without container or service startup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee import autostart, initial_prompt, lifecycle, tmux
from jailbee.agents import primary_agent
from jailbee.orchestration.labels import child_env

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.runner import Runner


def relaunch(
    cfg: Config,
    incus: Incus,
    child: str,
    prompt: str,
    *,
    runner: Runner | None,
    old_agent: str,
) -> None:
    """Replace one agent window using the same prompt wrapper as a boot."""
    target = "claude" if runner is not None else primary_agent(cfg).name
    steps = autostart.agent_autostart_steps(
        cfg, initial_prompt=True, extra_env=child_env(cfg, incus, child), runner=runner
    )
    step = next(s for s in steps if s.name == target)
    repo_dir = lifecycle.container_repo_dir(cfg, incus, child)
    env, cwd, timeout = autostart._step_invocation(cfg, step, repo_dir)
    # Stop the old wrapper before resetting its runtime/exit files: it must not
    # write a late exit status into the new session's monitoring state.
    tmux.kill_window(incus, child, tmux.window_for(old_agent))
    if target != old_agent:
        tmux.kill_window(incus, child, tmux.window_for(step.name))
    initial_prompt.write_prompt(
        incus, child, prompt, uid=cfg.container_user.uid, gid=cfg.container_user.gid
    )
    tmux.launch_step(
        incus, child, name=step.name, command=step.run, env=env, cwd=cwd,
        background=True, timeout=timeout,
    )
