"""Per-container carve-outs inside a shared agent mount.

`agents.<name>.shared[].private` names subpaths of a shared directory that
must not be shared: an IPC socket, a pid file, a lock. Sharing any of those
breaks the container boundary — a pathname AF_UNIX socket is not confined by
a network namespace, and a PID means nothing across a PID namespace, so one
container's frontend can drive another container's daemon and have it edit
the wrong clone. See docs/agents.md.

Deliberately not `pool.py`: pooling exists to seed and recycle warm state
between containers, and a recycled slot would hand the next container a stale
socket. These directories start empty, never hold anything worth keeping, and
die with the container.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import IncusError
from jailbee.tui import warn

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.agents import PrivateSubpath
    from jailbee.config import Config
    from jailbee.incus import Incus


def private_root(cfg: Config, container: str) -> Path:
    """Host directory holding every private subpath of one container.

    Under `<shared_dir>/.private/`, not inside the agent's own shared
    directory: that keeps it out of every container's view of the mount, so no
    agent can list a sibling container's runtime state.
    """
    assert cfg.shared_dir is not None  # set by load_config
    return cfg.shared_dir / ".private" / container


def _subpaths(cfg: Config) -> list[PrivateSubpath]:
    from jailbee.agents import enabled_agent_specs

    return [p for spec in enabled_agent_specs(cfg) for p in spec.private]


def _container_path(private: PrivateSubpath) -> str:
    home = f"/home/{CONTAINER_USERNAME}"
    path = private.container_path
    return path.replace("~", home, 1) if path.startswith("~") else path


def attach(cfg: Config, incus: Incus, container: str) -> None:
    """Mount each private subpath over its place in the shared mount.

    Removes before adding, on every call. That is not defensive tidiness: the
    device must be hot-plugged into an already-mounted parent for the nested
    mount to land on top of the shared one. A device carried over from the
    previous boot mounts at `incus start`, in an order Incus picks — re-adding
    it against a running container makes the ordering ours.

    Call after `incus.start` has returned, on every path that boots a
    container.
    """
    root = private_root(cfg, container)
    for private in _subpaths(cfg):
        source = root / private.host_subpath
        source.mkdir(parents=True, exist_ok=True)
        incus.config_device_remove(container, private.name, missing_ok=True)
        incus.config_device_add(
            container,
            private.name,
            "disk",
            {"source": str(source), "path": _container_path(private)},
        )


def release(cfg: Config, incus: Incus, container: str) -> None:
    """Drop the devices and the host tree for a container being destroyed.

    Best-effort on the Incus side, like `pool.release_all`: a device left on an
    instance that is about to be deleted costs nothing, while raising here
    would leave a container the user asked to destroy undeletable. The host
    tree is removed either way — it is what would otherwise accumulate.
    """
    for private in _subpaths(cfg):
        try:
            incus.config_device_remove(container, private.name, missing_ok=True)
        except IncusError as e:
            warn(f"Could not remove device '{private.name}' from '{container}': {e}")
    shutil.rmtree(private_root(cfg, container), ignore_errors=True)
