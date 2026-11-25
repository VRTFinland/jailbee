"""Host-wide clean shutdown and restoration after a host reboot."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.tui import error_plain, info_plain, success, warn_plain

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.lifecycle import ContainerInfo
    from jailbee.dashboard.model import RepoGroup


def _state_path() -> Path:
    from jailbee.db import state_dir

    return state_dir() / "resume-set.json"


@dataclass
class ResumeSet:
    containers: dict[str, str] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)
    stopping: dict[str, str] = field(default_factory=dict)


def _read_set() -> ResumeSet:
    path = _state_path()
    if not path.exists():
        return ResumeSet()
    try:
        data = json.loads(path.read_text())
        entries = data["containers"]
        if (
            data["version"] != 1
            or not isinstance(entries, dict)
            or not all(
                isinstance(name, str) and isinstance(root, str) for name, root in entries.items()
            )
        ):
            raise ValueError("unsupported resume set")
        pending = data.get("pending", [])
        if not isinstance(pending, list) or not all(
            isinstance(name, str) and name in entries for name in pending
        ):
            raise ValueError("invalid pending resume entries")
        stopping = data.get("stopping", {})
        if not isinstance(stopping, dict) or not all(
            isinstance(name, str) and isinstance(root, str) for name, root in stopping.items()
        ):
            raise ValueError("invalid stopping entries")
        return ResumeSet(dict(entries), set(pending), dict(stopping))
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc


def resumable_repos(groups: list[RepoGroup]) -> list[str]:
    """Repos with an existing saved container, for the shared state snapshot."""
    try:
        state = _read_set()
    except (OSError, ValueError):
        return []
    names = state.containers.keys() | state.stopping.keys()
    return sorted(
        g.prefix for g in groups
        if g.repo_root is not None and any(c.name in names for c in g.containers)
    )


def _write_set(state: ResumeSet) -> None:
    path = _state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".resume-set-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {
                    "version": 1,
                    "containers": state.containers,
                    "pending": sorted(state.pending),
                    "stopping": state.stopping,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def operation() -> Iterator[None]:
    """Serialize host-wide lifecycle operations, including their Incus side effects."""
    path = _state_path().with_name("resume-operation.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            info_plain("Waiting for another stop/resume operation...")
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def _edit_set() -> Iterator[ResumeSet]:
    path = _state_path().with_suffix(".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never unlink the lock: waiters must keep using the same inode.
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = _read_set()
            yield state
            _write_set(state)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def remove(name: str) -> None:
    """Forget a destroyed or successfully resumed container under the journal lock."""
    with _edit_set() as state:
        state.containers.pop(name, None)
        state.pending.discard(name)
        state.stopping.pop(name, None)


def _configs(cfg: Config, failures: dict[Path, str] | None = None) -> dict[str, Config]:
    from jailbee.config import ConfigError, load_repo_config
    from jailbee.dashboard.model import registered_repo_roots

    from jailbee.remote_ssh.repo_scope import scope_for_session
    from jailbee.remote_ssh.session import is_remote_session

    scope = scope_for_session() if is_remote_session() else None
    configs = {cfg.container_prefix: cfg} if scope is None or scope.allows(cfg.container_prefix) else {}
    for root in registered_repo_roots():
        if root == cfg.repo_root:
            continue
        try:
            other = load_repo_config(root)
        except ConfigError as exc:
            if failures is None:
                warn_plain(f"Cannot load repository {root}: {exc}")
            else:
                failures[root] = str(exc)
            continue
        if scope is None or scope.allows(other.container_prefix):
            configs[other.container_prefix] = other
    return configs


def _session_containers(cfg: Config, incus: Incus, configs: dict[str, Config]) -> list[ContainerInfo]:
    from jailbee.lifecycle import list_containers
    from jailbee.remote_ssh.session import is_remote_session

    containers = list_containers(cfg, incus, all_repos=True)
    if is_remote_session():
        # A profile alone does not authorize an orphan; require a loaded repo.
        containers = [c for c in containers if c.repo in configs]
    return containers


def _live_job(cfg: Config, incus_name: str) -> bool:
    from jailbee import background
    from jailbee.lifecycle import lookup_background_job

    row = lookup_background_job(cfg, incus_name)
    return row is not None and not background.clearable(row.phase, row.pid, live=row.observed_live)


def run_stop_all(
    cfg: Config,
    incus: Incus,
    *,
    yes: bool = False,
    stop: Callable[[Config, Incus, str], None],
) -> bool:
    """Stop running development containers, recording only successful stops."""
    with operation():
        return _stop_all(cfg, incus, yes=yes, stop=stop)


def _stop_all(
    cfg: Config,
    incus: Incus,
    *,
    yes: bool,
    stop: Callable[[Config, Incus, str], None],
) -> bool:
    from jailbee import prompting
    from jailbee.remote_ssh.session import is_remote_session

    configs = _configs(cfg)
    containers = [c for c in _session_containers(cfg, incus, configs) if c.state == "Running"]
    if not containers:
        info_plain("No running JailBee containers to stop.")
        return True
    extent = "visible repositories" if is_remote_session() else "all repositories"
    detail = f"Running JailBee containers across {extent}:\n" + "\n".join(
        f"  {c.name}" for c in containers
    )
    if yes:
        info_plain(detail)
    if not yes and not prompting.ask_confirm(
        "Stop all listed containers?", destructive=True, alternative="--yes", detail=detail
    ):
        return True
    # Check persistence before stopping anything; preserve earlier batches.
    with _edit_set():
        pass
    ok = True
    for container in containers:
        try:
            target = configs.get(container.repo or "")
            if target is None:
                raise ValueError("repository configuration is unavailable")
            if _live_job(target, container.name):
                warn_plain(f"Skipping {container.name}: live background job.")
                continue
            # Journal before the side effect; recovery checks whether it stopped.
            with _edit_set() as state:
                state.stopping[container.name] = str(target.repo_root)
            stop(target, incus, container.name)
            with _edit_set() as state:
                state.containers[container.name] = str(target.repo_root)
                state.pending.discard(container.name)
                state.stopping.pop(container.name, None)
            success(f"Stopped: {container.name}")
        except Exception as exc:
            error_plain(f"{container.name}: {exc}")
            ok = False
            continue
    return ok


def run_resume(
    cfg: Config,
    incus: Incus,
    *,
    post_start: Callable[[Config, Incus, str], None],
    clear_boot: Callable[[Config, str], None],
) -> bool:
    """Restore the saved set and repair autostart missing after Incus boots."""
    with operation():
        return _resume(cfg, incus, post_start=post_start, clear_boot=clear_boot)


def _resume(
    cfg: Config,
    incus: Incus,
    *,
    post_start: Callable[[Config, Incus, str], None],
    clear_boot: Callable[[Config, str], None],
) -> bool:
    import shlex

    from jailbee.config import load_repo_config
    from jailbee.remote_ssh.session import is_remote_session
    from jailbee.tmux import SESSION_NAME

    failures: dict[Path, str] = {}
    reported: set[Path] = set()
    configs = _configs(cfg, failures)
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    with Session(get_engine()) as session:
        roots = {
            r.container_prefix: Path(r.repo_root) for r in session.exec(select(RegisteredRepo))
        }
    containers = _session_containers(cfg, incus, configs)
    by_name = {c.name: c for c in containers}
    remote = is_remote_session()
    with _edit_set() as state:
        entries, pending = state.containers, state.pending
        # Remote callers must not prune excluded or orphan entries, even when
        # those instances disappeared. A local resume owns that housekeeping.
        for name in ((entries.keys() | state.stopping.keys()) - by_name.keys()) if not remote else ():
            info_plain(f"Dropping {name}: container no longer exists.")
            entries.pop(name, None)
            pending.discard(name)
            state.stopping.pop(name, None)
        # Recover intent left by a process that died after stopping an instance.
        for name, root in list(state.stopping.items()):
            if remote and name not in by_name:
                continue
            if by_name[name].state != "Running":
                entries.setdefault(name, root)
            del state.stopping[name]
    candidates = dict.fromkeys(name for name in entries if not remote or name in by_name)
    for running in containers:
        if running.state == "Running":
            candidates[running.name] = None
    ok = True
    attempted = False
    for name in candidates:
        try:
            container = by_name.get(name)
            if container is None:
                raise ValueError("recorded container no longer exists; resume set retained")
            target = configs.get(container.repo or "")
            repo_root = Path(entries[name]) if name in entries else roots.get(container.repo or "")
            if target is None and repo_root is not None:
                from jailbee.config import ConfigError

                if repo_root not in failures:
                    try:
                        target = load_repo_config(repo_root)
                    except ConfigError as exc:
                        failures[repo_root] = str(exc)
                if repo_root in failures:
                    with _edit_set() as current:
                        current.containers[name] = str(repo_root)
                    if repo_root not in reported:
                        error_plain(
                            f"Cannot load repository {repo_root}: {failures[repo_root]}. "
                            "Resume entries for existing containers retained; "
                            "restore the repository configuration and retry, or remove "
                            "unwanted containers with jailbee orphan destroy."
                        )
                        reported.add(repo_root)
                    ok = False
                    continue
                assert target is not None
                if target.container_prefix != container.repo:
                    raise ValueError("recorded repository no longer owns this container")
            if target is None:
                raise ValueError("repository configuration is unavailable")
            if _live_job(target, name):
                warn_plain(f"Skipping {name}: live background job; retry resume when it finishes.")
                ok = False
                continue
            if container.state == "Running" and name not in pending:
                from jailbee.boot_state import MARKER

                # The marker also covers empty plans and intentional skips.
                # Older boots have no marker: retain tmux as a fallback.
                present = incus.exec(
                    name,
                    [
                        "bash",
                        "-lc",
                        f"if test -f {MARKER}; then cat {MARKER}; "
                        f"elif tmux has-session -t {shlex.quote('=' + SESSION_NAME)} 2>/dev/null; "
                        "then printf present; else printf missing; fi",
                    ],
                    uid=target.container_user.uid,
                    timeout=15,
                ).strip()
                from jailbee.autostart import agent_autostart_steps
                from jailbee.autostart_plan import AutostartTrigger, plan_autostart
                from jailbee.autostart_profiles import profiled_config

                if present in ("present", "handled", "skipped"):
                    if name in entries:
                        remove(name)
                    continue
                profile = container.autostart_profile or target.autostart.default_profile
                effective = profiled_config(target, profile)
                plan = plan_autostart(
                    effective.autostart,
                    AutostartTrigger.ON_START,
                    agent_steps=agent_autostart_steps(effective),
                    services_stage=bool(effective.services),
                )
                if present == "missing" and not plan.blocking and not plan.detached:
                    if name in entries:
                        remove(name)
                    continue
                if present not in ("missing", "started"):
                    raise ValueError("cannot determine autostart session state")
            attempted = True
            # An incomplete run may create tmux before a later stage fails.
            # Keep that retry distinct from a healthy, already-running session.
            with _edit_set() as current:
                current.containers[name] = str(target.repo_root)
                current.pending.add(name)
            from jailbee.create_flow import Interaction, preflight_cache_pools
            from jailbee.lifecycle import boot_container

            preflight_cache_pools(target, confirm=Interaction.terminal().confirm)
            boot_container(target, incus, name, restart=False, ensure_running=True)
            post_start(target, incus, name)
            clear_boot(target, name)
            remove(name)
            success(f"Resumed: {name}")
        except Exception as exc:
            error_plain(f"{name}: {exc}")
            ok = False
            continue
    if not attempted and ok:
        info_plain("Nothing to resume.")
    return ok
