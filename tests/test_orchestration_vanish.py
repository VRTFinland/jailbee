"""Lifecycle guard at broker.tick and the public state-store boundaries."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.orchestration import actions, publishing, query, socket_ops, state, watch
from jailbee.orchestration.agent_watch import AgentStatus
from jailbee.orchestration.child_status import StatusCache
from jailbee.orchestration.store import BrokerStore
from jailbee.outbox.publication_db import PublicationStore, SentEntry
from jailbee.outbox.store_db import OutboxStore
from tests.test_orchestration_broker import GRANT, Rig, _c
from tests.test_orchestration_socket_device import Env

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import broker, broker_state
from jailbee.orchestration.store import open_state_store
from jailbee.orchestration.journal import Journal

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
FULL = "proj-gone"
UUID = "uuid-gone"

# Every field has an owner and a lifetime. Mixed service objects classify their
# embedded records separately below rather than pretending the object vanishes.
INSTANCE = "per-instance"
GLOBAL = "global"
PERSISTENT = "persistent-with-own-cleanup"
MEMORY_LIFETIMES = {
    "publish_threads": (PERSISTENT, "tick reaps completed workers; shutdown joins live publishers"),
    "env_ensured": (INSTANCE, "tick inventory"),
    "env_roles": (INSTANCE, "tick inventory"),
    "listed_roles": (INSTANCE, "rebuilt from each listing"),
    "warned": (PERSISTENT, "tick prunes instance warnings; repo/security warnings last until restart"),
    "seq": (GLOBAL, "host spool ordering"),
    "status": (INSTANCE, "StatusCache.prune in tick, including idle rounds"),
    "children": (INSTANCE, "tick inventory and valid orchestrator contexts"),
    "contexts": (INSTANCE, "tick inventory"),
    "listener": (GLOBAL, "service; close_container removes vanished connections"),
    "store": (GLOBAL, "database handle; record lifetimes classified below"),
    "queries": (GLOBAL, "service; publish_queries replaces its snapshot each round"),
    "requests": (PERSISTENT, "executor consumes events, including listener disconnects"),
    "attached": (INSTANCE, "check_socket_devices closes vanished sockets"),
    "reassert": (INSTANCE, "tick inventory, even when attach never succeeded"),
    "waiters": (PERSISTENT, "executor consumes DisconnectEvent or evaluate answers the waiter"),
    "last_purge": (GLOBAL, "hourly retention clock"),
    "absent_uuids": (PERSISTENT, "retention.purge_messages replaces on a nonempty all-project inventory"),
    "shared_uuids": (INSTANCE, "rebuilt by check_socket_devices"),
    "reusable_socket_dirs": (INSTANCE, "rebuilt by other_projects"),
    "unsafe_socket_dirs": (GLOBAL, "security tombstones; restart uses durable socket-retired keys"),
    "failures": (INSTANCE, "tick inventory and settle_failures"),
}
STATUS_LIFETIMES = {name: (INSTANCE, "StatusCache.prune") for name in (
    "host_at", "exec_at", "samplers", "agent",
)}
DOCUMENT_LIFETIMES = {
    "reservations.json": (PERSISTENT, "prune_reservations removes only exited create workers"),
    "queue.json": (INSTANCE, "tick drops vanished sender queues"),
    "queue-cancellations": (INSTANCE, "tick purges vanished cancellation senders"),
    "watch.json": (INSTANCE, "forget_vanished_watches before idle return"),
    "publish-inflight.json": (INSTANCE, "publishing.prune audits abandoned publications"),
    "publish-known.json": (INSTANCE, "publishing.prune checks name and UUID"),
    "publish-accepted.json": (INSTANCE, "publishing.prune checks UUID"),
    "publish-budget.json": (INSTANCE, "publishing.prune drops vanished names"),
    "publish-result:": (INSTANCE, "publishing.prune audits orphan results"),
    "socket-retired:": (GLOBAL, "security tombstone: never revive a copied socket generation"),
    "outbox.claim:": (PERSISTENT, "PublicationStore.claim releases token; dead owners recovered on claim"),
}
TABLE_LIFETIMES = {
    "meta": (GLOBAL, "schema and legacy import markers"),
    "state": (GLOBAL, "document lifetimes classified separately"),
    "items": (PERSISTENT, "two hourly absences; young unread items retain repo retention"),
    "requests": (PERSISTENT, "BrokerStore.purge_uuids after two hourly absences"),
    "recipients": (INSTANCE, "check_socket_devices removes vanished routes"),
    "digest": (PERSISTENT, "BrokerStore.purge_uuids after two hourly absences"),
    "journal": (PERSISTENT, "append-only replay protection; retained for host state lifetime"),
    "outbox_proposal": (PERSISTENT, "explicit destroy via OutboxStore.delete_container; uncertain retained"),
    "outbox_body": (PERSISTENT, "delete_container/drop/settled publication, together with proposal"),
    "outbox_counter": (PERSISTENT, "never reuse proposal sequence; retained for host state lifetime"),
    "outbox_draft": (PERSISTENT, "delete_container, submit_draft or discard_draft"),
    "outbox_journal": (PERSISTENT, "archive after resolution; retain audit for host state lifetime"),
    "outbox_journal_action": (PERSISTENT, "publication resolution/archive; retain audit history"),
    "outbox_sent": (PERSISTENT, "PublicationStore.clear_sent is explicit operator cleanup"),
}


def test_idle_round_forgets_vanished_probe_and_diagnostic_state(tmp_path, monkeypatch):
    """The no-work early return must clean caches too, not just busy rounds."""
    memory = broker_state.BrokerMemory()
    memory.status.host_due(FULL, 1.0)
    memory.status.exec_due(FULL, 1.0)
    memory.status.sampler_for(FULL)
    memory.warn_once(f"env:{FULL}", "old instance failed")
    memory.warn_once("repo-load:proj", "repo config failed")
    memory.failures[f"serve:{FULL}"] = "old failure"
    memory.reassert.add(UUID)  # bind/attach failed before attached was recorded
    incus = MagicMock()
    incus.list_containers.return_value = []
    monkeypatch.setattr(broker, "load_global_config", lambda _: (GlobalConfig(), []))

    assert not broker.tick(incus, root=tmp_path, journal=Journal(tmp_path), now=NOW, memory=memory)

    assert memory.status.host_at == {}
    assert memory.status.exec_at == {}
    assert memory.status.samplers == {}
    assert memory.warned == {"repo-load:proj"}
    assert memory.failures == {}
    assert memory.reassert == set()


def test_vanished_publisher_drops_dedup_and_rate_budget(tmp_path):
    store = open_state_store(tmp_path)
    store.kv_put("publish-accepted.json", {UUID: ["old-publish"]})
    store.kv_put("publish-budget.json", {FULL: [NOW.timestamp()]})
    incus = MagicMock()
    incus.list_containers.return_value = []
    memory = broker_state.BrokerMemory()

    assert not broker.tick(incus, root=tmp_path, journal=Journal(tmp_path), now=NOW, memory=memory)

    assert store.kv_get("publish-accepted.json") == {}
    assert store.kv_get("publish-budget.json") == {}


def test_every_memory_field_and_store_document_has_a_lifetime(tmp_path):
    assert {f.name for f in fields(broker_state.BrokerMemory)} == set(MEMORY_LIFETIMES)
    assert {f.name for f in fields(StatusCache)} == set(STATUS_LIFETIMES)
    for classification in (MEMORY_LIFETIMES, STATUS_LIFETIMES, DOCUMENT_LIFETIMES, TABLE_LIFETIMES):
        assert all(kind in {INSTANCE, GLOBAL, PERSISTENT} and reason for kind, reason in classification.values())

    # Guard the schema as well as KV names. Read-only public transactions avoid
    # coupling this inventory to the migration scripts' formatting.
    store = open_state_store(tmp_path)
    with store.transaction(write=False) as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables - {"sqlite_sequence"} == set(TABLE_LIFETIMES)

    source = Path(broker.__file__).parents[1]
    documents = set()
    sql_owners = set()
    # Generic store plumbing takes arbitrary names by design. All other callers
    # are inventoried below, including wrappers and direct SQL claim/result writes.
    indirect_keys = {
        "orchestration/state.py": {"RESERVATIONS_FILE", "QUEUE_FILE", "CANCELLATIONS"},
        "orchestration/watch.py": {"WATCH_FILE"},
        "orchestration/publishing.py": {"name", "key", "_RESULT_PREFIX", "_result_key(full, ref)",
            "_result_key(entry.get('container_key', ctx.sender.full), entry['ref'])"},
        "orchestration/view.py": {"QUEUE_FILE"},
    }
    for path in source.rglob("*.py"):
        relative = path.relative_to(source).as_posix()
        tree = ast.parse(path.read_text())
        if relative == "orchestration/store.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                value = node.value.value
                if isinstance(value, str) and value.endswith(".json") and not value.startswith("."):
                    if relative.startswith("orchestration/"):
                        documents.add(value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"(?:INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+state\b|FROM\s+state\s+WHERE|UPDATE\s+state\s+SET)", node.value, re.I):
                    sql_owners.add(relative)
                if node.value in {"queue-cancellations", "publish-result:", "socket-retired:", "outbox.claim:"}:
                    documents.add(node.value)
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            index = 1 if name in {"import_json_document", "read_state_snapshot"} else 0
            if name not in {"kv_get", "kv_put", "kv_delete", "kv_keys", "update_documents", "import_json_document", "read_state_snapshot"}:
                continue
            key = node.args[index]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                documents.add(key.value)
            elif isinstance(key, ast.JoinedStr):
                assert isinstance(key.values[0], ast.Constant)
                documents.add(key.values[0].value)
            else:
                expression = ast.unparse(key)
                allowed = indirect_keys.get(relative, set())
                if name == "update_documents":
                    assert isinstance(key, (ast.List, ast.Tuple)), (relative, expression)
                    assert {ast.unparse(v) for v in key.elts} <= {"QUEUE_FILE", "CANCELLATIONS", "_BUDGET", "_INFLIGHT"}
                else:
                    assert expression in allowed, (relative, expression)
    assert sql_owners == {"orchestration/publishing.py", "outbox/publication_db.py"}
    assert documents == set(DOCUMENT_LIFETIMES)


@pytest.mark.parametrize("queued", [False, True], ids=["cancellations-only", "queued-and-cancelling"])
def test_vanished_container_has_no_unclassified_surviving_state(tmp_path, monkeypatch, queued):
    env = Env(Rig(tmp_path, monkeypatch))
    env.set(_c(FULL, **{"volatile.uuid": UUID, "user.jailbee.orchestration.grant": GRANT.to_label()}))
    memory, store, root = env.memory, env.store, env.rig.root
    memory.queries = query.Queries(store)
    # Use a real context but no serving mocks: the inventory round is empty.
    node = actions.Node(FULL, "gone", True, {"volatile.uuid": UUID})
    memory.contexts[UUID] = actions.ActionContext(env.rig.cfg, GlobalConfig(), env.rig.incus, root, node, {}, NOW)
    memory.env_ensured.add(UUID)
    memory.env_roles[UUID] = "child"
    memory.listed_roles[UUID] = "child"
    memory.children[FULL] = broker_state.ChildrenView(1.0, [{"name": "child"}])
    memory.status.host_due(FULL, 1.0)
    memory.status.exec_due(FULL, 1.0)
    memory.status.sampler_for(FULL)
    memory.status.agent[FULL] = AgentStatus(True)
    memory.warn_once(f"env:{FULL}", "old warning")
    memory.failures[f"serve:{FULL}"] = "old failure"
    memory.attached[FULL] = UUID
    memory.reassert.add(UUID)
    memory.shared_uuids.add(UUID)
    memory.reusable_socket_dirs.add("old-socket-path")
    memory.unsafe_socket_dirs.add("retired-path")
    memory.absent_uuids.add(UUID)
    memory.last_purge = broker_state.monotonic()  # retention is deliberately not due
    memory.queries.publish_container(UUID, query.ContainerSnapshot("child", True, "{}", None))
    env.listener.open_container(UUID, root / "old-socket")
    store.set_recipient(FULL, UUID, now=1.0)
    memory.waiters.add(socket_ops.Waiter(UUID, 7, None, 0, 0, None))
    # Disconnects and waiters are executor-owned, not blindly discarded by tick.
    from jailbee.orchestration.listener import DisconnectEvent

    event = DisconnectEvent(UUID, 7)
    memory.requests.put(event)
    worker = MagicMock()
    worker.is_alive.return_value = True
    memory.publish_threads.append(worker)

    request = state.QueuedRequest("spawn-1", "proj-child", "new", None, NOW.isoformat(), "branch", None, "task", {})
    state.save_queue(root, {FULL: [request]})
    state.cancel_queued(root, FULL, state.CancelledRequest(request, "cancel-1", UUID, "status-1"))
    if queued:
        state.save_queue(root, {FULL: [request]})
    state.save_reservations(root, {FULL: [state.Reservation("proj-creating", 123, NOW.isoformat())]})
    monkeypatch.setattr(actions, "reservation_alive", lambda _: True)
    watch.save_watch(root, {FULL: [watch.WatchEntry("spawn-1", "proj-child", "spawn", NOW.isoformat())]})
    pending = state.pending_dir(root, FULL)
    pending.mkdir(parents=True)
    (pending / "message.json").write_text('{"type":"message"}')
    result_key = f"publish-result:{hashlib.sha256(UUID.encode()).hexdigest()}-publish-1"
    store.kv_put("publish-inflight.json", {FULL: {"container_key": UUID, "ref": "publish-1", "outbox": "issue", "manifest": "a.json"}})
    store.kv_put("publish-known.json", {FULL: UUID})
    store.kv_put("publish-accepted.json", {UUID: ["publish-1"]})
    store.kv_put("publish-budget.json", {FULL: [NOW.timestamp()]})
    store.kv_put(result_key, {"state": "done", "ref": "publish-1"})
    store.kv_put("socket-retired:retired-path", True)
    item = {"version": 1, "id": "message-1", "type": "message", "kind": "info", "text": "retain unread"}
    store.insert_item(UUID, FULL, item, now=NOW.timestamp())
    store.record_request(UUID, "request-1", digest=None, state="done", detail="ok", item=None, now=NOW.timestamp())
    store.set_digest_at(UUID, NOW.timestamp())
    journal = Journal(root)
    journal.record(UUID, "manifest-digest", "manifest-1", "done", NOW, container_full=FULL)

    proposals = OutboxStore(store)
    proposal = proposals.put_proposal(UUID, "issue", "review", lambda _: ('{"actions":[]}', {"body.md": "body"}), now=1.0)
    proposals.draft_append(UUID, "issue", "draft", {"kind": "comment"}, {}, now=1.0)
    publications = PublicationStore(store)
    with publications.claim(UUID, "issue", proposal.name) as claim:
        publications.begin(claim, digest="digest", action_count=2, container_full=FULL, now=1.0)
        publications.mark_prepared(claim, "digest", 0, repo="org/repo", now=1.0)
        publications.record_sent(SentEntry(UUID, "proj", FULL, "user", "issue", proposal.name, "digest", 0, "comment", "org/repo", 1, None, None, "body"), "applied", now=1.0)
        claim_keys = store.kv_keys("outbox.claim:")
        assert len(claim_keys) == 1
        claim_before = store.kv_get(claim_keys[0])
        env.set()  # the container has vanished, without an explicit destroy
        assert not broker.tick(env.rig.incus, root=root, journal=journal, now=NOW, memory=memory)
        assert store.kv_get(claim_keys[0]) == claim_before  # live external publisher owns it

    for name in ("env_ensured", "env_roles", "listed_roles", "children", "contexts", "attached", "reassert", "shared_uuids", "reusable_socket_dirs", "failures"):
        assert not getattr(memory, name), name
    for name in STATUS_LIFETIMES:
        assert not getattr(memory.status, name), name
    assert not memory.warned
    assert not memory.queries.snapshot()
    assert not env.listener.open
    assert not store.recipients()
    assert not pending.exists()
    assert state.load_queue(root) == {}
    assert state.load_cancellations(root) == []
    assert watch.load_watch(root) == {}
    for name in ("publish-inflight.json", "publish-known.json", "publish-accepted.json", "publish-budget.json"):
        assert store.kv_get(name) == {}, name
    assert store.kv_get(result_key) is None
    assert "abandoned" in (root / "publish-log.jsonl").read_text()
    assert state.load_reservations(root)[FULL][0].pid == 123  # still creating a child
    assert memory.publish_threads == [worker]
    assert memory.requests.get_nowait() == event
    assert len(memory.waiters) == 1
    memory.waiters.drop(UUID, 7)  # executor's DisconnectEvent cleanup
    assert len(memory.waiters) == 0
    assert memory.absent_uuids == {UUID}
    assert memory.unsafe_socket_dirs == {"retired-path"}
    assert store.kv_get("socket-retired:retired-path") is True
    assert store.history(UUID)[0].id == "message-1"
    assert store.lookup_request(UUID, "request-1") is not None
    assert store.get_digest_at(UUID) == NOW.timestamp()
    assert journal.seen(UUID, "manifest-digest")
    assert proposals.get_proposal(UUID, "issue", proposal.name) is not None
    assert proposals.get_draft(UUID, "issue", "draft") is not None
    assert publications.load(UUID, "issue", proposal.name) is not None
    assert publications.sent_for(UUID)
    assert not store.kv_keys("outbox.claim:")  # released by its owner, not by vanish
