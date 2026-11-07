"""Outbox socket ops, answered by the broker's query workers.

The container is untrusted input: every op takes the caller's uuid from the
socket, never from its arguments, and a staged proposal is re-validated here
(`issue_manifest.parse_manifest`, size caps, the action and pending caps)
before it reaches the store. Role checks happen in `Queries` before dispatch.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from jailbee.issue_manifest import (
    MAX_BODY_BYTES,
    MAX_MANIFESTS,
    MAX_OFFER_ACTIONS,
    IssueManifestError,
    parse_manifest,
)
from jailbee.orchestration import wire
from jailbee.outbox.store_db import (
    KINDS,
    Kind,
    OutboxStore,
    Proposal,
    ProposalCapError,
    ProposalChangedError,
    ProposalLockedError,
)

_SLUG_RE = re.compile(r"[a-z0-9-]{1,80}")
_BODY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROPOSAL_NAME_RE = re.compile(r"[0-9]{3,}-[a-z0-9-]{1,80}\.json")


def _bad(detail: str) -> wire.WireError:
    return wire.WireError(wire.BAD_REQUEST, detail)


def _kind(value: object, *, optional: bool = False) -> Kind | None:
    if value is None and optional:
        return None
    if value not in KINDS:
        raise _bad("kind must be issue or pr")
    return "issue" if value == "issue" else "pr"


def _rename_refs(value: object, names: Mapping[str, str]) -> object:
    if isinstance(value, dict):
        return {
            key: names.get(child, child)
            if key == "body_file" and isinstance(child, str)
            else _rename_refs(child, names)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_rename_refs(child, names) for child in value]
    return value


def _bodies(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise _bad("bodies must map file names to text")
    for name, text in value.items():
        if not _BODY_NAME_RE.fullmatch(name):
            raise _bad(f"body name {name!r} must be a plain file name")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise _bad(f"body {name!r} is not valid UTF-8") from exc
        if size > MAX_BODY_BYTES:
            raise _bad(f"body {name!r} exceeds the 64 KiB limit")
    return dict(value)


def _parse_issue(name: str, manifest: object, bodies: Mapping[str, str]) -> str:
    """Validate one issue manifest against its bodies; return its JSON text."""
    if not isinstance(manifest, dict):
        raise _bad("manifest must be a JSON object")
    actions = manifest.get("actions")
    if isinstance(actions, list) and len(actions) > MAX_OFFER_ACTIONS:
        raise _bad(f"issue manifest exceeds the cap of {MAX_OFFER_ACTIONS} actions")
    try:
        text = json.dumps(manifest, ensure_ascii=False)
        parsed = parse_manifest(name, text, bodies)
    except (IssueManifestError, TypeError, ValueError, RecursionError) as exc:
        raise _bad(str(exc)) from exc
    unused = sorted(set(bodies) - parsed.body_files)
    if unused:
        raise _bad(f"bodies not referenced by the manifest: {', '.join(unused)}")
    return text


def handle_put(store: OutboxStore, uuid: str, args: Mapping[str, Any], *, now: float) -> object:
    if _kind(args.get("kind")) != "issue":
        raise _bad("only issue proposals are staged through the broker so far")
    slug = args.get("slug")
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise _bad("slug must match [a-z0-9-]{1,80}")
    manifest = args.get("manifest")
    bodies = _bodies(args.get("bodies"))
    _parse_issue(f"{slug}.json", manifest, bodies)

    def build(stem: str) -> tuple[str, dict[str, str]]:
        names = {old: f"{stem}-{index}.md" for index, old in enumerate(bodies, 1)}
        final = {names[old]: text for old, text in bodies.items()}
        return _parse_issue(f"{stem}.json", _rename_refs(manifest, names), final), final

    try:
        proposal = store.put_proposal(uuid, "issue", slug, build, now=now, cap=MAX_MANIFESTS)
    except ProposalCapError as exc:
        raise _bad(str(exc)) from exc
    return {"kind": proposal.kind, "name": proposal.name}


def _summary(proposal: Proposal) -> tuple[str, int]:
    try:
        actions = json.loads(proposal.manifest)["actions"]
        return ", ".join(str(a.get("type", "?")) for a in actions) or "no actions", len(actions)
    except (ValueError, KeyError, TypeError, AttributeError):
        return "invalid manifest", 0


def handle_list(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    rows = []
    for proposal in store.list_proposals(uuid, _kind(args.get("kind"), optional=True)):
        summary, count = _summary(proposal)
        rows.append(
            {
                "kind": proposal.kind,
                "name": proposal.name,
                "summary": summary,
                "actions": count,
                "created_at": proposal.created_at,
                "revision": proposal.revision,
            }
        )
    return {"proposals": rows}


def handle_show(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    kind = _kind(args.get("kind"))
    assert kind is not None
    name = args.get("name")
    if not isinstance(name, str) or not _PROPOSAL_NAME_RE.fullmatch(name):
        raise _bad("name must name a staged proposal (NNN-slug.json)")
    proposal = store.get_proposal(uuid, kind, name)
    if proposal is None:
        raise wire.WireError(wire.NOT_FOUND, f"no staged {kind} proposal {name}")
    return {
        "kind": proposal.kind,
        "name": proposal.name,
        "manifest": proposal.manifest,
        "bodies": dict(proposal.bodies),
        "revision": proposal.revision,
    }


def handle_drop(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    kind = _kind(args.get("kind"))
    assert kind is not None
    name = args.get("name")
    if not isinstance(name, str) or not _PROPOSAL_NAME_RE.fullmatch(name):
        raise _bad("name must name a staged proposal (NNN-slug.json)")
    revision = args.get("revision")
    if revision is not None and (type(revision) is not int or revision < 1):
        raise _bad("revision must be a positive integer")
    try:
        store.drop_proposal(uuid, kind, name, revision)
    except ProposalChangedError as exc:
        raise wire.WireError(wire.NOT_FOUND, f"no staged {kind} proposal {name}") from exc
    except ProposalLockedError as exc:
        raise _bad(str(exc)) from exc
    return {"dropped": name}


def handle(
    store: OutboxStore, uuid: str, op: str, args: Mapping[str, Any], *, now: float
) -> object:
    if op == wire.OUTBOX_PUT:
        return handle_put(store, uuid, args, now=now)
    if op == wire.OUTBOX_LIST:
        return handle_list(store, uuid, args)
    if op == wire.OUTBOX_SHOW:
        return handle_show(store, uuid, args)
    if op == wire.OUTBOX_DROP:
        return handle_drop(store, uuid, args)
    raise _bad(f"unknown op '{op}'")
