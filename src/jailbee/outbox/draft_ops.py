"""Draft batch socket ops (``jailbee issue … --batch NAME``), answered by query workers.

A draft is a manifest under construction. The container sends one action at a time;
the host appends it under the draft's row lock, so concurrent appends serialise, and
derives the ``expected`` values of ``@REF`` actions from the earlier create *in the
stored draft*, whatever the container claimed. Every append re-validates the whole
draft and submit re-validates it again with the same caps as `outbox.put`.

The container is untrusted input, as in `broker_ops`: uuid from the socket, role
checks in `Queries` before dispatch (a child may read drafts, never change them).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from jailbee.issue_manifest import (
    MAX_MANIFESTS,
    MAX_OFFER_ACTIONS,
    ME_LOGIN,
)
from jailbee.issue_manifest import _LOGIN_RE as LOGIN_RE
from jailbee.orchestration import wire
from jailbee.outbox.broker_ops import _bad, _bodies, _kind, _parse_issue, _rename_refs
from jailbee.outbox.store_db import (
    Draft,
    DraftCapError,
    DraftNotFoundError,
    DraftValidate,
    OutboxStore,
    ProposalCapError,
)

_DRAFT_NAME_RE = re.compile(r"[a-z0-9-]{1,40}")


def _issue_kind(args: Mapping[str, Any]) -> Any:
    kind = _kind(args.get("kind"))
    if kind != "issue":
        raise _bad("only issue drafts are kept by the broker so far")
    return kind


def _name(args: Mapping[str, Any]) -> str:
    name = args.get("name")
    if not isinstance(name, str) or not _DRAFT_NAME_RE.fullmatch(name):
        raise _bad("draft name must match [a-z0-9-]{1,40}")
    return name


def _string_list(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _bad(f"{what} must be a list of strings")
    return list(value)


def _check_labels(add: object, remove: object, labels: list[str]) -> None:
    current = {x.casefold() for x in labels}
    for item in _string_list(add, "labels add"):
        if item.casefold() in current:
            raise _bad(f"label {item!r} already present")
    for item in _string_list(remove, "labels remove"):
        if item.casefold() not in current:
            raise _bad(f"label {item!r} is not present")


def _check_assignees(add: object, remove: object) -> None:
    """The created issue has no assignees yet: nothing can be removed, and the
    syntax rules match `issue_manifest`'s."""
    added = _string_list(add, "assignees add")
    removed = _string_list(remove, "assignees remove")
    for items in (added, removed):
        seen: set[str] = set()
        for item in items:
            if item != ME_LOGIN and not LOGIN_RE.fullmatch(item):
                raise _bad(f"invalid GitHub login {item!r}")
            if item.casefold() in seen:
                raise _bad(f"assignee {item!r} given more than once")
            seen.add(item.casefold())
    gone = {x.casefold() for x in removed}
    for item in added:
        if item.casefold() in gone:
            raise _bad(f"login {item!r} cannot be both add and remove")
    for item in removed:
        if item != ME_LOGIN:
            raise _bad(f"assignee {item!r} is not assigned")


def _prepare(
    manifest: dict[str, Any], bodies: Mapping[str, str], action: dict[str, Any]
) -> dict[str, Any]:
    """Derive ``expected`` for an ``issue_ref`` action from the draft's own create."""
    ref = action.get("issue_ref")
    if not isinstance(ref, str):
        return action
    for create in manifest["actions"]:
        if (
            isinstance(create, dict)
            and create.get("type") == "create"
            and create.get("ref") == ref
            and create.get("repo") == action.get("repo")
        ):
            body = create.get("body")
            if "body_file" in create:
                body = bodies.get(create["body_file"])
            title = create.get("title")
            labels = create.get("labels", [])
            if (
                not isinstance(title, str)
                or not isinstance(body, str)
                or not isinstance(labels, list)
                or not all(isinstance(label, str) for label in labels)
            ):
                raise _bad(f"issue_ref {ref!r} has an invalid create action")
            result = dict(action)
            kind = action.get("type")
            if kind == "edit":
                expected: dict[str, Any] = {}
                if "title" in action:
                    expected["title"] = title
                if "body" in action or "body_file" in action:
                    expected["body"] = body
                result["expected"] = expected
            elif kind == "labels":
                _check_labels(action.get("add", []), action.get("remove", []), labels)
                result["expected"] = {"labels": list(labels)}
            elif kind == "state":
                result["expected"] = {"state": "open"}
            elif kind == "assignees":
                supplied = action.get("expected")
                if supplied is not None:
                    names = supplied.get("assignees", []) if isinstance(supplied, dict) else None
                    _string_list(names, "assignees expected.assignees")
                _check_assignees(action.get("add", []), action.get("remove", []))
                result["expected"] = {"assignees": []}
            return result
    raise _bad(f"issue_ref {ref!r} does not name an earlier create in the same repo")


