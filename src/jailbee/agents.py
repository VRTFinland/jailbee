"""Compile `agents:` config into the lifecycle's five agent hooks.

Pure: no `subprocess`, no Incus, no filesystem. `ensure_agents` (Task 5) is
the only impure part and goes through the `Incus` wrapper.
"""

from __future__ import annotations

import importlib.resources
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.config import CONTAINER_USERNAME, ClaudeAgentConfig, SharedCache, device_name
from jailbee.constants import CLAUDE_PLUGIN_HOSTS
from jailbee.tui import info, warn

if TYPE_CHECKING:
    from typing import IO

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
    env = dict(agent.env)
    if isinstance(agent, ClaudeAgentConfig) and agent.plugins_enabled:
        egress = egress + CLAUDE_PLUGIN_HOSTS
    if name == "claude":
        # ensure-claude.sh's own "store already populated" branch keys
        # `claude update` off this exact env var, independent of which
        # command line (install vs. update) invoked it — both map to the
        # same bundled script (see `_resolve_bundled`). This is the dynamic,
        # Claude-specific value this module's existing convention (see the
        # `plugins_enabled` egress add above) puts here rather than in the
        # generic dispatch in `_ensure_one`.
        env["JAILBEE_CLAUDE_AUTO_UPDATE"] = "true" if agent.auto_update else "false"
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
        env=tuple(env.items()),
    )


def enabled_agent_specs(cfg: Config) -> list[AgentSpec]:
    """Specs for every enabled agent: others by name, then `claude` last.

    `claude` last preserves today's tmux behaviour — its window is the
    most-recently-created one, which is what `jailbee tmux` focuses.

    A single sorted pass over `cfg.agents`, keyed on `n == "claude"` to put
    claude at the end, rather than one pass for the other agents plus a
    `cfg.agents.get("claude")` special case. Two passes let the two disagree:
    on a `MagicMock` config `items()` iterates empty while
    `get("claude").enabled` is truthy, so a mocked config yielded a phantom
    Claude spec nobody had enabled.
    """
    names = sorted(cfg.agents, key=lambda n: (n == "claude", n))
    return [_spec(n, cfg.agents[n]) for n in names if cfg.agents[n].enabled]


_BUNDLED_PREFIX = "__bundled__:"


def _resolve_bundled(command: str) -> str:
    """Turn a `__bundled__:<script>` sentinel into the script's text.

    Only `claude_preset()` uses this today: `ensure-claude.sh` keeps its
    `versions/` symlink logic as a real script file rather than being
    inlined as a one-line shell command like every other agent's
    install/update. Any other command line passes through unchanged.

    The suffix is config-supplied, so it is rejected unless it is a bare
    filename: `__bundled__:../../../../etc/passwd` would otherwise build a
    traversing path whose content is then executed in the container. Not an
    escalation — a config that can write `install:` can equally write
    `install: cat /etc/passwd`, and a PR branch's `agents:` block never
    reaches here (see `branch_config`) — but the primitive exists only by
    accident, so it goes.
    """
    if not command.startswith(_BUNDLED_PREFIX):
        return command
    script_name = command[len(_BUNDLED_PREFIX) :]
    if not script_name or "/" in script_name or ".." in script_name:
        raise ValueError(
            f"bundled script name {script_name!r} must be a bare filename "
            f"(no '/' or '..')"
        )
    return importlib.resources.files("jailbee.provision").joinpath(script_name).read_text()


def _check_installed(incus: Incus, cfg: Config, container: str, spec: AgentSpec) -> bool:
    """Probe whether `spec` is already installed inside `container`.

    Runs `spec.install_check` (e.g. `command -v codex`) as the dev user via
    `incus.exec`. `--user UID` does not derive `HOME` from `/etc/passwd`
    (see `Incus.exec`'s docstring), and the profile.d snippets that put
    agent binaries on PATH (`~/.local/bin`, `~/.npm-global/bin`) expand
    against `$HOME` — so this must pass `HOME`/`USER`/`LOGNAME` explicitly,
    or every probe reports "not installed" even when the binary is present.

    Catches bare `Exception`, not just `IncusError`: this probe only
    decides install-vs-update, so it must never itself be fatal to
    `jailbee new` — a narrower catch would turn an unrelated wrapper error
    into a failed container creation instead of the safe fallback
    (treat as "not installed", attempt install).
    """
    home = f"/home/{CONTAINER_USERNAME}"
    try:
        incus.exec(
            container,
            ["bash", "-lc", spec.install_check],
            uid=cfg.container_user.uid,
            gid=cfg.container_user.gid,
            env={"HOME": home, "USER": CONTAINER_USERNAME, "LOGNAME": CONTAINER_USERNAME},
        )
        return True
    except Exception:
        return False


