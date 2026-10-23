"""The terminal dashboard's Outbox entry: the staged proposals of one container.

The same picker panels as Snapshots, not a second program: the proposals are
listed quietly with `jailbee outbox ls -o json`, a proposal offers Show,
Publish… and Delete…, and each change asks its own No-first question before a
real `jailbee outbox …` child runs it. Deleting a single action or comment
stays in `jailbee outbox browse`, which the last entry hands the terminal to.

Everything here is pure; `jailbee.dashboard.tui.session` wires it into `DashboardSession`. Which
entries a proposal offers is decided by the caller from the remote-SSH policy
(`dashboard.commands.permitted`), as for snapshots.

The second half of the module (from "Outbox manager") is the pure state of the
Outbox manager that replaces the pickers; the picker-era names above it are
kept, with `legacy_` names where the new API reuses a name, until their last
caller is gone.

Must not import `jailbee.dashboard.tui`, which imports this module.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from rich.text import Text

from jailbee.dashboard.overlays import Picker, PickerEntry

BROWSE = "browse"
SHOW = "show"
PUBLISH = "publish"
CREATE_PR = "create-pr"
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
    create_scope: dict[str, str] | None = None
    # `github:` targets of an issue proposal; the host plan decides whether one is
    # really foreign (a slug may name this repo), so this only marks them.
    foreign_repos: tuple[str, ...] = ()

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
        _scope(item.get("create_scope")),
        _foreign_repos(actions),
    )


def _foreign_repos(actions: list[object]) -> tuple[str, ...]:
    from jailbee.issue_manifest import foreign_slug

    slugs: dict[str, str] = {}
    for action in actions:
        repo = action.get("repo") if isinstance(action, dict) else None
        slug = foreign_slug(repo) if isinstance(repo, str) else None
        if slug is not None:
            slugs.setdefault(slug.casefold(), slug)
    return tuple(sorted(slugs.values(), key=str.casefold))


def _scope(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    if value == {"kind": "repo"}:
        return {"kind": "repo"}
    if set(value) == {"kind", "path"} and value.get("kind") == "submodule":
        path = value.get("path")
        if isinstance(path, str) and path and path not in (".", ".."):
            return {"kind": "submodule", "path": path}
    return None


def legacy_parse_outbox_listing(stdout: str, container: str) -> OutboxListing:
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


def legacy_outbox_ls_argv(name: str) -> list[str]:
    return ["outbox", "ls", name, "-o", "json"]


def outbox_browse_argv(name: str) -> list[str]:
    return ["outbox", "browse", name]


def outbox_show_argv(name: str, proposal: str) -> list[str]:
    """Shown through a pager, so `--color` keeps the bodies rendered across the pipe."""
    return ["outbox", "show", name, proposal, "--color"]


def create_pr_argv(name: str, scope: dict[str, str] | None) -> list[str] | None:
    if scope == {"kind": "repo"}:
        return ["pr", name]
    if scope is not None and scope.get("kind") == "submodule" and scope.get("path"):
        return ["submodule", "pr", name, scope["path"]]
    return None


def legacy_outbox_apply_argv(name: str, proposal: str, revision: str) -> list[str]:
    """Ask under the plan (including foreign-PR warnings), pinned to the listed revision."""
    return ["outbox", "apply", name, proposal, "--revision", revision]


def legacy_outbox_drop_argv(name: str, proposal: str, revision: str) -> list[str]:
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
    foreign = f", ⚠ foreign: {', '.join(row.foreign_repos)}" if row.foreign_repos else ""
    return f"{row.id}  {row.state}  ({row.error or _plural(row.actions, 'action')}{foreign})"


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
    if (
        can_publish
        and row.publishable
        and row.state == "awaiting-pr"
        and row.create_scope is not None
    ):
        entries.append(PickerEntry("Create PR…", CREATE_PR))
    elif can_publish and row.publishable and row.state != "awaiting-pr":
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


def outbox_delete_confirm_picker(container: str, pid: str, revision: str, actions: int) -> Picker:
    """Confirm a delete. "No" comes first, so a stray Enter changes nothing.

    A publish has no dashboard question: `outbox apply` asks once, under its plan.
    """
    return Picker(
        "container-outbox-confirm",
        f"Delete {pid} ({_plural(actions, 'action')}) from {container}?",
        (PickerEntry("No", "no"), PickerEntry("Yes, delete", "yes")),
        target=container,
        carry=(pid, revision),
    )


# ---------------------------------------------------------------------------
# Outbox manager: pure state, rows, preview, hint, argv
# ---------------------------------------------------------------------------

RowKey = tuple[str, str, int | None, int | None]  # (container, proposal id, action, comment)

_PUBLISHABLE_STATES = frozenset({"pending", "partial"})


@dataclass(frozen=True)
class CommentItem:
    index: int
    label: str
    text: str
    path: str | None
    line: int | None


@dataclass(frozen=True)
class ActionItem:
    index: int
    kind: str
    repo: str
    target: str
    title: str | None
    body: str | None
    markdown: bool
    state: str
    receipt: str | None
    reply_to: int | None
    delete_scope: tuple[int, ...] | None
    delete_block: str | None
    comments: tuple[CommentItem, ...]


@dataclass(frozen=True)
class ProposalItem:
    id: str
    kind: str
    state: str
    revision: str
    error: str | None
    edit_block: str | None
    create_scope: dict[str, str] | None
    draftable: bool
    deletable: bool
    delete_block: str | None
    delete_archive: bool
    foreign_repos: tuple[str, ...]
    raw_text: str
    actions: tuple[ActionItem, ...]

    @property
    def publishable(self) -> bool:
        return self.error is None and self.state in _PUBLISHABLE_STATES

    @property
    def creatable(self) -> bool:
        return self.state == "awaiting-pr" and self.create_scope is not None


@dataclass(frozen=True)
class ContainerListing:
    name: str
    error: str | None
    warnings: tuple[str, ...]
    proposals: tuple[ProposalItem, ...]


@dataclass(frozen=True)
class Question:
    purpose: str
    title: str
    lines: tuple[str, ...]
    choices: tuple[tuple[str, str], ...]
    default: str
    carry: tuple[str, ...] = ()


@dataclass(frozen=True)
class Abilities:
    delete: bool
    publish: bool
    publish_hand_off: bool
    create_pr: bool
    clear: bool
    scope: bool


@dataclass(frozen=True)
class OutboxState:
    title: str
    containers: tuple[ContainerListing, ...]
    expanded: frozenset[RowKey] = frozenset()
    marks: frozenset[RowKey] = frozenset()
    cursor: RowKey | None = None
    preview: Literal["auto", "on", "off"] = "auto"
    raw: bool = False
    question: Question | None = None
    busy: str | None = None
    status: tuple[str, ...] = ()
    abilities: Abilities = Abilities(False, False, False, False, False, False)
    all_repos: bool = False
    prefix: str = ""  # dashboard: the container's repo prefix; standalone: ""
    scope_name: str | None = None  # dashboard: the one container; standalone: None


@dataclass(frozen=True)
class Row:
    key: RowKey | None
    depth: int
    text: Text
    markable: bool
    role: Literal["header", "proposal", "action", "comment", "note"]


def _str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _comment_item(item: object) -> CommentItem:
    if not isinstance(item, dict) or _opt_int(item.get("index")) is None:
        raise _bad_listing()
    return CommentItem(
        int(item["index"]),
        _str(item.get("label")),
        _str(item.get("text")),
        _opt_str(item.get("path")),
        _opt_int(item.get("line")),
    )


def _action_item(item: object) -> ActionItem:
    if not isinstance(item, dict) or _opt_int(item.get("index")) is None:
        raise _bad_listing()
    scope = item.get("delete_scope")
    delete_scope = (
        tuple(i for i in scope if isinstance(i, int)) if isinstance(scope, list) and scope else None
    )
    comments = item.get("comments") or []
    if not isinstance(comments, list):
        raise _bad_listing()
    title, body, receipt = item.get("title"), item.get("body"), item.get("receipt")
    return ActionItem(
        int(item["index"]),
        _str(item.get("kind")),
        _str(item.get("repo")),
        _str(item.get("target")),
        title if isinstance(title, str) else None,
        body if isinstance(body, str) else None,
        item.get("markdown") is not False,
        _str(item.get("state"), "pending"),
        _opt_str(receipt),
        _opt_int(item.get("reply_to")),
        delete_scope,
        _opt_str(item.get("delete_block")),
        tuple(_comment_item(c) for c in comments),
    )


def _proposal_item(item: object) -> ProposalItem:
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
    return ProposalItem(
        pid,
        _str(item.get("kind")) or pid.split("/", 1)[0],
        state,
        revision,
        _opt_str(item.get("error")),
        _opt_str(item.get("edit_block")),
        _scope(item.get("create_scope")),
        item.get("draftable") is True,
        item.get("deletable") is True,
        _opt_str(item.get("delete_block")),
        item.get("delete_archive") is True,
        _foreign_repos(actions),
        _str(item.get("raw_text")),
        tuple(_action_item(a) for a in actions),
    )


def _container_listing(entry: object) -> ContainerListing:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise _bad_listing()
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
    return ContainerListing(
        entry["name"], error, warnings, tuple(_proposal_item(p) for p in proposals)
    )


def parse_outbox_listing(stdout: str) -> tuple[ContainerListing, ...]:
    """Every container of `jailbee outbox ls -o json`."""
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise _bad_listing() from exc
    if not isinstance(data, dict) or not isinstance(data.get("containers"), list):
        raise _bad_listing()
    return tuple(_container_listing(entry) for entry in data["containers"])


def find_proposal(
    state: OutboxState, key: RowKey | None
) -> tuple[ContainerListing, ProposalItem] | None:
    if key is None:
        return None
    for container in state.containers:
        if container.name != key[0]:
            continue
        for proposal in container.proposals:
            if proposal.id == key[1]:
                return container, proposal
    return None


def _clean(value: str) -> str:
    from jailbee.outbox.inspect import safe_text

    return safe_text(value)


def _first_line(text: str | None) -> str | None:
    for line in _clean(text or "").splitlines():
        if line.strip():
            return line.strip()
    return None


def proposal_summary(item: ProposalItem) -> str:
    first = item.actions[0] if item.actions else None
    if first is not None:
        if first.title and first.title.strip():
            return _clean(first.title).strip()
        line = _first_line(first.body)
        if line is not None:
            return line
    return "(no text)"


def _state_text(item: ProposalItem) -> str:
    if item.state == "partial":
        done = sum(a.state == "applied" for a in item.actions)
        return f"applied {done}/{len(item.actions)}"
    return item.state


def _target(item: ProposalItem) -> str:
    first = item.actions[0] if item.actions else None
    if first is None:
        return _clean(item.id)
    repo = _clean(first.repo)
    if item.kind == "issue" and first.kind == "create":
        return repo
    return f"{repo}#{_clean(first.target)}" if first.target else repo


def _fit(text: Text, width: int, depth: int) -> Text:
    text.no_wrap = True
    text.overflow = "ellipsis"
    text.truncate(max(width - 2 * depth, 1), overflow="ellipsis")
    return text


def _build_proposal_row(
    container: ContainerListing, item: ProposalItem, state: OutboxState, width: int
) -> Row:
    key: RowKey = (container.name, item.id, None, None)
    caret = ("▾" if key in state.expanded else "▸") if item.actions else " "
    mark = "● " if key in state.marks else "  "
    tag = "PR" if item.kind == "pr" else "ISS" if item.kind == "issue" else item.kind.upper()
    text = Text(f"{caret} {mark}{tag.ljust(3)} {_target(item)}  ", no_wrap=True)
    if item.error:
        text.append(_clean(item.error), style="yellow")
    else:
        text.append(proposal_summary(item))
    count = len(item.actions)
    word = "action" if count == 1 else "actions"
    text.append(f"  · {count} {word} · {_state_text(item)}")
    if item.foreign_repos:
        text.append("  ⚠ foreign")
    return Row(key, 0, _fit(text, width, 0), item.deletable, "proposal")


def _action_label(proposal: ProposalItem, action: ActionItem) -> str:
    kind = action.kind
    if proposal.kind == "pr":
        if kind == "review":
            n = len(action.comments)
            return f"review · {n} comment{'' if n == 1 else 's'}"
        if kind == "reply":
            return f"reply to #{action.reply_to}" if action.reply_to is not None else "reply"
        return kind
    if kind == "create":
        return f"create: {_clean(action.title or '').strip() or '(untitled)'}"
    return f"{kind} #{_clean(action.target)}" if action.target else kind


def _action_row(
    container: ContainerListing,
    proposal: ProposalItem,
    action: ActionItem,
    state: OutboxState,
    width: int,
) -> Row:
    key: RowKey = (container.name, proposal.id, action.index, None)
    caret = ("▾" if key in state.expanded else "▸") if action.comments else " "
    mark = "● " if key in state.marks else "  "
    text = Text(f"{caret} {mark}{_action_label(proposal, action)}  [{_clean(action.state)}]")
    return Row(key, 1, _fit(text, width, 1), action.delete_scope is not None, "action")


def _comment_row(
    container: ContainerListing,
    proposal: ProposalItem,
    action: ActionItem,
    comment: CommentItem,
    state: OutboxState,
    width: int,
) -> Row:
    key: RowKey = (container.name, proposal.id, action.index, comment.index)
    mark = "● " if key in state.marks else "  "
    where = (
        f"{_clean(comment.path)}:{comment.line}"
        if comment.path is not None and comment.line is not None
        else _clean(comment.label)
    )
    text = Text(f"{mark}{where}  {_first_line(comment.text) or ''}".rstrip())
    return Row(key, 2, _fit(text, width, 2), action.delete_scope is not None, "comment")


def visible_rows(state: OutboxState, width: int) -> tuple[Row, ...]:
    rows: list[Row] = []
    many = len(state.containers) > 1
    for container in state.containers:
        if container.error:
            header = Text(f"{_clean(container.name)}  ⚠ {_clean(container.error)}", style="yellow")
            rows.append(Row(None, 0, _fit(header, width, 0), False, "header"))
            continue
        if many:
            header = Text(_clean(container.name), style="bold")
            rows.append(Row(None, 0, _fit(header, width, 0), False, "header"))
        for proposal in container.proposals:
            rows.append(_build_proposal_row(container, proposal, state, width))
            if (container.name, proposal.id, None, None) not in state.expanded:
                continue
            for action in proposal.actions:
                rows.append(_action_row(container, proposal, action, state, width))
                if (container.name, proposal.id, action.index, None) not in state.expanded:
                    continue
                for comment in action.comments:
                    rows.append(_comment_row(container, proposal, action, comment, state, width))
        for note in container.warnings:
            rows.append(
                Row(None, 1, _fit(Text(_clean(note), style="dim"), width, 1), False, "note")
            )
    return tuple(rows)


def _body_lines(body: str | None, markdown: bool, width: int) -> list[Text]:
    if not body:
        return []
    if markdown:
        from jailbee.outbox.markdown_view import render_markdown

        return [Text.from_ansi(line) for line in render_markdown(body, width=width)]
    return [Text(line) for line in _clean(body).split("\n")]


def _action_preview(action: ActionItem, width: int) -> list[Text]:
    kind, repo, target = _clean(action.kind), _clean(action.repo), _clean(action.target)
    head = f"Action {action.index}: {kind} {repo} {target} [{_clean(action.state)}]"
    lines = [Text(head)]
    if action.title is not None:
        lines.append(Text(f"Title: {_clean(action.title)}"))
    lines.extend(_body_lines(action.body, action.markdown, width))
    if action.receipt:
        lines.append(Text(f"Receipt: {_clean(action.receipt)}"))
    for comment in action.comments:
        lines.extend(_comment_preview(comment, width))
    return lines


def _comment_preview(comment: CommentItem, width: int) -> list[Text]:
    return [
        Text(f"Comment {comment.index}: {_clean(comment.label)}"),
        *_body_lines(comment.text, True, width),
    ]


def preview_lines(state: OutboxState, key: RowKey | None, width: int) -> tuple[Text, ...]:
    lines: list[Text] = []
    found = find_proposal(state, key)
    if state.question is not None:
        lines.extend(Text(_clean(line)) for line in state.question.lines)
    elif found is not None and key is not None:
        _, proposal = found
        if state.raw:
            lines.extend(Text(_clean(line)) for line in proposal.raw_text.splitlines())
        elif key[2] is None:
            if proposal.error:
                lines.append(Text(_clean(proposal.error), style="yellow"))
            if proposal.edit_block:
                lines.append(Text(_clean(proposal.edit_block)))
            for action in proposal.actions:
                lines.extend(_action_preview(action, width))
        else:
            target = next((a for a in proposal.actions if a.index == key[2]), None)
            if target is not None and key[3] is None:
                lines.extend(_action_preview(target, width))
            elif target is not None:
                comment = next((c for c in target.comments if c.index == key[3]), None)
                if comment is not None:
                    lines.extend(_comment_preview(comment, width))
    if state.status:
        if lines:
            lines.append(Text(""))
        lines.extend(Text(_clean(line)) for line in state.status)
    return tuple(lines)


def delete_scope_lines(state: OutboxState, keys: Iterable[RowKey]) -> tuple[str, ...]:
    chosen = set(keys)
    many = len(state.containers) > 1
    lines: list[str] = []
    for container in state.containers:
        prefix = f"{_clean(container.name)}: " if many else ""
        for proposal in container.proposals:
            whole = (container.name, proposal.id, None, None)
            pid = _clean(proposal.id)
            if whole in chosen:
                suffix = "whole, journal archived" if proposal.delete_archive else "whole"
                lines.append(f"{prefix}{pid} ({suffix})")
                continue
            for action in proposal.actions:
                akey = (container.name, proposal.id, action.index, None)
                if akey in chosen:
                    extra = [i for i in (action.delete_scope or ()) if i != action.index]
                    dep = ""
                    if extra:
                        word = "dependent" if len(extra) == 1 else "dependents"
                        dep = f" + {word} {', '.join(str(i) for i in extra)}"
                    lines.append(
                        f"{prefix}{pid} action {action.index} ({_clean(action.kind)}){dep}"
                    )
                    continue
                for comment in action.comments:
                    if (container.name, proposal.id, action.index, comment.index) in chosen:
                        where = (
                            f"{comment.path}:{comment.line}"
                            if comment.path is not None and comment.line is not None
                            else comment.label
                        )
                        lines.append(
                            f"{prefix}{pid} action {action.index} comment {comment.index} "
                            f"({_clean(where)})"
                        )
    return tuple(lines)


def outbox_hint(state: OutboxState, *, standalone: bool) -> str:
    if state.question is not None:
        return "↑/↓ choose · Enter confirm · Esc cancel"
    ab = state.abilities
    parts = ["↑/↓ move", "Enter open", "Space mark"]
    if ab.delete:
        parts.append("d delete")
    if ab.publish or ab.publish_hand_off:
        parts.append("p publish")
    if ab.create_pr:
        parts.append("c create PR")
    if ab.clear:
        parts.append("X clear")
    parts += ["v preview", "m manifest"]
    if standalone and ab.scope:
        parts.append("a all repos")
    parts.append("r refresh")
    parts.append("q quit" if standalone else "Esc close")
    return " · ".join(parts)


def outbox_ls_argv(name: str | None, *, all_repos: bool = False) -> list[str]:
    argv = ["outbox", "ls"]
    if name is not None:
        argv.append(name)
    if all_repos:
        argv.append("--all-repos")
    return [*argv, "-o", "json"]


def outbox_plan_argv(name: str, proposal: str, revision: str) -> list[str]:
    return [
        "outbox", "apply", name, proposal, "--dry-run", "--foreign",
        "--revision", revision, "-o", "json",
    ]  # fmt: skip


def outbox_apply_argv(
    name: str,
    proposal: str,
    revision: str,
    *,
    yes: bool,
    draft: bool = False,
    foreign: bool = False,
) -> list[str]:
    argv = ["outbox", "apply", name, proposal, "--revision", revision]
    if yes:
        argv.append("-y")
    if draft:
        argv.append("--draft")
    if foreign:
        argv.append("--foreign")
    return argv


def outbox_drop_argv(
    name: str,
    proposal: str,
    revision: str,
    *,
    action: int | None = None,
    comment: int | None = None,
    with_dependents: bool = False,
    archive_journal: bool = False,
) -> list[str]:
    argv = ["outbox", "drop", name, proposal]
    if action is not None:
        argv += ["--action", str(action)]
    if comment is not None:
        argv += ["--comment", str(comment)]
    if with_dependents:
        argv.append("--with-dependents")
    if archive_journal:
        argv.append("--archive-journal")
    return [*argv, "--yes", "--revision", revision]


def clear_prepare_argv(name: str) -> list[str]:
    return ["outbox", "clear", name, "--prepare-snapshot"]


def clear_approved_argv(name: str, token: str) -> list[str]:
    return ["outbox", "clear", name, "--approved-snapshot", token, "--yes"]
