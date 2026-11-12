"""Outbox socket ops, answered by the broker's query workers.

The container is untrusted input: every op takes the caller's uuid from the
socket, never from its arguments, and a staged proposal is re-validated here
(`issue_manifest.parse_manifest`, size caps, the action and pending caps)
before it reaches the store. Role checks happen in `Queries` before dispatch.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from jailbee.issue_manifest import (
    MAX_BODY_BYTES,
    MAX_MANIFESTS,
    MAX_OFFER_ACTIONS,
    IssueManifestError,
    parse_manifest,
)
from jailbee.issue_projects import ProjectExpectation
from jailbee.orchestration import wire
from jailbee.outbox.store_db import (
    Kind,
    OutboxStore,
    Proposal,
    ProposalCapError,
    ProposalChangedError,
    ProposalLockedError,
)

if TYPE_CHECKING:
    from jailbee.outbox.publication_db import PublicationStore

_SLUG_RE = re.compile(r"[a-z0-9-]{1,80}")
_BODY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROPOSAL_NAME_RE = re.compile(r"[0-9]{3,}-[a-z0-9-]{1,80}\.json")
MAX_PR_ACTIONS = 100  # actions per PR proposal or draft, as MAX_OFFER_ACTIONS for issues


def bad_request(detail: str) -> wire.WireError:
    return wire.WireError(wire.BAD_REQUEST, detail)


def kind_arg(value: object) -> Kind:
    """The op's ``kind`` argument; a bad request unless it is issue or pr."""
    kind = wire.outbox_kind(value)
    if kind is None:
        raise bad_request("kind must be issue or pr")
    return kind


def optional_kind_arg(value: object) -> Kind | None:
    """As `kind_arg`, with None (omitted) meaning both kinds."""
    return None if value is None else kind_arg(value)


