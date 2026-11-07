"""Host-side serialization of a container's PR outbox publication."""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import local
from typing import cast

from jailbee.db import state_dir
from jailbee.outbox.models import OutboxChanged, OutboxExecutionError
from jailbee.outbox_io import ContainerIdentity, acquire_outbox_lock

# Inspection is interactive: a wedged daemon/helper must fail, not hang or look empty.
READ_TIMEOUT = 30


class PrManagement:
    """Instance-reentrant, host-side serialization of one container's PR store."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else state_dir() / "pr-outbox" / "locks"
        self._lock_state = local()

    @property
    def identity(self) -> ContainerIdentity | None:
        """The identity owned by this thread's outermost operation, if any."""
        return cast("ContainerIdentity | None", getattr(self._lock_state, "identity", None))

    @contextmanager
    def lock(self, identity: ContainerIdentity, *, timeout: float | None = None) -> Iterator[None]:
        """Serialize the PR store; opt-in finite seconds bound lock contention."""
        if not identity.full_name or not identity.created_at:
            raise OutboxExecutionError("cannot lock an empty container identity")
        if self.identity is not None and self.identity != identity:
            raise OutboxChanged("container changed during publication; refresh required")
        digest = hashlib.sha256(f"{identity.full_name}\0{identity.created_at}".encode()).hexdigest()
        path = self.root / f"{digest}.lock"
        held = getattr(self._lock_state, "paths", None)
        if held is None:
            held = set()
            self._lock_state.paths = held
        if path in held:
            yield
            return
        descriptor = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.chmod(path, 0o600)
            acquire_outbox_lock(descriptor, timeout)
        except (OSError, ValueError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            if isinstance(exc, TimeoutError):
                raise OutboxExecutionError(
                    "Timed out waiting for PR outbox lock; "
                    "refresh and retry after the current operation finishes"
                ) from exc
            if isinstance(exc, ValueError):
                raise OutboxExecutionError(str(exc)) from exc
            raise OutboxExecutionError("could not lock PR outbox") from exc
        held.add(path)
        self._lock_state.identity = identity
        try:
            yield
        finally:
            held.remove(path)
            self._lock_state.identity = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
