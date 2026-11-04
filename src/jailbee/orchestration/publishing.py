"""Request-driven host publication with durable budget and asynchronous results."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee import branch_pr, git, issue_manifest, issue_outbox, pr_outbox
from jailbee.github_repo import github_slug
from jailbee.incus import Incus
from jailbee.orchestration.actions import HANDLERS, ActionContext, Outcome, done, refused
from jailbee.orchestration.grant import Grant, PublishRights, effective_grant
from jailbee.orchestration import labels, profiles
from jailbee.orchestration.protocol import Manifest, PublishManifest
from jailbee.orchestration.state import atomic_write_text
from jailbee.outbox.models import ProposalId
from jailbee.outbox.publish import PublishOptions, publish_selected
from jailbee.outbox_io import JournalStore, container_identity

if TYPE_CHECKING:
    from jailbee.config import Config

log = logging.getLogger(__name__)


def _load(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    return json.loads(path.read_text()) if path.exists() else {}


def _save(root: Path, name: str, data: dict[str, Any]) -> None:
    atomic_write_text(root / name, json.dumps(data, sort_keys=True))


def _result(root: Path, full: str, ref: str) -> Path:
    return root / "publish-results" / f"{hashlib.sha256(full.encode()).hexdigest()}-{ref}.json"


def inflight(root: Path) -> dict[str, Any]:
    return _load(root, "publish-inflight.json")


def _kind(action: issue_manifest.IssueAction) -> str:
    return type(action).__name__.removesuffix("Action").lower()


def _coverage(rights: PublishRights, kind: str, target: issue_outbox.RepoTarget) -> None:
    if not rights.covers_issue(kind, target.slug, foreign=target.foreign):
        raise issue_outbox.IssueGateError(
            f"issue action `{kind}` on `{target.slug}` is not granted"
        )


def _issue_policy(
    rights: PublishRights, count: int
) -> Callable[[issue_outbox.PreparedBatch], None]:
    def check(batch: issue_outbox.PreparedBatch) -> None:
        pending = [a for m in batch.manifests for a in m.actions if a.status != "applied"]
        for action in pending:
            _coverage(rights, _kind(action.action), action.repo)
        if len(pending) > count:
            raise issue_outbox.IssueGateError("pending actions exceed the reserved publish budget")

    return check


def _own_review_repo(cfg: Config, manifest: pr_outbox.Manifest) -> None:
    slug = github_slug(git.get_remote_url(cfg.repo_root, cfg.upstream_remote) or "")
    if slug is None or slug.casefold() != manifest.repo.casefold():
        raise pr_outbox.GateError("PR reviews may target only the own repository")


def _review_policy(
    rights: PublishRights, count: int, cfg: Config, incus: Incus, full: str
) -> Callable[[pr_outbox.Manifest], None]:
    def check(manifest: pr_outbox.Manifest) -> None:
        if pr_outbox.is_source_manifest(manifest) or not rights.pr_reviews:
            raise pr_outbox.GateError("pr_reviews is not granted for this manifest")
        _own_review_repo(cfg, manifest)
        from jailbee.pr_flow import PR_LABEL_PREFIX, STACKED_LABEL_PREFIX

        bound = {entry.pr for entry in branch_pr.BranchPrMap(incus, full).read().values()}
        for label in (PR_LABEL_PREFIX, STACKED_LABEL_PREFIX):
            value = incus.config_get_checked(full, label)
            if value and value.isdigit():
                bound.add(int(value))
        if manifest.pr is not None:
            if manifest.pr not in bound:
                raise pr_outbox.GateError(f"PR #{manifest.pr} is not bound to this container")
        elif len(bound) != 1:
            raise pr_outbox.GateError("review needs exactly one explicitly bound PR")

    return check


def _precheck(
    ctx: ActionContext, m: PublishManifest, rights: PublishRights
) -> tuple[str, list[dict[str, str]], bool]:
    uid = ctx.cfg.container_user.uid
    if m.outbox == "issue":
        snapshot = issue_outbox.read_issue_outbox(ctx.incus, ctx.sender.full, uid=uid)
        targets = issue_outbox.resolve_repo_targets(ctx.cfg)
        refusals: list[str] = []
        proposals = issue_outbox._load_proposals(
            snapshot,
            (m.manifest,),
            targets,
            ctx.cfg.repo_root,
            container_identity(ctx.incus, ctx.sender.full),
            JournalStore(),
            refusals,
        )
        if refusals:
            raise issue_outbox.IssueGateError("; ".join(refusals))
        actions = []
        foreign = False
        for manifest, _, journal in proposals:
            receipts = {a.index: a for a in journal.actions} if journal else {}
            for index, action in enumerate(manifest.actions):
                receipt = receipts.get(index)
                if receipt is not None:
                    if receipt.state != "applied":
                        raise issue_outbox.IssueGateError(
                            "outcome uncertain; resolve with `jb outbox` on the host"
                        )
                    continue
                target = issue_outbox.target_for(targets, action.repo, ctx.cfg.repo_root)
                if target is None:
                    raise issue_outbox.IssueGateError(f"unknown repository {action.repo}")
                _coverage(rights, _kind(action), target)
                actions.append({"kind": _kind(action), "repo": target.slug})
                foreign |= target.foreign
        return "issue", actions, foreign
    snapshot_pr = pr_outbox.read_outbox(ctx.incus, ctx.sender.full, uid=uid, strict=True)
    if m.manifest not in snapshot_pr.files or snapshot_pr.rejected:
        raise pr_outbox.GateError("manifest unavailable or outbox snapshot incomplete")
    manifest_pr = pr_outbox.parse_manifest(
        m.manifest, snapshot_pr.files[m.manifest], snapshot_pr.files
    )
    if pr_outbox.is_source_manifest(manifest_pr):
        if not rights.pull_requests:
            raise pr_outbox.GateError("pull_requests is not granted")
        pending = pr_outbox.source_publication_receipt(snapshot_pr, m.manifest) is None
        return "source", [{"kind": "source", "repo": manifest_pr.repo}] if pending else [], False
    if not rights.pr_reviews:
        raise pr_outbox.GateError("pr_reviews is not granted")
    _own_review_repo(ctx.cfg, manifest_pr)
    progress = pr_outbox._publication_progress(snapshot_pr, m.manifest, len(manifest_pr.actions))
    indices = pr_outbox.pending_indices(manifest_pr, progress)
    return (
        "review",
        [
            {
                "kind": type(manifest_pr.actions[i]).__name__.removesuffix("Action").lower(),
                "repo": manifest_pr.repo,
            }
            for i in indices
        ],
        False,
    )


def known_senders(root: Path) -> set[str]:
    """Instances previously served, so revocation still gets a refusal reply."""
    return set(_load(root, "publish-known.json"))


def remember(ctx: ActionContext) -> None:
    known = _load(ctx.root, "publish-known.json")
    if known.get(ctx.sender.full) != ctx.sender.key:
        known[ctx.sender.full] = ctx.sender.key
        _save(ctx.root, "publish-known.json", known)


def was_accepted(ctx: ActionContext, ref: str) -> bool:
    return ref in _load(ctx.root, "publish-accepted.json").get(ctx.sender.key, [])


def abort_before_start(ctx: ActionContext, ref: str, detail: str) -> None:
    entry = inflight(ctx.root).get(ctx.sender.full)
    if entry is not None and entry["ref"] == ref:
        atomic_write_text(
            _result(ctx.root, entry.get("container_key", ctx.sender.full), ref),
            json.dumps({"ref": ref, "state": "failed", "detail": detail}),
        )


def _audit(root: Path, row: dict[str, Any]) -> None:
    with (root / "publish-log.jsonl").open("a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
    log.info("broker: publish %s", json.dumps(row, sort_keys=True))


def prune(root: Path, identities: dict[str, str], now: datetime) -> None:
    current = inflight(root)
    for full, entry in list(current.items()):
        if identities.get(full) == entry.get("container_key", full):
            continue
        path = _result(root, entry.get("container_key", full), entry["ref"])
        result = json.loads(path.read_text()) if path.exists() else {}
        _audit(root, {"time": now.isoformat(), "orchestrator": full, "ref": entry["ref"],
            "outbox": entry["outbox"], "manifest": entry["manifest"], "actions": result.get("actions", []),
            "urls": result.get("urls", []), "outcome": "abandoned"})
        del current[full]
        path.unlink(missing_ok=True)
    _save(root, "publish-inflight.json", current)
    known = _load(root, "publish-known.json")
    _save(root, "publish-known.json", {full: key for full, key in known.items() if identities.get(full) == key})


def handle_publish(ctx: ActionContext, m: Manifest) -> Outcome:
    assert isinstance(m, PublishManifest)
    outcome = _handle_publish(ctx, m)
    if outcome.state == "refused":
        _audit(ctx.root, {"time": ctx.now.isoformat(), "orchestrator": ctx.sender.full,
            "ref": m.id, "outbox": m.outbox, "manifest": m.manifest, "actions": [], "urls": [],
            "outcome": "refused", "detail": outcome.detail})
    return outcome


def _handle_publish(ctx: ActionContext, m: Manifest) -> Outcome:
    assert isinstance(m, PublishManifest)
    current = inflight(ctx.root)
    if ctx.sender.full in current:
        return refused(f"a publish is already running (ref {current[ctx.sender.full]['ref']})")
    raw = ctx.incus.config_get(ctx.sender.full, labels.GRANT)
    try:
        grant = effective_grant(Grant.from_label(raw), profiles.repo_cap(ctx.cfg)) if raw else None
    except (TypeError, ValueError):
        grant = None
    if ctx.role() != "orchestrator" or grant is None or grant.publish.empty():
        return refused("publishing is not in this container's grant")
    try:
        kind, actions, foreign = _precheck(ctx, m, grant.publish)
        if not actions:
            return done("nothing pending")
        budget = _load(ctx.root, "publish-budget.json")
        cutoff = ctx.now - timedelta(hours=1)
        recent = [t for t in budget.get(ctx.sender.full, []) if datetime.fromisoformat(t) > cutoff]
        if len(recent) + len(actions) > grant.publish.max_actions_per_hour:
            return refused("publish rate cap exhausted; request refused whole")
        budget[ctx.sender.full] = recent + [ctx.now.isoformat()] * len(actions)
        _save(ctx.root, "publish-budget.json", budget)
        current[ctx.sender.full] = {
            "ref": m.id,
            "container_key": ctx.sender.key,
            "outbox": m.outbox,
            "manifest": m.manifest,
            "started_at": ctx.now.isoformat(),
            "actions": actions,
            "kind": kind,
            "foreign": foreign,
            "rights": grant.publish.model_dump(mode="json"),
        }
        _save(ctx.root, "publish-inflight.json", current)
        remember(ctx)
    except Exception as e:
        return refused(str(e))
    return Outcome(
        "started", f"publishing {m.outbox} manifest {m.manifest}: {len(actions)} actions"
    )


def start(ctx: ActionContext, m: PublishManifest) -> threading.Thread | None:
    """Activate only after the broker recorded, replied to and removed the request."""
    entry = inflight(ctx.root).get(ctx.sender.full)
    if entry is not None and entry["ref"] == m.id:
        worker = threading.Thread(
            target=_run,
            args=(ctx.cfg, ctx.root, ctx.sender.full, ctx.sender.short, entry),
            daemon=True,
        )
        worker.start()
        return worker
    return None


def _run(cfg: Config, root: Path, full: str, short: str, entry: dict[str, Any]) -> None:
    lines: list[str] = []
    actual: list[dict[str, str]] = []
    try:
        incus = Incus()
        rights = PublishRights.model_validate(entry["rights"])
        count = len(entry["actions"])
        if entry["kind"] == "source":
            result = branch_pr.publish_source_manifest(
                cfg,
                incus,
                short,
                entry["manifest"],
                force=False,
                yes=True,
                confirm=lambda q: False,
                own_branches_only=True,
                result_sink=lines.append,
                audit_sink=actual.append,
            )
        else:
            policy = (
                _issue_policy(rights, count)
                if entry["kind"] == "issue"
                else _review_policy(rights, count, cfg, incus, full)
            )
            result = publish_selected(
                cfg,
                incus,
                full,
                ProposalId(entry["outbox"], entry["manifest"]),
                journal_store=JournalStore(),
                options=PublishOptions(foreign=entry["foreign"]),
                confirm=lambda n: True,
                confirm_foreign=lambda q: False,
                raise_errors=True,
                policy=policy,
                result_sink=lines.append,
                audit_sink=actual.append,
                action_limit=count,
            )
        if result:
            raise RuntimeError("publication failed; check `jb outbox` on the host")
        state, detail = "done", "\n".join(lines) or "nothing pending"
    except Exception as e:
        state, detail = "failed", str(e)
        if "uncertain" in detail.lower():
            detail += "; resolve with `jb outbox` on the host"
    try:
        atomic_write_text(
            _result(root, entry.get("container_key", full), entry["ref"]),
            json.dumps(
                {
                    "ref": entry["ref"],
                    "state": state,
                    "detail": detail[:4000],
                    "urls": [u for line in lines for u in re.findall(r"https?://[^\s)]+", line)],
                    "actions": actual,
                }
            ),
        )
    except Exception:
        log.exception("broker: could not save publish result for %s %s", full, entry["ref"])


def settle(ctx: ActionContext, reply: Callable[[str, Outcome], None]) -> None:
    current = inflight(ctx.root)
    entry = current.get(ctx.sender.full)
    if entry is None or entry.get("container_key", ctx.sender.key) != ctx.sender.key:
        return
    path = _result(ctx.root, entry.get("container_key", ctx.sender.full), entry["ref"])
    if not path.exists():
        return
    result = json.loads(path.read_text())
    accepted = _load(ctx.root, "publish-accepted.json")
    key = entry.get("container_key", ctx.sender.key)
    refs = accepted.setdefault(key, [])
    if entry["ref"] not in refs:
        refs.append(entry["ref"])
        _save(ctx.root, "publish-accepted.json", accepted)
    audit = {
        "time": ctx.now.isoformat(),
        "orchestrator": ctx.sender.full,
        "ref": entry["ref"],
        "outbox": entry["outbox"],
        "manifest": entry["manifest"],
        "actions": result.get("actions", []),
        "urls": result.get("urls", []),
        "outcome": result["state"],
    }
    if not entry.get("audited"):
        _audit(ctx.root, audit)
        entry["audited"] = True
        _save(ctx.root, "publish-inflight.json", current)
    reply(entry["ref"], Outcome(result["state"], result["detail"]))
    del current[ctx.sender.full]
    _save(ctx.root, "publish-inflight.json", current)
    path.unlink(missing_ok=True)


def recover(root: Path) -> None:
    for full, entry in inflight(root).items():
        path = _result(root, entry.get("container_key", full), entry["ref"])
        if not path.exists():
            atomic_write_text(
                path,
                json.dumps(
                    {
                        "ref": entry["ref"],
                        "state": "failed",
                        "detail": "outcome uncertain: the broker restarted during publish; "
                        "check `jb outbox` on the host",
                    }
                ),
            )


HANDLERS["publish"] = handle_publish