def rename_refs(value: object, names: Mapping[str, str]) -> object:
    if isinstance(value, dict):
        return {
            key: names.get(child, child)
            if key == "body_file" and isinstance(child, str)
            else rename_refs(child, names)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [rename_refs(child, names) for child in value]
    return value


def parse_bodies(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise bad_request("bodies must map file names to text")
    for name, text in value.items():
        if not _BODY_NAME_RE.fullmatch(name):
            raise bad_request(f"body name {name!r} must be a plain file name")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise bad_request(f"body {name!r} is not valid UTF-8") from exc
        if size > MAX_BODY_BYTES:
            raise bad_request(f"body {name!r} exceeds the 64 KiB limit")
    return dict(value)


def parse_issue(name: str, manifest: object, bodies: Mapping[str, str]) -> str:
    """Validate one issue manifest against its bodies; return its JSON text."""
    if not isinstance(manifest, dict):
        raise bad_request("manifest must be a JSON object")
    actions = manifest.get("actions")
    if isinstance(actions, list) and len(actions) > MAX_OFFER_ACTIONS:
        raise bad_request(f"issue manifest exceeds the cap of {MAX_OFFER_ACTIONS} actions")
    try:
        text = json.dumps(manifest, ensure_ascii=False)
        parsed = parse_manifest(name, text, bodies)
    except (IssueManifestError, TypeError, ValueError, RecursionError) as exc:
        raise bad_request(str(exc)) from exc
    unused = sorted(set(bodies) - parsed.body_files)
    if unused:
        raise bad_request(f"bodies not referenced by the manifest: {', '.join(unused)}")
    return text


def parse_pr(name: str, manifest: object, bodies: Mapping[str, str]) -> str:
    """Validate one PR manifest (any kind: review, reply, comment, description or
    source) against its bodies with `pr_manifest.parse_manifest`; return its JSON text."""
    from jailbee.pr_manifest import ManifestError
    from jailbee.pr_manifest import parse_manifest as parse_pr_manifest

    if not isinstance(manifest, dict):
        raise bad_request("manifest must be a JSON object")
    actions = manifest.get("actions")
    if isinstance(actions, list) and len(actions) > MAX_PR_ACTIONS:
        raise bad_request(f"PR manifest exceeds the cap of {MAX_PR_ACTIONS} actions")
    try:
        text = json.dumps(manifest, ensure_ascii=False)
        parse_pr_manifest(name, text, bodies)
    except (ManifestError, TypeError, ValueError, RecursionError) as exc:
        raise bad_request(str(exc)) from exc
    unused = sorted(set(bodies) - pr_body_refs(text))
    if unused:
        raise bad_request(f"bodies not referenced by the manifest: {', '.join(unused)}")
    return text


def pr_body_refs(text: str) -> set[str]:
    """Body names a PR manifest's actions reference (not extension metadata), so the
    stored bodies are exactly what the host's proposal digest covers."""
    from jailbee.pr_outbox import body_references

    try:
        return body_references(text)
    except (ValueError, RecursionError) as exc:
        raise bad_request(str(exc)) from exc


ProjectReader = Callable[[str, str, int, str | None], ProjectExpectation]


def provisional_project(action: dict[str, Any], default_project: str | None) -> dict[str, Any]:
    """Validate host-filled syntax before any project read."""
    if action.get("type") not in ("project_add", "project_set", "project_clear", "project_remove"):
        return action
    project = action.get("project", default_project)
    if project is None:
        raise bad_request("supply --project owner/number or configure github.project")
    expected: dict[str, Any] = {"present": False}
    if action["type"] in ("project_set", "project_clear"):
        expected["value"] = None
    return {**action, "project": project, "expected": expected}


def capture_project(
    action: dict[str, Any], reader: ProjectReader | None, default_project: str | None
) -> dict[str, Any]:
    """Host-owned expectations; the container's supplied membership is never trusted."""
    from jailbee.issue_github import IssueGithubReadError
    from jailbee.issue_manifest import valid_project

    if action.get("type") not in ("project_add", "project_set", "project_clear", "project_remove"):
        return action
    project = action.get("project", default_project)
    if not isinstance(project, str) or not valid_project(project):
        raise bad_request("supply --project owner/number or configure github.project")
    result = {**action, "project": project}
    if "issue_ref" in action:
        result["expected"] = {
            "present": False,
            **({"value": None} if action["type"] in ("project_set", "project_clear") else {}),
        }
    else:
        number, repo = action.get("issue"), action.get("repo")
        if type(number) is not int or number <= 0 or not isinstance(repo, str):
            raise bad_request("project action requires a positive issue number and repo")
        if reader is None:
            raise wire.WireError(wire.REFUSED, "host Projects reader is unavailable; retry shortly")
        try:
            snapshot = reader(project, repo, number, action.get("field"))
        except (IssueGithubReadError, ValueError) as exc:
            raise wire.WireError(wire.REFUSED, str(exc)) from exc
        if not isinstance(snapshot, ProjectExpectation) or type(snapshot.present) is not bool:
            raise wire.WireError(wire.REFUSED, "host Projects reader returned invalid membership")
        result["expected"] = {
            "present": snapshot.present,
            **(
                {"value": snapshot.value}
                if action["type"] in ("project_set", "project_clear")
                else {}
            ),
        }
    return result


def handle_put(
    store: OutboxStore,
    uuid: str,
    args: Mapping[str, Any],
    *,
    now: float,
    project_reader: ProjectReader | None = None,
    default_project: str | None = None,
) -> object:
    kind = kind_arg(args.get("kind"))
    slug = args.get("slug")
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise bad_request("slug must match [a-z0-9-]{1,80}")
    manifest = args.get("manifest")
    bodies = parse_bodies(args.get("bodies"))
    parse = parse_issue if kind == "issue" else parse_pr
    if kind == "issue" and isinstance(manifest, dict) and isinstance(manifest.get("actions"), list):
        # Validate the entire untrusted proposal and its caps before any host read.
        provisional = {
            **manifest,
            "actions": [
                provisional_project(a, default_project) if isinstance(a, dict) else a
                for a in manifest["actions"]
            ],
        }
        parse_issue(f"{slug}.json", provisional, bodies)
        manifest = {
            **manifest,
            "actions": [
                capture_project(a, project_reader, default_project) for a in provisional["actions"]
            ],
        }
    parse(f"{slug}.json", manifest, bodies)

    def build(stem: str) -> tuple[str, dict[str, str]]:
        names = {old: f"{stem}-{index}.md" for index, old in enumerate(bodies, 1)}
        final = {names[old]: text for old, text in bodies.items()}
        return parse(f"{stem}.json", rename_refs(manifest, names), final), final

    try:
        proposal = store.put_proposal(uuid, kind, slug, build, now=now, cap=MAX_MANIFESTS)
    except ProposalCapError as exc:
        raise bad_request(str(exc)) from exc
    return {"kind": proposal.kind, "name": proposal.name}


def _summary(proposal: Proposal) -> tuple[str, int]:
    try:
        actions = json.loads(proposal.manifest)["actions"]
        return ", ".join(str(a.get("type", "?")) for a in actions) or "no actions", len(actions)
    except (ValueError, KeyError, TypeError, AttributeError):
        return "invalid manifest", 0


def handle_list(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    rows = []
    for proposal in store.list_proposals(uuid, optional_kind_arg(args.get("kind"))):
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
    kind = kind_arg(args.get("kind"))
    name = args.get("name")
    if not isinstance(name, str) or not _PROPOSAL_NAME_RE.fullmatch(name):
        raise bad_request("name must name a staged proposal (NNN-slug.json)")
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


SENT_DEFAULT_LIMIT = 50
SENT_MAX_LIMIT = 200
_SENT_SUMMARY_CHARS = 120


def handle_sent(store: PublicationStore, uuid: str, args: Mapping[str, Any]) -> object:
    """The caller's own sent records, newest first: what the host published for it.

    Only rows of the calling instance (its uuid from the socket) are returned;
    the content is cut to a one-line summary to stay inside the reply cap.
    """
    limit = args.get("limit", SENT_DEFAULT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= SENT_MAX_LIMIT:
        raise bad_request(f"limit must be an integer from 1 to {SENT_MAX_LIMIT}")
    kind = optional_kind_arg(args.get("kind"))
    rows = []
    for record in store.sent_for(uuid, outbox=kind, limit=limit):
        first = next((line for line in record.content.splitlines() if line.strip()), "")
        rows.append(
            {
                "sent_at": record.sent_at,
                "outbox": record.outbox,
                "kind": record.kind,
                "manifest": record.manifest,
                "index": record.index,
                "repo": record.repo,
                "target": record.target,
                "url": record.url,
                "state": record.state,
                "publisher": record.publisher,
                "title": record.title,
                "summary": first[:_SENT_SUMMARY_CHARS],
            }
        )
    return {"records": rows}


def handle_drop(store: OutboxStore, uuid: str, args: Mapping[str, Any]) -> object:
    kind = kind_arg(args.get("kind"))
    name = args.get("name")
    if not isinstance(name, str) or not _PROPOSAL_NAME_RE.fullmatch(name):
        raise bad_request("name must name a staged proposal (NNN-slug.json)")
    revision = args.get("revision")
    if revision is not None and (type(revision) is not int or revision < 1):
        raise bad_request("revision must be a positive integer")
    try:
        store.drop_proposal(uuid, kind, name, revision)
    except ProposalChangedError as exc:
        raise wire.WireError(wire.NOT_FOUND, f"no staged {kind} proposal {name}") from exc
    except ProposalLockedError as exc:
        raise bad_request(str(exc)) from exc
    return {"dropped": name}


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
    if op in wire.OUTBOX_DRAFT_OPS:
        from jailbee.outbox import draft_ops

        return draft_ops.handle(
            store,
            uuid,
            op,
            args,
            now=now,
            project_reader=project_reader,
            default_project=default_project,
        )
    if op == wire.OUTBOX_PUT:
        return handle_put(
            store,
            uuid,
            args,
            now=now,
            project_reader=project_reader,
            default_project=default_project,
        )
    if op == wire.OUTBOX_LIST:
        return handle_list(store, uuid, args)
    if op == wire.OUTBOX_SHOW:
        return handle_show(store, uuid, args)
    if op == wire.OUTBOX_DROP:
        return handle_drop(store, uuid, args)
    raise bad_request(f"unknown op '{op}'")