def _validate(name: str) -> DraftValidate:
    def check(manifest: dict[str, Any], bodies: Mapping[str, str]) -> None:
        if len(manifest["actions"]) > MAX_OFFER_ACTIONS:
            raise _bad(f"issue manifest exceeds the cap of {MAX_OFFER_ACTIONS} actions")
        _parse_issue(f"{name}.json", manifest, bodies)

    return check


def handle_append(
    store: OutboxStore, uuid: str, args: Mapping[str, Any], *, now: float
) -> object:
    kind = _issue_kind(args)
    name = _name(args)
    action = args.get("action")
    if not isinstance(action, dict):
        raise _bad("action must be a JSON object")
    bodies = _bodies(args.get("bodies"))
    try:
        draft = store.draft_append(
            uuid, kind, name, action, bodies, now=now, prepare=_prepare, validate=_validate(name)
        )
    except DraftCapError as exc:
        raise _bad(str(exc)) from exc
    return _row(draft)


def _row(draft: Draft) -> dict[str, object]:
    return {
        "kind": draft.kind,
        "name": draft.name,
        "actions": draft.action_count,
        "revision": draft.revision,
    }


def handle_list(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    kind = _kind(args.get("kind"), optional=True)
    return {"drafts": [_row(d) for d in store.list_drafts(uuid, kind)]}


def handle_show(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    kind = _issue_kind(args)
    name = _name(args)
    draft = store.get_draft(uuid, kind, name)
    if draft is None:
        raise wire.WireError(wire.NOT_FOUND, f"no {kind} draft {name}")
    return {
        **_row(draft),
        "manifest": draft.manifest,
        "bodies": dict(draft.bodies),
    }


def handle_discard(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    kind = _issue_kind(args)
    name = _name(args)
    try:
        store.discard_draft(uuid, kind, name)
    except DraftNotFoundError as exc:
        raise wire.WireError(wire.NOT_FOUND, f"no {kind} draft {name}") from exc
    return {"kind": kind, "discarded": name}


def handle_submit(
    store: OutboxStore, uuid: str, args: Mapping[str, Any], *, now: float
) -> object:
    kind = _issue_kind(args)
    name = _name(args)

    def build(stem: str, manifest_text: str, bodies: Mapping[str, str]) -> tuple[str, dict[str, str]]:
        manifest = json.loads(manifest_text)
        if len(manifest.get("actions", [])) > MAX_OFFER_ACTIONS:
            raise _bad(f"issue manifest exceeds the cap of {MAX_OFFER_ACTIONS} actions")
        names = {old: f"{stem}-{index}.md" for index, old in enumerate(bodies, 1)}
        final = {names[old]: text for old, text in bodies.items()}
        text = _parse_issue(f"{stem}.json", _rename_refs(manifest, names), final)
        return text, final

    try:
        proposal = store.submit_draft(uuid, kind, name, name, build, now=now, cap=MAX_MANIFESTS)
    except DraftNotFoundError as exc:
        raise wire.WireError(wire.NOT_FOUND, f"no {kind} draft {name}") from exc
    except ProposalCapError as exc:
        raise _bad(str(exc)) from exc
    return {"kind": proposal.kind, "name": proposal.name}


def handle(
    store: OutboxStore, uuid: str, op: str, args: Mapping[str, Any], *, now: float
) -> object:
    if op == wire.OUTBOX_DRAFT_APPEND:
        return handle_append(store, uuid, args, now=now)
    if op == wire.OUTBOX_DRAFT_LIST:
        return handle_list(store, uuid, args)
    if op == wire.OUTBOX_DRAFT_SHOW:
        return handle_show(store, uuid, args)
    if op == wire.OUTBOX_DRAFT_DISCARD:
        return handle_discard(store, uuid, args)
    if op == wire.OUTBOX_DRAFT_SUBMIT:
        return handle_submit(store, uuid, args, now=now)
    raise _bad(f"unknown op '{op}'")
