"""The terminal dashboard's Outbox entry: the staged proposals of one container.

The same picker panels as Snapshots, not a second program: the proposals are
listed quietly with `jailbee outbox ls -o json`, a proposal offers Show,
Publish… and Delete…, and each change asks its own No-first question before a
real `jailbee outbox …` child runs it. Deleting a single action or comment
stays in `jailbee outbox browse`, which the last entry hands the terminal to.

Everything here is pure; `jailbee.dashboard` wires it into `run()`. Which
entries a proposal offers is decided by the caller from the remote-SSH policy
(`dashboard_commands.permitted`), as for snapshots.

Must not import `jailbee.dashboard`, which imports this module.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from jailbee.dashboard_overlays import Picker, PickerEntry

BROWSE = "browse"
SHOW = "show"
PUBLISH = "publish"
DELETE = "delete"

# Listed proposals carry this prefix in their picker value, so no proposal id
# can ever be read as the browse entry.
_PROPOSAL_VALUE = "proposal:"

# The states `jailbee outbox apply` still has something to publish in.
_PUBLISHABLE = frozenset({"pending", "partial", "awaiting-pr"})


@dataclass(frozen=True)
class ProposalRow:
    """One proposal of `jailbee outbox ls -o json`, as much as the menu shows."""

    id: str
    state: str
    revision: str
    actions: int
    error: str | None
    edit_block: str | None

    @property
    def publishable(self) -> bool:
        return self.error is None and self.state in _PUBLISHABLE

    @property
    def deletable(self) -> bool:
        """Whole-manifest deletion; an issue manifest with an edit block refuses it."""
        return self.edit_block is None


@dataclass(frozen=True)
class OutboxListing:
    """The proposals of one container, or why it could not be read."""

    rows: tuple[ProposalRow, ...]
    error: str | None
    warnings: tuple[str, ...]


class OutboxLoadError(Exception):
    """`jailbee outbox ls` printed something that is not an outbox listing."""


def _bad_listing() -> OutboxLoadError:
    return OutboxLoadError("unexpected output from 'jailbee outbox ls'")


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _proposal_row(item: object) -> ProposalRow:
    if not isinstance(item, dict):
        raise _bad_listing()
    pid, state, revision, actions = (
        item.get("id"),
        item.get("state"),
        item.get("revision"),
        item.get("actions"),
    )
    if not (
        isinstance(pid, str)
        and pid
        and isinstance(state, str)
        and isinstance(revision, str)
        and isinstance(actions, list)
    ):
        raise _bad_listing()
    return ProposalRow(
        pid,
        state,
        revision,
        len(actions),
        _opt_str(item.get("error")),
        _opt_str(item.get("edit_block")),
    )


def parse_outbox_listing(stdout: str, container: str) -> OutboxListing:
    """``container``'s entry of the listing; a missing entry is an empty outbox."""
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise _bad_listing() from exc
    if not isinstance(data, dict) or not isinstance(data.get("containers"), list):
        raise _bad_listing()
    for entry in data["containers"]:
        if not isinstance(entry, dict) or entry.get("name") != container:
            continue
        proposals = entry.get("proposals", [])
        stores = entry.get("stores", [])
        if not isinstance(proposals, list) or not isinstance(stores, list):
            raise _bad_listing()
        warnings = tuple(
            text
            for store in stores
            if isinstance(store, dict)
            for key in ("warnings", "rejected")
            for text in store.get(key) or ()
            if isinstance(text, str)
        )
        error = _opt_str(entry.get("error"))
        if entry.get("available") is False and error is None:
            error = "container unavailable"
        return OutboxListing(tuple(_proposal_row(p) for p in proposals), error, warnings)
    return OutboxListing((), None, ())


def outbox_ls_argv(name: str) -> list[str]:
    return ["outbox", "ls", name, "-o", "json"]


def outbox_browse_argv(name: str) -> list[str]:
    return ["outbox", "browse", name]


def outbox_show_argv(name: str, proposal: str) -> list[str]:
    """Shown through a pager, so `--color` keeps the bodies rendered across the pipe."""
    return ["outbox", "show", name, proposal, "--color"]


def outbox_apply_argv(name: str, proposal: str, revision: str) -> list[str]:
    """Confirmed in the dashboard; `--revision` refuses a manifest changed since it was listed."""
    return ["outbox", "apply", name, proposal, "--yes", "--revision", revision]


def outbox_drop_argv(name: str, proposal: str, revision: str) -> list[str]:
    """The whole manifest, confirmed in the dashboard and pinned to the listed revision."""
    return ["outbox", "drop", name, proposal, "--yes", "--revision", revision]


def proposal_value(pid: str) -> str:
    return _PROPOSAL_VALUE + pid


def proposal_id(value: str) -> str | None:
    """The proposal a picker value names, or None for the browse entry."""
    return value.removeprefix(_PROPOSAL_VALUE) if value.startswith(_PROPOSAL_VALUE) else None


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _proposal_label(row: ProposalRow) -> str:
    return f"{row.id}  {row.state}  ({row.error or _plural(row.actions, 'action')})"


def outbox_picker(container: str, rows: Sequence[ProposalRow], *, can_browse: bool) -> Picker:
    """The proposals in listing order, then the full browser (when permitted)."""
    listed = tuple(PickerEntry(_proposal_label(row), proposal_value(row.id)) for row in rows)
    browse = (PickerEntry("Browse actions & comments…", BROWSE),) if can_browse else ()
    return Picker("container-outbox", f"Outbox — {container}", (*listed, *browse), target=container)


def proposal_picker(
    container: str, row: ProposalRow, *, can_show: bool, can_publish: bool, can_delete: bool
) -> Picker:
    """What can be done to one proposal; ``carry`` pins it and the revision listed."""
    entries: list[PickerEntry] = []
    if can_show:
        entries.append(PickerEntry("Show", SHOW))
    if can_publish and row.publishable:
        entries.append(PickerEntry("Publish…", PUBLISH))
    if can_delete and row.deletable:
        entries.append(PickerEntry("Delete…", DELETE))
    return Picker(
        "container-outbox-proposal",
        f"{row.id} — {container}",
        tuple(entries),
        target=container,
        carry=(row.id, row.revision, str(row.actions)),
    )


def outbox_confirm_picker(
    container: str, action: str, pid: str, revision: str, actions: int
) -> Picker:
    """Confirm a publish or a delete. "No" comes first, so a stray Enter changes nothing."""
    if action == PUBLISH:
        question = f"Publish every pending action of {pid}?"
        yes = "Yes, publish"
    else:
        question = f"Delete {pid} ({_plural(actions, 'action')}) from {container}?"
        yes = "Yes, delete"
    return Picker(
        "container-outbox-confirm",
        question,
        (PickerEntry("No", "no"), PickerEntry(yes, "yes")),
        target=container,
        carry=(action, pid, revision),
    )
