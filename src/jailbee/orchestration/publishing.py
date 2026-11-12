"""Request-driven host publication with durable budget and asynchronous results."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee import branch_pr, git, issue_manifest, issue_outbox, pr_outbox
from jailbee.github_repo import github_slug
from jailbee.incus import Incus
from jailbee.orchestration import labels, profiles
from jailbee.orchestration.actions import HANDLERS, ActionContext, Outcome, done, refused
from jailbee.orchestration.grant import Grant, PublishRights, effective_grant
from jailbee.orchestration.protocol import Manifest, PublishManifest
from jailbee.orchestration.state import atomic_write_text
from jailbee.orchestration.store import BrokerStore, import_json_document, open_state_store
from jailbee.outbox.models import OutboxError, ProposalId
from jailbee.outbox.publish import PublishOptions, publish_selected
from jailbee.outbox_io import ContainerIdentity

if TYPE_CHECKING:
    from jailbee.config import Config

log = logging.getLogger(__name__)

# Legacy filenames also serve as store keys and import markers.
_INFLIGHT = "publish-inflight.json"
_KNOWN = "publish-known.json"
_ACCEPTED = "publish-accepted.json"
_BUDGET = "publish-budget.json"


def _validate_document(document: object) -> None:
    if not isinstance(document, dict):
        raise ValueError("expected an object")


def _load(root: Path, name: str) -> dict[str, Any]:
    value = import_json_document(root, name, _validate_document).kv_get(name)
    return value if isinstance(value, dict) else {}


def _save(root: Path, name: str, data: dict[str, Any]) -> None:
    import_json_document(root, name, _validate_document, for_write=True).kv_put(name, data)


_RESULT_PREFIX = "publish-result:"


def _result_key(full: str, ref: str) -> str:
    return _RESULT_PREFIX + f"{hashlib.sha256(full.encode()).hexdigest()}-{ref}"


def _results_store(root: Path) -> BrokerStore:
    store = open_state_store(root)
    directory = root / "publish-results"

    def load(con: sqlite3.Connection) -> None:
        if not directory.exists():
            return
        for path in directory.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(value, dict)
                    or value.get("state") not in ("done", "failed")
                    or not isinstance(value.get("detail"), str)
                ):
                    raise ValueError("invalid result state or detail")
            except (ValueError, UnicodeError) as exc:
                log.warning("ignoring corrupt publish result %s: %s", path, exc)
                try:
                    path.replace(path.with_name(path.name + ".corrupt"))
                except OSError:
                    log.exception("cannot quarantine publish result %s", path)
                    raise
                continue
            con.execute(
                "INSERT OR IGNORE INTO state(key, value) VALUES (?, ?)",
                (_RESULT_PREFIX + path.stem, json.dumps(value)),
            )

    store.import_once("publish-results", load)
    # TODO(state-db-cleanup): retain imported results for one release.
    try:
        directory.replace(directory.with_name(directory.name + ".migrated"))
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("cannot rename migrated publish results %s: %s", directory, exc)
    return store


def _get_result(root: Path, full: str, ref: str) -> dict[str, Any] | None:
    value = _results_store(root).kv_get(_result_key(full, ref))
    return value if isinstance(value, dict) else None


def _put_result(root: Path, full: str, ref: str, value: dict[str, Any]) -> None:
    _results_store(root).kv_put(_result_key(full, ref), value)


def inflight(root: Path) -> dict[str, Any]:
    return _load(root, _INFLIGHT)


def _kind(action: issue_manifest.IssueAction) -> str:
    return type(action).__name__.removesuffix("Action").lower()


def _needed_kind(action: issue_manifest.IssueAction, batch: issue_outbox.PreparedBatch) -> str:
    """The grant kind ``action`` needs; resolves ``@me`` before comparing logins.

    `assign_self` only when every login touched is the (non-empty) publisher
    login, compared case-insensitively; anything else, or no known login, needs `assign`.
    """
    if not isinstance(action, issue_manifest.AssigneesAction):
        return _kind(action)
    login = batch.login
    if not login:
        return "assign"
    add, remove = issue_outbox.resolved_assignees(action, login)
    mine = login.casefold()
    logins = (*add, *remove)
    return "assign_self" if logins and all(x.casefold() == mine for x in logins) else "assign"


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
            _coverage(rights, _needed_kind(action.action, batch), action.repo)
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

        entries = branch_pr.BranchPrMap(incus, full).read().values()
        bound = {entry.pr for entry in entries}
        authored = {entry.pr for entry in entries if entry.author and not entry.adopted}
        for label in (PR_LABEL_PREFIX, STACKED_LABEL_PREFIX):
            value = incus.config_get_checked(full, label)
            if value and value.isdigit():
                bound.add(int(value))
        if manifest.pr is not None:
            if manifest.pr not in bound:
                raise pr_outbox.GateError(f"PR #{manifest.pr} is not bound to this container")
        elif len(bound) != 1:
            raise pr_outbox.GateError("review needs exactly one explicitly bound PR")
        target_number = manifest.pr if manifest.pr is not None else next(iter(bound))
        if (
            any(isinstance(action, pr_outbox.DescriptionAction) for action in manifest.actions)
            and target_number not in authored
        ):
            raise pr_outbox.GateError(
                "description rewrite requires an authored, non-adopted PR binding"
            )

    return check


def _precheck(
    ctx: ActionContext, m: PublishManifest, rights: PublishRights
) -> tuple[str, list[dict[str, str]], bool]:
    if m.outbox == "issue":
        # Issue proposals and their journals live in the host outbox store,
        # keyed by the sender's uuid: no `incus exec` into the orchestrator.
        uuid = ctx.sender.key
        snapshot = issue_outbox.store_outbox(uuid)
        targets = issue_outbox.resolve_repo_targets(ctx.cfg)
        refusals: list[str] = []
        proposals = issue_outbox._load_proposals(
            snapshot,
            (m.manifest,),
            targets,
            ctx.cfg.repo_root,
            issue_outbox.store_journals(ContainerIdentity(ctx.sender.full, ""), uuid),
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
                # The login is only known on the host in the locked policy
                # check, which is strict; this early check needs any assign right.
                needed = "assign_self" if _kind(action) == "assignees" else _kind(action)
                _coverage(rights, needed, target)
                actions.append({"kind": _kind(action), "repo": target.slug})
                foreign |= target.foreign
        return "issue", actions, foreign
    # PR source proposals live in the host outbox store, keyed by the sender's
    # uuid: no `incus exec` into the orchestrator.
    try:
        source = branch_pr.stored_source(ctx.sender.key, m.manifest)
    except pr_outbox.ManifestError:
        source = None  # a stored review/comment/description proposal, gated below
    if source is not None:
        if not rights.pull_requests:
            raise pr_outbox.GateError("pull_requests is not granted")
        recorded = branch_pr.source_journal(ctx.sender.key, m.manifest)
        if recorded is not None and recorded.uncertain:
            raise pr_outbox.GateError("outcome uncertain; resolve with `jb pr push` on the host")
        pending = recorded is None or recorded.digest != source.digest or not recorded.complete
        return (
            "source",
            [{"kind": "source", "repo": source.manifest.repo}] if pending else [],
            False,
        )
    # Every PR proposal lives in the host outbox store, keyed by the sender's uuid.
    snapshot_pr = pr_outbox.store_outbox(ctx.sender.key, container=ctx.sender.full)
    if m.manifest not in snapshot_pr.files or snapshot_pr.rejected:
        raise pr_outbox.GateError("manifest unavailable or outbox snapshot incomplete")
    manifest_pr = pr_outbox.parse_manifest(
        m.manifest, snapshot_pr.files[m.manifest], snapshot_pr.files
    )
    if pr_outbox.is_source_manifest(manifest_pr):
        # stored_source accepted every valid source above; this one is not valid.
        raise pr_outbox.GateError("invalid source manifest")
    if not rights.pr_reviews:
        raise pr_outbox.GateError("pr_reviews is not granted")
    _own_review_repo(ctx.cfg, manifest_pr)
    try:
        progress = pr_outbox._publication_progress(
            snapshot_pr, m.manifest, len(manifest_pr.actions)
        )
    except OutboxError as exc:
        raise pr_outbox.GateError(f"{exc}; resolve it on the host") from exc
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
    return set(_load(root, _KNOWN))


def remember(ctx: ActionContext) -> None:
    known = _load(ctx.root, _KNOWN)
    if known.get(ctx.sender.full) != ctx.sender.key:
        known[ctx.sender.full] = ctx.sender.key
        _save(ctx.root, _KNOWN, known)


def was_accepted(ctx: ActionContext, ref: str) -> bool:
    return ref in _load(ctx.root, _ACCEPTED).get(ctx.sender.key, [])


def abort_before_start(ctx: ActionContext, ref: str, detail: str) -> None:
    entry = inflight(ctx.root).get(ctx.sender.full)
    if entry is not None and entry["ref"] == ref:
        _put_result(
            ctx.root,
            entry.get("container_key", ctx.sender.full),
            ref,
            {"ref": ref, "state": "failed", "detail": detail},
        )


# The audit log is append-only, and uncertain reviews require human resolution.
# Both remain human-facing files rather than broker state documents.
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
        key = _result_key(entry.get("container_key", full), entry["ref"])
        result = _get_result(root, entry.get("container_key", full), entry["ref"]) or {}
        _audit(
            root,
            {
                "time": now.isoformat(),
                "orchestrator": full,
                "ref": entry["ref"],
                "outbox": entry["outbox"],
                "manifest": entry["manifest"],
                "actions": result.get("actions", []),
                "urls": result.get("urls", []),
                "outcome": "abandoned",
            },
        )
        del current[full]
        _results_store(root).kv_delete(key)
    _save(root, _INFLIGHT, current)
    active_results = {
        _result_key(entry.get("container_key", full), entry["ref"])
        for full, entry in current.items()
    }
    store = _results_store(root)
    for key in store.kv_keys(_RESULT_PREFIX):
        if key in active_results:
            continue
        value = store.kv_get(key)
        result = value if isinstance(value, dict) else {}
        _audit(
            root,
            {
                "time": now.isoformat(),
                "orchestrator": result.get("orchestrator", "unknown"),
                "ref": result.get("ref"),
                "actions": result.get("actions", []),
                "urls": result.get("urls", []),
                "outcome": "abandoned-result",
            },
        )
        store.kv_delete(key)
    known = _load(root, _KNOWN)
    _save(
        root,
        _KNOWN,
        {full: key for full, key in known.items() if identities.get(full) == key},
    )
    live_keys = set(identities.values())
    accepted = _load(root, _ACCEPTED)
    _save(root, _ACCEPTED, {key: refs for key, refs in accepted.items() if key in live_keys})
    budget = _load(root, _BUDGET)
    _save(root, _BUDGET, {full: stamps for full, stamps in budget.items() if full in identities})


def handle_publish(ctx: ActionContext, m: Manifest) -> Outcome:
    assert isinstance(m, PublishManifest)
    outcome = _handle_publish(ctx, m)
    if outcome.state == "refused":
        _audit(
            ctx.root,
            {
                "time": ctx.now.isoformat(),
                "orchestrator": ctx.sender.full,
                "ref": m.id,
                "outbox": m.outbox,
                "manifest": m.manifest,
                "actions": [],
                "urls": [],
                "outcome": "refused",
                "detail": outcome.detail,
            },
        )
    return outcome


def _review_marker(root: Path, key: str, name: str) -> Path:
    digest = hashlib.sha256(f"{key}\0{name}".encode()).hexdigest()
    return root / "publish-review-uncertain" / f"{digest}.json"


def _handle_publish(ctx: ActionContext, m: Manifest) -> Outcome:
    assert isinstance(m, PublishManifest)
    if m.outbox == "pr" and _review_marker(ctx.root, ctx.sender.key, m.manifest).exists():
        return refused(
            "outcome uncertain: review publication requires human resolution "
            "with `jb outbox` on the host"
        )
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
        budget = _load(ctx.root, _BUDGET)
        cutoff = ctx.now - timedelta(hours=1)
        recent = [t for t in budget.get(ctx.sender.full, []) if datetime.fromisoformat(t) > cutoff]
        if len(recent) + len(actions) > grant.publish.max_actions_per_hour:
            return refused("publish rate cap exhausted; request refused whole")
        budget[ctx.sender.full] = recent + [ctx.now.isoformat()] * len(actions)
        _save(ctx.root, _BUDGET, budget)
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
        _save(ctx.root, _INFLIGHT, current)
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
    receipts: list[dict[str, object]] = []
    refused_by_policy = False
    try:
        incus = Incus()
        key = entry.get("container_key", full)
        if key != full:
            incus.guard_instance(full, key)
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
                publisher="orchestrator",
            )
        else:
            if entry["kind"] == "review":
                atomic_write_text(
                    _review_marker(root, entry.get("container_key", full), entry["manifest"]),
                    json.dumps({"ref": entry["ref"], "manifest": entry["manifest"]}),
                )
            policy = (
                _issue_policy(rights, count)
                if entry["kind"] == "issue"
                else _review_policy(rights, count, cfg, incus, full)
            )
            inner = policy

            def policy(batch: Any) -> None:
                # A policy refusal ran before any GitHub mutation; settle() refunds it.
                nonlocal refused_by_policy
                try:
                    inner(batch)
                except Exception:
                    refused_by_policy = True
                    raise

            result = publish_selected(
                cfg,
                incus,
                full,
                ProposalId(entry["outbox"], entry["manifest"]),
                options=PublishOptions(foreign=entry["foreign"], publisher="orchestrator"),
                confirm=lambda n: True,
                confirm_foreign=lambda q: False,
                raise_errors=True,
                policy=policy,
                result_sink=lines.append,
                audit_sink=actual.append,
                receipt_sink=receipts.append,
                action_limit=count,
            )
        if result:
            raise RuntimeError("publication failed; check `jb outbox` on the host")
        state, detail = "done", "\n".join(lines) or "nothing pending"
        if entry["kind"] == "review":
            _review_marker(root, entry.get("container_key", full), entry["manifest"]).unlink(
                missing_ok=True
            )
    except Exception as e:
        state, detail = "failed", str(e)
        if "uncertain" in detail.lower():
            detail += "; resolve with `jb outbox` on the host"
    try:
        _put_result(
            root,
            entry.get("container_key", full),
            entry["ref"],
            {
                "ref": entry["ref"],
                "orchestrator": full,
                "state": state,
                "detail": detail[:4000],
                "urls": [u for line in lines for u in re.findall(r"https?://[^\s)]+", line)],
                "actions": actual,
                "results": receipts,
                "policy_refused": refused_by_policy,
            },
        )
    except Exception:
        log.exception("broker: could not save publish result for %s %s", full, entry["ref"])


def _refund(root: Path, full: str, entry: dict[str, Any]) -> None:
    """Commit the refund and its idempotency marker together."""
    for name in (_BUDGET, _INFLIGHT):
        import_json_document(root, name, _validate_document, for_write=True)

    def apply(docs: dict[str, dict[str, Any]]) -> bool:
        current = docs[_INFLIGHT]
        saved = current.get(full)
        if saved is None or saved.get("ref") != entry["ref"] or saved.get("refunded"):
            return False
        budget = docs[_BUDGET]
        stamps = list(budget.get(full, []))
        for _ in saved["actions"]:
            if saved["started_at"] in stamps:
                stamps.remove(saved["started_at"])
        budget[full] = stamps
        saved["refunded"] = True
        return True

    if not open_state_store(root).update_documents((_BUDGET, _INFLIGHT), apply):
        return
    entry["refunded"] = True


def settle(ctx: ActionContext, reply: Callable[[str, Outcome], None]) -> None:
    current = inflight(ctx.root)
    entry = current.get(ctx.sender.full)
    if entry is None or entry.get("container_key", ctx.sender.key) != ctx.sender.key:
        return
    result = _get_result(ctx.root, entry.get("container_key", ctx.sender.full), entry["ref"])
    if result is None:
        return
    accepted = _load(ctx.root, _ACCEPTED)
    key = entry.get("container_key", ctx.sender.key)
    refs = accepted.setdefault(key, [])
    if entry["ref"] not in refs:
        refs.append(entry["ref"])
        _save(ctx.root, _ACCEPTED, accepted)
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
        _save(ctx.root, _INFLIGHT, current)
    if result.get("policy_refused") and not entry.get("refunded"):
        _refund(ctx.root, ctx.sender.full, entry)
    reply(entry["ref"], Outcome(result["state"], result["detail"], result.get("results", [])))
    del current[ctx.sender.full]
    _save(ctx.root, _INFLIGHT, current)
    _results_store(ctx.root).kv_delete(
        _result_key(entry.get("container_key", ctx.sender.full), entry["ref"])
    )


def recover(root: Path) -> None:
    for full, entry in inflight(root).items():
        key = entry.get("container_key", full)
        if _get_result(root, key, entry["ref"]) is None:
            if entry.get("kind") == "review":
                atomic_write_text(
                    _review_marker(root, entry.get("container_key", full), entry["manifest"]),
                    json.dumps({"ref": entry["ref"]}),
                )
            _put_result(
                root,
                key,
                entry["ref"],
                {
                    "ref": entry["ref"],
                    "state": "failed",
                    "detail": "outcome uncertain: the broker restarted during publish; "
                    "check `jb outbox` on the host",
                },
            )


HANDLERS["publish"] = handle_publish