def _auto_update(cfg: Config, name: str) -> bool:
    agent = cfg.agents.get(name)
    return agent is not None and agent.auto_update


def _acquire_install_lock(cfg: Config) -> IO[str] | None:
    """Best-effort host-side flock serialising parallel `jailbee new` runs
    against the shared install prefix these commands write.

    Returns the open lock file (caller `close()`s it when done), or `None`
    when locking isn't possible — the caller still runs every install, just
    unlocked; the lock is a nice-to-have against a rare race, not a
    precondition for installing agents at all.

    Never creates `cfg.shared_dir`: that tree belongs to `init_command`.
    A missing/non-directory shared dir here means "install unlocked", not
    "create the tree ourselves" or "abort `jailbee new`".
    """
    import fcntl
    import os

    assert cfg.shared_dir is not None  # set by load_config
    if not cfg.shared_dir.is_dir():
        warn(f"Shared dir {cfg.shared_dir} not found; installing agents without a lock")
        return None

    lock_path = cfg.shared_dir / ".agent-install.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        lock = os.fdopen(fd, "r+")
    except OSError as e:
        warn(f"Could not open the agent-install lock file (continuing without it): {e}")
        return None

    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another `jailbee new` holds it — wait, but say so first: a silent
        # multi-minute stall here would look like a hang, not a queue.
        info("Waiting for another jailbee new to finish installing agents...")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError as e:
            warn(f"Could not acquire the agent-install lock (continuing without it): {e}")
            lock.close()
            return None
    return lock


def ensure_agents(
    cfg: Config,
    incus: Incus,
    container: str,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Install or update every enabled agent inside `container`.

    Called from `lifecycle.new_container` after mounts are attached (a
    shared install store must be present) and after the network ACL
    warm-up (installers need egress), before autostart execs the binaries.

    Each command goes through `autostart._apply_step`, which gives it a fresh
    `bash -lc` login shell. That is load-bearing rather than incidental:
    `~/.local/bin` and `~/.npm-global/bin` reach PATH only via
    /etc/profile.d, so `command -v <agent>` in a non-login shell would fail
    even with the binary installed — surfacing later as an opaque exit-127 in
    the autostart window.

    Locking (see `_acquire_install_lock`) is best-effort and never gates
    whether installs run — only whether they're serialised against other
    concurrent `jailbee new` runs sharing the same shared dir.
    """
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

    lock = _acquire_install_lock(cfg)
    try:
        for spec in specs:
            _ensure_one(cfg, incus, container, repo_dir, spec, mirror_endpoint)
    finally:
        if lock is not None:
            lock.close()


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

    try:
        # Building the step (including `_resolve_bundled`, which reads the
        # bundled script off disk) is inside this try along with running
        # it: a missing/unreadable bundled script must degrade to the same
        # per-agent warning as a failed install, not propagate past this
        # function and abort `jailbee new`.
        step = AutostartStep(
            name=f"install-{spec.name}",
            # `bash -c <script>` rather than the raw script text, for every
            # agent command — not just the bundled ones. `tmux.run_step`'s
            # sync path concatenates the command into a larger shell line
            # (`cd … && <command>; rc=$?; echo $rc > sentinel; tmux wait-for`),
            # which breaks two ways when `<command>` is a whole script:
            #   1. a multi-line command ending in a newline puts the `;` on a
            #      fresh line — a bash syntax error, so the sentinel is never
            #      written and the host blocks on `tmux wait-for` for the full
            #      `autostart.step_timeout`;
            #   2. even without the newline, `set -e`/`exit N` inside the
            #      script share the wrapper's shell and exit before the
            #      sentinel is written — the same stall, misreported as a
            #      timeout instead of the real exit code.
            # A child `bash -c` gives the script its own shell, so its exit
            # status flows into `rc=$?` and the sentinel is always written.
            # This also makes a user's multi-line `install: |` block scalar
            # behave the same way.
            run=f"bash -c {shlex.quote(_resolve_bundled(command))}",
            network="loose" if spec.install_network == "loose" else None,
            env=dict(spec.env),
        )
        _apply_step(cfg, incus, container, step, repo_dir, mirror_endpoint=mirror_endpoint)
    except Exception as e:  # non-fatal: never block container creation
        warn(f"{spec.name} install/update step failed (continuing): {e}")
