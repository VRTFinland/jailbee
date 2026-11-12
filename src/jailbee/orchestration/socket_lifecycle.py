"""Lifecycle management for orchestration broker sockets."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.incus import ORCHESTRATION_DEVICE, IncusError
from jailbee.orchestration import broker_state, labels, socket_device
from jailbee.orchestration.outbox import ensure_orchestration_env
from jailbee.orchestration.store import StoreError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.actions import ActionContext, Node

log = logging.getLogger(__name__)


def ensure_env(ctx: ActionContext, memory: broker_state.BrokerMemory) -> None:
    """Ensure each served container once per broker process; retry failures.

    Fork children copy an ensured orchestrator; new children use the current
    golden image.
    Cache by instance identity so a recreated container is ensured again.
    """
    full, key = ctx.sender.full, ctx.sender.key
    from jailbee.container_locks import locked as container_locked

    try:
        with container_locked(full):
            # The tick's nodes may predate a release. Recheck even on a cache hit.
            parent = ctx.incus.config_get_checked(full, labels.SPAWNED_BY)
            grant = ctx.incus.config_get_checked(full, labels.GRANT)
            role = "child" if parent else "orchestrator" if grant else "plain"
            if role == "plain":
                # The labels went away: no orchestration inbox any more, but
                # every container keeps its broker socket.
                memory.env_ensured.discard(key)
                memory.env_roles.pop(key, None)
                _ensure_socket(ctx, memory)
                return
            if key in memory.env_ensured and memory.env_roles.get(key) == role:
                _ensure_socket(ctx, memory)
                return
            ensure_orchestration_env(ctx.incus, full, uid=ctx.cfg.container_user.uid)
            if role == "child":
                labels.write_child_role(ctx.incus, full, uid=ctx.cfg.container_user.uid)
            memory.env_ensured.add(key)
            memory.env_roles[key] = role
            _ensure_socket(ctx, memory)
    except (IncusError, OSError) as exc:
        memory.env_ensured.discard(key)
        memory.env_roles.pop(key, None)
        memory.warn_once(
            f"env:{full}",
            "broker: %s: could not prepare the orchestration inbox: %s",
            full,
            exc,
        )


def _ensure_socket(ctx: ActionContext, memory: broker_state.BrokerMemory) -> None:
    ensure_socket(ctx.incus, ctx.root, memory, ctx.sender)


def serve_plain(
    incus: Incus,
    raw: list[dict[str, Any]],
    memory: broker_state.BrokerMemory,
    root: Path,
    configs: Mapping[str, Config | None],
) -> None:
    """Give every running container of a registered repo without an orchestration
    label its broker socket; labelled ones are served by ``manifests.serve``."""
    from jailbee.orchestration.actions import Node

    for entry in raw:
        name, config = entry.get("name"), entry.get("config")
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        if entry.get("status") != "Running" or broker_state.labelled(config):
            continue
        if not broker_state.is_managed(name, configs):
            continue
        node = Node(
            full=name,
            short=name,
            running=True,
            config={str(k): str(v) for k, v in config.items()},
        )
        ensure_socket(incus, root, memory, node)


def ensure_socket(
    incus: Incus, root: Path, memory: broker_state.BrokerMemory, sender: Node
) -> None:
    """Open the listener, attach the device and record the recipient (idempotent)."""
    listener, store = memory.listener, memory.store
    uuid = sender.config.get("volatile.uuid")
    if listener is None or store is None or not uuid:
        return
    full = sender.full
    if uuid in memory.shared_uuids:
        return  # check_socket_devices warned and tore down
    if listener.has_container(uuid) and memory.attached.get(full) == uuid:
        return
    try:
        if not listener.has_container(uuid):
            recorded = incus.config_device_get(full, ORCHESTRATION_DEVICE, "source")
            directory_key = str(Path(recorded).resolve()) if recorded else ""
            reused = (
                socket_device.reusable_dir(root, uuid, recorded)
                if (
                    directory_key in memory.reusable_socket_dirs
                    and directory_key not in memory.unsafe_socket_dirs
                    and not store.kv_get(f"socket-retired:{directory_key}")
                )
                else None
            )
            if reused is None and recorded and socket_device.dir_uuid(recorded) == uuid:
                # Persist retirement before cleanup: an invisible or lazily
                # detached copy may hold it across another broker restart.
                store.kv_put(f"socket-retired:{directory_key}", True)
                with contextlib.suppress(OSError):
                    socket_device.remove_dir(root, recorded)
            sdir = reused or socket_device.fresh_sock_dir(root, uuid)
            existed = sdir.exists()
            try:
                created = listener.open_container(uuid, sdir)
            except OSError:
                # The directory may be ours although the bind failed.
                if not existed and sdir.exists():
                    memory.reassert.add(uuid)
                raise
            if created:
                memory.reassert.add(uuid)
        live = listener.socket_dir(uuid)
        if live is None:
            raise OSError(f"no socket directory for {uuid}")
        socket_device.attach(incus, full, live, reassert=uuid in memory.reassert)
        memory.reassert.discard(uuid)
        store.set_recipient(full, uuid, now=time.time())
        memory.attached[full] = uuid
    except (IncusError, OSError, StoreError) as exc:
        memory.attached.pop(full, None)
        memory.warn_once(
            f"socket:{full}", "broker: %s: could not set up the broker socket: %s", full, exc
        )


# How long a socket directory nobody serves yet keeps the broker running rounds
# early: `jb new` attaches the device of a new container (`socket_device.
# attach_new`) and only a round binds the listener in it.
NEW_DIR_GRACE_SECONDS = 120.0


def unserved_new_dir(root: Path, memory: broker_state.BrokerMemory, *, now: float) -> bool:
    """Whether a socket directory younger than `NEW_DIR_GRACE_SECONDS` is not served.

    The broker then runs its next round early instead of after the idle interval,
    so a container staging right after `jb new` reaches a listener within the
    client's connect retry window. A directory that stays unserved (say, its
    container is not started) stops counting once it is older than the grace.
    """
    listener = memory.listener
    if listener is None:
        return False
    try:
        entries = list(os.scandir(root / "sock"))
    except OSError:
        return False
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            age = now - entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if age > NEW_DIR_GRACE_SECONDS:
            continue
        if listener.socket_dir(socket_device.dir_uuid(entry.path)) != Path(entry.path):
            return True
    return False


def teardown_socket(
    incus: Incus, memory: broker_state.BrokerMemory, full: str, uuid: str | None, root: Path
) -> None:
    """Remove socket access; unread items stay in the durable store."""
    listener, store = memory.listener, memory.store
    if listener is None or store is None:
        return
    try:
        uuid = uuid or memory.attached.get(full) or store.recipient_uuid(full)
        if uuid:
            # Closed before the detach, so a failed detach still fails closed.
            listener.close_container(uuid)
        socket_device.detach(incus, full)
        store.remove_recipient(full)
        memory.attached.pop(full, None)
        memory.reassert.discard(uuid or "")
    except (IncusError, OSError, StoreError) as exc:
        memory.warn_once(
            f"socket-off:{full}", "broker: %s: could not remove the broker socket: %s", full, exc
        )


def _forget_vanished(raw: list[dict[str, Any]], memory: broker_state.BrokerMemory) -> None:
    """Close listeners and drop state of containers that are gone or recreated.

    No device call: a vanished container has no device to remove, and a
    recreated one is set up (under its new uuid) when it is next served.
    """
    listener, store = memory.listener, memory.store
    if listener is None or store is None:
        return
    live: dict[str, str | None] = {}
    for entry in raw:
        name, config = entry.get("name"), entry.get("config")
        if isinstance(name, str) and isinstance(config, dict):
            uuid = config.get("volatile.uuid")
            live[name] = uuid if isinstance(uuid, str) and uuid else None
    try:
        recipients = store.recipients()
    except StoreError as exc:
        memory.warn_once("socket-recipients", "broker: cannot read the recipients: %s", exc)
        recipients = {}
    for full in set(memory.attached) | set(recipients):
        current = live.get(full, "")  # "" -> the name is gone
        if current is None:
            continue  # present but without a uuid: nothing to compare
        known = {u for u in (memory.attached.get(full), recipients.get(full)) if u}
        stale = known if current == "" else {u for u in known if u != current}
        if not stale:
            continue
        try:
            for uuid in stale:
                listener.close_container(uuid)
                memory.reassert.discard(uuid)
            memory.attached.pop(full, None)
            if recipients.get(full) in stale:
                store.remove_recipient(full)
        except (OSError, StoreError) as exc:
            memory.warn_once(f"socket-gone:{full}", "broker: %s: %s", full, exc)


def _shared_uuids(raw: list[dict[str, Any]]) -> set[str]:
    seen: set[str] = set()
    shared: set[str] = set()
    for entry in raw:
        config = entry.get("config")
        uuid = config.get("volatile.uuid") if isinstance(config, dict) else None
        if isinstance(uuid, str) and uuid:
            (shared if uuid in seen else seen).add(uuid)
    return shared


def _rotate_socket(
    incus: Incus, memory: broker_state.BrokerMemory, root: Path, uuid: str, copy: str
) -> None:
    """Move ``uuid``'s socket away from the directory ``copy`` mounts.

    Every connection drops and the old directory is removed, so the copy's
    mount (stale or lazily detached) leads nowhere; the owner's device is
    re-attached to the fresh directory. When the move fails the socket is
    closed instead (fail closed): the owner is set up afresh next round.
    """
    listener = memory.listener
    assert listener is not None
    owner = next((full for full, u in memory.attached.items() if u == uuid), None)
    fresh = socket_device.fresh_sock_dir(root, uuid)
    try:
        listener.rotate_container(uuid, fresh)
    except OSError as exc:
        memory.warn_once(f"socket-rotate:{uuid}", "broker: %s: %s", copy, exc)
        with contextlib.suppress(OSError):
            listener.close_container(uuid)
        if owner is not None:
            memory.attached.pop(owner, None)
        return
    log.warning("broker: %s mounted the socket of %s; moved it to %s", copy, uuid, fresh)
    if owner is None:
        return
    try:
        socket_device.attach(incus, owner, fresh, reassert=True)
    except (IncusError, OSError) as exc:
        memory.attached.pop(owner, None)  # _ensure_socket retries
        memory.warn_once(f"socket:{owner}", "broker: %s: %s", owner, exc)


def _strip_copy(
    incus: Incus,
    memory: broker_state.BrokerMemory,
    root: Path,
    entry: dict[str, Any],
    name: str,
    project: str | None,
) -> None:
    """Remove a foreign socket device; move the socket if it named the live directory."""
    where = name if project is None else f"{project}/{name}"
    source = socket_device.source_uuid(entry)
    path = socket_device.source(entry)
    listener, store = memory.listener, memory.store
    assert listener is not None and store is not None
    if source and path:
        path = str(Path(path).resolve())
        source = socket_device.dir_uuid(path)
        memory.unsafe_socket_dirs.add(path)
        try:
            # Keep the foreign device visible until its directory is durably
            # retired, even when removal or a later rotation fails.
            store.kv_put(f"socket-retired:{path}", True)
        except StoreError as exc:
            memory.warn_once(f"socket-retire:{path}", "broker: %s: %s", path, exc)
            with contextlib.suppress(OSError):
                listener.close_container(source)
            return
    log.warning("broker: %s carries another instance's socket device; removing", where)
    try:
        socket_device.detach(incus, name, project=project)
    except (IncusError, OSError) as exc:
        memory.warn_once(f"socket-off:{where}", "broker: %s: %s", where, exc)
    # Whether or not the detach worked (it may fail, or be lazy while the
    # copy holds the directory), a copy that mounts the source's live
    # directory moves the source to a fresh one.
    if not source or not path:
        return
    if str(listener.socket_dir(source)) == path:
        _rotate_socket(incus, memory, root, source, where)
        return
    # Not served right now (e.g. the first round after a broker restart): the
    # source must never be served from this directory again.
    try:
        if socket_device.remove_dir(root, path):
            log.warning("broker: removed the socket directory %s a copy mounted", path)
    except OSError as exc:
        memory.warn_once(f"socket-dir:{path}", "broker: %s: %s", path, exc)


def other_projects(incus: Incus, memory: broker_state.BrokerMemory) -> list[dict[str, Any]] | None:
    """The instances of every other Incus project, or None when they cannot be listed.

    A raw `incus copy --target-project` (or an import there) is invisible to
    the plain listing, yet may mount a served socket.
    """
    memory.reusable_socket_dirs.clear()
    try:
        current = incus.current_project()
        everything = incus.list_containers(fast=True, all_projects=True)
    except (IncusError, OSError, ValueError) as exc:
        memory.warn_once("socket-projects", "broker: cannot list all Incus projects: %s", exc)
        return None
    if not isinstance(current, str) or not current:
        return None
    claims: dict[str, list[dict[str, Any]]] = {}
    for entry in everything:
        path = socket_device.source(entry)
        if path:
            claims.setdefault(str(Path(path).resolve()), []).append(entry)
    memory.reusable_socket_dirs.update(
        path
        for path, entries in claims.items()
        if len(entries) == 1
        and entries[0].get("project") == current
        and not socket_device.mismatched(entries[0])
    )
    return [
        e for e in everything if isinstance(e.get("project"), str) and e.get("project") != current
    ]


def check_socket_devices(
    incus: Incus,
    raw: list[dict[str, Any]],
    memory: broker_state.BrokerMemory,
    root: Path,
    others: list[dict[str, Any]] | None = None,
    managed: Callable[[str], bool] | None = None,
) -> None:
    """Strip foreign devices, tear down unmanaged sockets and notice vanished devices.

    ``managed`` says which unlabelled containers are plain JailBee containers,
    which keep their socket; None (unknown) tears nothing of theirs down.

    ``others`` (other projects' instances) are only checked for copies and
    twins of served containers; nothing else of theirs is touched.
    """
    if memory.listener is None or memory.store is None:
        return
    others = others or []
    _forget_vanished(raw, memory)
    memory.shared_uuids = _shared_uuids(raw + others)
    for entry in others:
        name, config = entry.get("name"), entry.get("config")
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        if not socket_device.has_device(entry):
            continue
        if socket_device.mismatched(entry) or config.get("volatile.uuid") in memory.shared_uuids:
            _strip_copy(incus, memory, root, entry, name, str(entry["project"]))
    for entry in raw:
        name = entry.get("name")
        config = entry.get("config")
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        uuid = config.get("volatile.uuid")
        if uuid in memory.shared_uuids:
            # The socket identifies a caller by uuid only: two instances with
            # one uuid (an `incus import` beside the original) would speak as
            # each other, so neither gets the socket.
            memory.warn_once(
                f"socket-shared:{name}",
                "broker: %s and another instance share the instance uuid %s; "
                "no broker socket for either",
                name,
                uuid,
            )
            if name in memory.attached or socket_device.has_device(entry):
                # Close only the recorded socket, never a twin listed first.
                teardown_socket(incus, memory, name, None, root)
            continue
        if socket_device.mismatched(entry):
            _strip_copy(incus, memory, root, entry, name, None)
            memory.attached.pop(name, None)
            continue
        plain = not broker_state.labelled(config)
        if plain and managed is None:
            continue  # registry unknown: never tear a plain container down
        if not plain or (managed is not None and managed(name)):
            # A snapshot restore can rewind the source; an upgrade can move
            # the mount point. Invalidate ensure_socket's cache using this
            # round's listing, without another per-container call.
            if name in memory.attached and (
                socket_device.source(entry)
                != str(memory.listener.socket_dir(memory.attached[name]))
                or socket_device.mount_path(entry) != socket_device.CONTAINER_PATH
            ):
                memory.attached.pop(name, None)
        elif name in memory.attached or socket_device.has_device(entry):
            teardown_socket(incus, memory, name, config.get("volatile.uuid"), root)
