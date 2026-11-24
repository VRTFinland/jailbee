"""Host-wide clean shutdown and restoration after a host reboot."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.tui import error_plain, info_plain, success, warn_plain

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def _state_path() -> Path:
    from jailbee.db import state_dir

    return state_dir() / "resume-set.json"


def _read_set() -> tuple[dict[str, str], set[str]]:
    path = _state_path()
    if not path.exists():
        return {}, set()
    try:
        data = json.loads(path.read_text())
        entries = data["containers"]
        if data["version"] != 1 or not isinstance(entries, dict) or not all(
            isinstance(name, str) and isinstance(root, str) for name, root in entries.items()
        ):
            raise ValueError("unsupported resume set")
        pending = data.get("pending", [])
        if not isinstance(pending, list) or not all(isinstance(name, str) and name in entries for name in pending):
            raise ValueError("invalid pending resume entries")
        return dict(entries), set(pending)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc


def _write_set(entries: dict[str, str], pending: set[str]) -> None:
    path = _state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".resume-set-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "containers": entries, "pending": sorted(pending)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _configs(cfg: Config) -> dict[str, Config]:
    from jailbee.config import ConfigError, load_repo_config
    from jailbee.dashboard.model import registered_repo_roots

    configs = {cfg.container_prefix: cfg}
    for root in registered_repo_roots():
        if root == cfg.repo_root:
            continue
        try:
            other = load_repo_config(root)
        except ConfigError as exc:
            warn_plain(f"Cannot load repository {root}: {exc}")
            continue
        configs[other.container_prefix] = other
    return configs


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
    from jailbee import prompting
    from jailbee.lifecycle import list_containers

    containers = [c for c in list_containers(cfg, incus, all_repos=True) if c.state == "Running"]
    if not containers:
        info_plain("No running JailBee containers to stop.")
        return True
    detail = "Running JailBee containers across all repositories:\n" + "\n".join(
        f"  {c.name}" for c in containers
    )
    info_plain(detail)
    if not yes and not prompting.ask_confirm(
        "Stop all listed containers?", destructive=True, alternative="--yes", detail=detail
    ):
        return True
    entries, pending = _read_set()
    # Check persistence before stopping anything; preserve earlier batches.
    _write_set(entries, pending)
    configs = _configs(cfg)
    ok = True
    for container in containers:
        try:
            target = configs.get(container.repo or "")
            if target is None:
                raise ValueError("repository configuration is unavailable")
            if _live_job(target, container.name):
                warn_plain(f"Skipping {container.name}: live background job.")
                continue
            stop(target, incus, container.name)
        except Exception as exc:
            error_plain(f"{container.name}: {exc}")
            ok = False
            continue
        entries[container.name] = str(target.repo_root)
        pending.discard(container.name)
        _write_set(entries, pending)
        success(f"Stopped: {container.name}")
    return ok


def run_resume(
    cfg: Config,
    incus: Incus,
    *,
    start: Callable[[Config, Incus, str], None],
) -> bool:
    """Restore the saved set and repair autostart missing after Incus boots."""
    import shlex

    from jailbee.config import load_repo_config
    from jailbee.lifecycle import list_containers
    from jailbee.tmux import SESSION_NAME

    entries, pending = _read_set()
    configs = _configs(cfg)
    containers = list_containers(cfg, incus, all_repos=True)
    by_name = {c.name: c for c in containers}
    candidates = dict.fromkeys(entries)
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
            if target is None and name in entries:
                target = load_repo_config(Path(entries[name]))
                if target.container_prefix != container.repo:
                    raise ValueError("recorded repository no longer owns this container")
            if target is None:
                raise ValueError("repository configuration is unavailable")
            if _live_job(target, name):
                warn_plain(f"Skipping {name}: live background job; retry resume when it finishes.")
                ok = False
                continue
            if container.state == "Running" and name not in pending:
                # Probe as the same user that owns autostart's tmux server.
                present = incus.exec(
                    name,
                    [
                        "bash", "-lc",
                        f"if tmux has-session -t {shlex.quote('=' + SESSION_NAME)} 2>/dev/null; "
                        "then printf present; else printf missing; fi",
                    ],
                    uid=target.container_user.uid,
                    timeout=15,
                ).strip()
                if present == "present":
                    if name in entries:
                        del entries[name]
                        _write_set(entries, pending)
                    continue
                if present != "missing":
                    raise ValueError("cannot determine autostart session state")
            attempted = True
            # An incomplete run may create tmux before a later stage fails.
            # Keep that retry distinct from a healthy, already-running session.
            entries[name] = str(target.repo_root)
            pending.add(name)
            _write_set(entries, pending)
            start(target, incus, name)
        except Exception as exc:
            error_plain(f"{name}: {exc}")
            ok = False
            continue
        entries.pop(name, None)
        pending.discard(name)
        _write_set(entries, pending)
        success(f"Resumed: {name}")
    if not attempted and ok:
        info_plain("Nothing to resume.")
    return ok
