"""Store-only broker delivery and the permanent host pending spool."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from jailbee.incus import IncusError
from jailbee.orchestration.protocol import FINISH_SUFFIX, PendingFinish, inbox_file_name
from jailbee.orchestration.state import atomic_write_text, pending_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jailbee.incus import Incus
    from jailbee.orchestration.grant import Grant
    from jailbee.orchestration.protocol import InboxItem, InboxMessage
    from jailbee.orchestration.runner_model import DefaultRunnerSource, Runner

CONTAINER_JAILBEE = "/usr/local/bin/jailbee"
MISSING_JAILBEE_MESSAGE = (
    "container image has no in-container jailbee (needed for `jailbee inbox`) — "
    "run `jb base build` on the host, then recreate the container"
)


class ContainerJailbeeMissing(IncusError):  # noqa: N818 - public task interface
    """The container image lacks the inbox-capable launcher."""


def has_container_jailbee(incus: Incus, full: str) -> bool:
    """Check whether the in-container launcher is executable."""
    try:
        incus.exec(full, ["test", "-x", CONTAINER_JAILBEE])
    except IncusError:
        return False
    return True


def ensure_orchestration_env(incus: Incus, full: str, *, uid: int | None) -> None:
    """Require the inbox-capable launcher; container transport needs no directories."""
    if not has_container_jailbee(incus, full):
        raise ContainerJailbeeMissing(MISSING_JAILBEE_MESSAGE)


def _item_json(item: InboxItem) -> str:
    return item.model_dump_json(by_alias=True) + "\n"


def _insert_into_store(
    root: Path, full: str, item: InboxItem | dict[str, object], *, recipient_uuid: str | None = None
) -> bool:
    """Deliver to a verified instance, independently of its socket registration.

    The caller takes the uuid from its current inventory, never from a namesake's
    socket recipient row. No identity means keep the item in the host spool.
    """
    from jailbee.orchestration import store

    if not recipient_uuid:
        return False
    data = item if isinstance(item, dict) else json.loads(item.model_dump_json(by_alias=True))
    store.BrokerStore(store.store_path(root)).insert_item(
        recipient_uuid, full, data, now=time.time()
    )
    return True  # An already-inserted id is delivered too (crash-safe retry).


def deliver(
    incus: Incus,
    root: Path,
    recipient_full: str,
    item: InboxItem,
    *,
    running: bool,
    uid: int | None,
    now: datetime,
    seq: int = 0,
    recipient_uuid: str | None = None,
) -> Literal["delivered", "parked"]:
    """Insert into the store, or park behind older items/on missing identity or failure."""
    from jailbee.orchestration.store import StoreError

    try:
        if not has_pending(root, recipient_full) and _insert_into_store(
            root, recipient_full, item, recipient_uuid=recipient_uuid
        ):
            return "delivered"
    except StoreError:
        logging.getLogger(__name__).exception("broker store unavailable; parking item")
    atomic_write_text(
        pending_dir(root, recipient_full) / inbox_file_name(item, now, seq=seq), _item_json(item)
    )
    return "parked"


def _deliverable(path: Path) -> bool:
    """A parked file that still waits for delivery (not a temp or quarantined one)."""
    return path.is_file() and not path.name.startswith(".") and not path.name.endswith(".bad")


def has_pending(root: Path, recipient_full: str) -> bool:
    """Whether any parked file (not a dot or `.bad` one) waits for `recipient_full`."""
    directory = pending_dir(root, recipient_full)
    if not directory.is_dir():
        return False
    return any(_deliverable(p) for p in directory.iterdir())


def _note_quarantined(
    incus: Incus, root: Path, recipient_full: str, bad_name: str, *, recipient_uuid: str | None
) -> None:
    """Tell the orchestrator a queued done report was set aside, not lost silently."""
    from jailbee.orchestration.protocol import InboxMessage, new_host_id

    note = InboxMessage.model_validate(
        {
            "version": 1,
            "id": new_host_id(),
            "type": "message",
            "from": "broker",
            "kind": "note",
            "body": (
                "a queued done report could not be read and was set aside as "
                f"{bad_name} in the broker state dir; the child's work was not reported"
            ),
        }
    )
    name = inbox_file_name(note, datetime.now(UTC))
    from jailbee.orchestration.store import StoreError

    try:
        if _insert_into_store(root, recipient_full, note, recipient_uuid=recipient_uuid):
            return
    except StoreError:
        logging.getLogger(__name__).exception("broker store unavailable; parking quarantine note")
    atomic_write_text(pending_dir(root, recipient_full) / name, _item_json(note))


def flush_pending(
    incus: Incus,
    root: Path,
    recipient_full: str,
    *,
    uid: int | None,
    finish: Callable[[PendingFinish], InboxMessage] | None = None,
    recipient_uuid: str | None = None,
) -> int:
    """Deliver parked files in name order; stop at the first failure.

    Files live only on the host and are unlinked only after a successful store
    insert. Socket registration and container running state are irrelevant.
    """
    from jailbee.orchestration.store import StoreError

    directory = pending_dir(root, recipient_full)
    if not directory.is_dir():
        return 0
    if not recipient_uuid:
        return 0
    delivered = 0
    for path in sorted(p for p in directory.iterdir() if _deliverable(p)):
        name = path.name
        text = path.read_text(encoding="utf-8")
        if name.endswith(FINISH_SUFFIX):
            try:
                item = PendingFinish.model_validate_json(text)
            except ValidationError:
                logging.getLogger(__name__).exception("invalid pending finish %s", path)
                path.rename(path.with_name(path.name + ".bad"))
                _note_quarantined(
                    incus, root, recipient_full, path.name + ".bad", recipient_uuid=recipient_uuid
                )
                continue
            if finish is None:
                break
            text = _item_json(finish(item))
        try:
            loaded = json.loads(text)
            assert isinstance(loaded, dict) and isinstance(loaded.get("id"), str)
        except (ValueError, AssertionError):
            logging.getLogger(__name__).error("invalid pending item %s", path)
            path.rename(path.with_name(path.name + ".bad"))
            _note_quarantined(
                incus, root, recipient_full, path.name + ".bad", recipient_uuid=recipient_uuid
            )
            continue
        try:
            inserted = _insert_into_store(
                root, recipient_full, loaded, recipient_uuid=recipient_uuid
            )
        except StoreError:
            logging.getLogger(__name__).exception("broker store insert failed; keeping files")
            break
        if not inserted:
            break
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        delivered += 1
    return delivered


def grant_json_text(grant: Grant, runner: Runner | None, source: DefaultRunnerSource | None) -> str:
    data = json.loads(grant.to_label())
    data["default_runner"] = runner.model_dump(mode="json", exclude_none=True) if runner else None
    data["default_runner_source"] = source
    return json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"
