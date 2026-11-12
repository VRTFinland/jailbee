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
from collections.abc import Callable, Mapping
from typing import Any

from jailbee.issue_manifest import (
    LOGIN_RE,
    MAX_MANIFESTS,
    MAX_OFFER_ACTIONS,
    ME_LOGIN,
)
from jailbee.orchestration import wire
from jailbee.outbox.broker_ops import (
    MAX_PR_ACTIONS,
    ProjectReader,
    bad_request,
    capture_project,
    kind_arg,
    optional_kind_arg,
    parse_bodies,
    parse_issue,
    parse_pr,
    pr_body_refs,
    provisional_project,
    rename_refs,
)
from jailbee.outbox.store_db import (
    Draft,
    DraftCapError,
    DraftNotFoundError,
    DraftValidate,
    OutboxStore,
    ProposalCapError,
)

_DRAFT_NAME_RE = re.compile(r"[a-z0-9-]{1,40}")


def _name(args: Mapping[str, Any]) -> str:
    name = args.get("name")
    if not isinstance(name, str) or not _DRAFT_NAME_RE.fullmatch(name):
        raise bad_request("draft name must match [a-z0-9-]{1,40}")
    return name


def _string_list(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise bad_request(f"{what} must be a list of strings")
    return list(value)


def _check_labels(add: object, remove: object, labels: list[str]) -> None:
    current = {x.casefold() for x in labels}
    for item in _string_list(add, "labels add"):
        if item.casefold() in current:
            raise bad_request(f"label {item!r} already present")
    for item in _string_list(remove, "labels remove"):
        if item.casefold() not in current:
            raise bad_request(f"label {item!r} is not present")


def _check_assignees(add: object, remove: object) -> None:
    """The created issue has no assignees yet: nothing can be removed, and the
    syntax rules match `issue_manifest`'s."""
    added = _string_list(add, "assignees add")
    removed = _string_list(remove, "assignees remove")
    for items in (added, removed):
        seen: set[str] = set()
        for item in items:
            if item != ME_LOGIN and not LOGIN_RE.fullmatch(item):
                raise bad_request(f"invalid GitHub login {item!r}")
            if item.casefold() in seen:
                raise bad_request(f"assignee {item!r} given more than once")
            seen.add(item.casefold())
    gone = {x.casefold() for x in removed}
    for item in added:
        if item.casefold() in gone:
            raise bad_request(f"login {item!r} cannot be both add and remove")
    for item in removed:
        if item != ME_LOGIN:
            raise bad_request(f"assignee {item!r} is not assigned")


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
                raise bad_request(f"issue_ref {ref!r} has an invalid create action")
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
    raise bad_request(f"issue_ref {ref!r} does not name an earlier create in the same repo")


def _validate(name: str) -> DraftValidate:
    def check(manifest: dict[str, Any], bodies: Mapping[str, str]) -> None:
        if len(manifest["actions"]) > MAX_OFFER_ACTIONS:
            raise bad_request(f"issue manifest exceeds the cap of {MAX_OFFER_ACTIONS} actions")
        parse_issue(f"{name}.json", manifest, bodies)

    return check


def handle_append(
    store: OutboxStore,
    uuid: str,
    args: Mapping[str, Any],
    *,
    now: float,
    project_reader: ProjectReader | None = None,
    default_project: str | None = None,
) -> object:
    kind = kind_arg(args.get("kind"))
    name = _name(args)
    action = args.get("action")
    added = args.get("actions")
    if "actions" in args:
        if "action" in args or kind != "issue" or not isinstance(added, list) or not added or not all(isinstance(a, dict) for a in added):
            raise bad_request("supply action or a non-empty issue actions list")
        action = added[0]
    if not isinstance(action, dict):
        raise bad_request("action must be a JSON object")
    prevalidate = None
    if kind == "issue":
        def prevalidate(manifest: dict[str, Any], all_bodies: Mapping[str, str]) -> None:
            provisional: dict[str, Any] = {**manifest, "actions": []}
            for entry in manifest["actions"]:
                derived = _prepare(provisional, all_bodies, entry)
                provisional["actions"].append(provisional_project(derived, default_project))
            _validate(name)(provisional, all_bodies)
    bodies = parse_bodies(args.get("bodies"))
    if kind == "pr":
        prepare, validate = _pr_prepare(_pr_envelope(args.get("envelope"))), _pr_validate(name)
    else:
        validate = _validate(name)

        def prepare(
            manifest: dict[str, Any], stored_bodies: Mapping[str, str], added: dict[str, Any]
        ) -> dict[str, Any]:
            derived = _prepare(manifest, stored_bodies, added)
            if derived.get("type") in (
                "project_add",
                "project_set",
                "project_clear",
                "project_remove",
            ):
                provisional = provisional_project(derived, default_project)
                validate(
                    {**manifest, "actions": [*manifest["actions"], provisional]}, stored_bodies
                )
                return capture_project(provisional, project_reader, default_project)
            return derived

    try:
        draft = store.draft_append(
            uuid, kind, name, action, bodies, now=now, prepare=prepare, validate=validate,
            actions=added, prevalidate=prevalidate
        )
    except DraftCapError as exc:
        raise bad_request(str(exc)) from exc
    return _row(draft)


def _row(draft: Draft) -> dict[str, object]:
    return {
        "kind": draft.kind,
        "name": draft.name,
        "actions": draft.action_count,
        "revision": draft.revision,
    }


def handle_list(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    kind = optional_kind_arg(args.get("kind"))
    return {"drafts": [_row(d) for d in store.list_drafts(uuid, kind)]}


def handle_show(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    kind = kind_arg(args.get("kind"))
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
    kind = kind_arg(args.get("kind"))
    name = _name(args)
    try:
        store.discard_draft(uuid, kind, name)
    except DraftNotFoundError as exc:
        raise wire.WireError(wire.NOT_FOUND, f"no {kind} draft {name}") from exc
    return {"kind": kind, "discarded": name}


def handle_submit(store: OutboxStore, uuid: str, args: Mapping[str, Any], *, now: float) -> object:
    kind = kind_arg(args.get("kind"))
    name = _name(args)
    if kind == "pr":
        return _submit_pr(store, uuid, name, args, now=now)

    def build(
        stem: str, manifest_text: str, bodies: Mapping[str, str]
    ) -> tuple[str, dict[str, str]]:
        manifest = json.loads(manifest_text)
        if len(manifest.get("actions", [])) > MAX_OFFER_ACTIONS:
            raise bad_request(f"issue manifest exceeds the cap of {MAX_OFFER_ACTIONS} actions")
        names = {old: f"{stem}-{index}.md" for index, old in enumerate(bodies, 1)}
        final = {names[old]: text for old, text in bodies.items()}
        text = parse_issue(f"{stem}.json", rename_refs(manifest, names), final)
        return text, final

    try:
        proposal = store.submit_draft(uuid, kind, name, name, build, now=now, cap=MAX_MANIFESTS)
    except DraftNotFoundError as exc:
        raise wire.WireError(wire.NOT_FOUND, f"no {kind} draft {name}") from exc
    except ProposalCapError as exc:
        raise bad_request(str(exc)) from exc
    return {"kind": proposal.kind, "name": proposal.name}


# -- PR drafts ----------------------------------------------------------------
#
# Two shapes, as in the container's old file store: a batch (``--batch NAME``) of
# comment, reply and description actions, and a review draft (``review-<PR>``)
# whose one review action collects inline comments. The container sends the PR
# envelope (repo, pr, head_sha) it read with its own ``gh``; the first append
# fixes it, every later one must match it, and a review additionally pins the
# head its line anchors were read at.

MAX_PR_DRAFT_ACTIONS = MAX_PR_ACTIONS
_ENVELOPE_KEYS = ("repo", "pr", "head_sha")


def _pr_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise bad_request("envelope must be a JSON object with repo, pr and head_sha")
    return {key: value.get(key) for key in _ENVELOPE_KEYS}


def _is_review(action: object) -> bool:
    return isinstance(action, dict) and action.get("type") == "review"


def _pr_prepare(
    envelope: Mapping[str, Any],
) -> Callable[[dict[str, Any], Mapping[str, str], dict[str, Any]], dict[str, Any]]:
    def prepare(
        manifest: dict[str, Any], bodies: Mapping[str, str], action: dict[str, Any]
    ) -> dict[str, Any]:
        actions = manifest["actions"]
        if not actions:
            manifest.update(envelope)
        elif any(manifest.get(key) != envelope[key] for key in ("repo", "pr")):
            raise bad_request("draft targets a different repo or PR")
        if not _is_review(action):
            if any(_is_review(existing) for existing in actions):
                raise bad_request("cannot append a batch action to a review draft")
            return action
        if actions and not (len(actions) == 1 and _is_review(actions[0])):
            raise bad_request("not a review draft; choose a different batch name")
        if actions and manifest.get("head_sha") != envelope["head_sha"]:
            raise bad_request(
                "PR head moved; discard the review draft and re-read the diff before re-anchoring"
            )
        if not actions:
            return action
        # Merge into the draft's one review action: the store appends the
        # returned action, so the old one is taken out of the loaded manifest.
        current = actions.pop()
        added = action.get("comments")
        if not isinstance(added, list) or not isinstance(current.get("comments"), list):
            raise bad_request("review comments must be a list")
        return {**current, "comments": [*current["comments"], *added]}

    return prepare


def _pr_validate(name: str) -> DraftValidate:
    def check(manifest: dict[str, Any], bodies: Mapping[str, str]) -> None:
        if len(manifest["actions"]) > MAX_PR_DRAFT_ACTIONS:
            raise bad_request(f"PR manifest exceeds the cap of {MAX_PR_DRAFT_ACTIONS} actions")
        parse_pr(f"{name}.json", manifest, bodies)

    return check


def _submit_pr(
    store: OutboxStore, uuid: str, name: str, args: Mapping[str, Any], *, now: float
) -> object:
    """Stage a PR draft. A review draft needs ``review`` (the repo, PR and head the
    summary was written against, and the summary body) and is staged as
    ``NNN-review.json``."""
    current = store.get_draft(uuid, "pr", name)
    if current is None:
        raise wire.WireError(wire.NOT_FOUND, f"no pr draft {name}")
    review = args.get("review")
    if review is not None and not isinstance(review, dict):
        raise bad_request("review must be a JSON object")
    extra = parse_bodies(args.get("bodies"))
    is_review = any(_is_review(a) for a in json.loads(current.manifest).get("actions", []))
    if is_review and review is None:
        raise bad_request(
            "review drafts require fresh PR metadata; use jailbee pr review submit PR"
        )
    if review is not None and not is_review:
        raise bad_request("not a review draft; choose a different batch name")

    def build(
        stem: str, manifest_text: str, bodies: Mapping[str, str]
    ) -> tuple[str, dict[str, str]]:
        manifest = json.loads(manifest_text)
        actions = manifest.get("actions", [])
        if review is not None:
            if len(actions) != 1 or not _is_review(actions[0]):
                raise bad_request("not a review draft; choose a different batch name")
            if any(manifest.get(key) != review.get(key) for key in ("repo", "pr")):
                raise bad_request("draft targets a different repo or PR")
            if manifest.get("head_sha") != review.get("head_sha"):
                raise bad_request(
                    "PR head moved; discard the review draft and re-read the diff "
                    "before re-anchoring"
                )
            summary = {k: v for k, v in actions[0].items() if k not in ("body", "body_file")}
            if "body_file" in review:
                summary["body_file"] = review["body_file"]
            else:
                summary["body"] = review.get("body", "")
            manifest["actions"] = [summary]
        elif any(_is_review(a) for a in actions):
            raise bad_request(
                "review drafts require fresh PR metadata; use jailbee pr review submit PR"
            )
        if len(manifest["actions"]) > MAX_PR_DRAFT_ACTIONS:
            raise bad_request(f"PR manifest exceeds the cap of {MAX_PR_DRAFT_ACTIONS} actions")
        merged = {**bodies, **extra}
        refs = pr_body_refs(json.dumps(manifest, ensure_ascii=False))
        used = {n: t for n, t in merged.items() if n in refs}
        names = {old: f"{stem}-{index}.md" for index, old in enumerate(used, 1)}
        final = {names[old]: text for old, text in used.items()}
        text = parse_pr(f"{stem}.json", rename_refs(manifest, names), final)
        return text, final

    slug = "review" if is_review else name
    try:
        proposal = store.submit_draft(uuid, "pr", name, slug, build, now=now, cap=MAX_MANIFESTS)
    except DraftNotFoundError as exc:
        raise wire.WireError(wire.NOT_FOUND, f"no pr draft {name}") from exc
    except ProposalCapError as exc:
        raise bad_request(str(exc)) from exc
    return {"kind": proposal.kind, "name": proposal.name}


def handle(
    store: OutboxStore,
    uuid: str,
    op: str,
    args: Mapping[str, Any],
    *,
    now: float,
    project_reader: ProjectReader | None = None,
    default_project: str | None = None,
) -> object:
    if op == wire.OUTBOX_DRAFT_APPEND:
        return handle_append(
            store,
            uuid,
            args,
            now=now,
            project_reader=project_reader,
            default_project=default_project,
        )
    if op == wire.OUTBOX_DRAFT_LIST:
        return handle_list(store, uuid, args)
    if op == wire.OUTBOX_DRAFT_SHOW:
        return handle_show(store, uuid, args)
    if op == wire.OUTBOX_DRAFT_DISCARD:
        return handle_discard(store, uuid, args)
    if op == wire.OUTBOX_DRAFT_SUBMIT:
        return handle_submit(store, uuid, args, now=now)
    raise bad_request(f"unknown op '{op}'")
