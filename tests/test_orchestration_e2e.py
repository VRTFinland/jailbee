"""Socket request dispatch through real handlers, queue and durable store delivery.

Incus, creation and git transport are faked; parsed socket send operations,
status recording, broker maintenance and inbox storage use production code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from jailbee import background
from jailbee.db.models import JOB_CREATE
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.orchestration import actions, broker, broker_state, labels, manifests, socket_ops, watch
from jailbee.orchestration.grant import Grant
from jailbee.orchestration.journal import Journal
from jailbee.orchestration.outbox import CONTAINER_JAILBEE
from jailbee.orchestration.state import load_queue, pending_dir
from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.sync import FetchResult, MergeInContainerResult, PushResult
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
HEAD = "c" * 40


class FakeIncus:
    """Containers, labels and queued socket sends, with inboxes in the real store."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.containers: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, list[dict[str, Any]]] = {}
        self.events: list[tuple[Any, ...]] = []

    def add(self, name: str, *, running: bool, **config: str) -> None:
        self.containers[name] = {
            "name": name,
            "status": "Running" if running else "Stopped",
            "config": {"volatile.uuid": f"uuid-{name}", **config},
        }
        self.requests.setdefault(name, [])

    def set_running(self, name: str, running: bool) -> None:
        self.containers[name]["status"] = "Running" if running else "Stopped"

    # -- the Incus surface the broker uses ---------------------------------

    def list_containers(self) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(c)) for c in self.containers.values()]

    def exists(self, name: str) -> bool:
        return name in self.containers

    def config_get(self, name: str, key: str) -> str | None:
        value = self.containers[name]["config"].get(key)
        return str(value) if value else None

    def config_get_checked(self, name: str, key: str) -> str | None:
        # Like Incus: a missing container is a command failure, not "unset".
        if name not in self.containers:
            raise IncusError(f"instance {name} not found")
        return self.config_get(name, key)

    def config_set(self, name: str, key: str, value: str) -> None:
        self.containers[name]["config"][key] = value

    def config_unset_checked(self, name: str, key: str) -> None:
        self.containers[name]["config"].pop(key, None)

    def stop(self, name: str, force: bool = False, timeout: int | None = None) -> None:
        self.events.append(("stop", name))
        self.set_running(name, False)

    def exec_capped(self, name: str, argv: list[str], **kw: object) -> str:
        if argv[:3] == ["python3", "-I", "-c"]:
            return json.dumps({"issue": [], "pr": []})
        if argv[4] in ("/home/dev/.jailbee/issue-outbox", "/home/dev/.jailbee/pr-outbox"):
            import base64

            return base64.b64encode(bytes(1024)).decode()
        raise AssertionError(f"unexpected capped exec {argv}")

    def exec(self, name: str, argv: list[str], **kw: object) -> str:
        if argv[:2] == ["sh", "-c"] and argv[-1] == "/home/dev/.jailbee/orchestration/role":
            self.events.append(("role", name, kw.get("uid")))
            return ""
        if argv == ["test", "-x", CONTAINER_JAILBEE]:
            return ""
        if "status" in argv and "--porcelain" in argv:
            return ""
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return HEAD + "\n"
        raise AssertionError(f"unexpected exec {argv}")

    def with_exec_timeout(self, seconds: int) -> FakeIncus:
        self.events.append(("bound", seconds))
        return self

    def exec_with_input(self, name: str, argv: list[str], text: str, **kw: object) -> str:
        raise AssertionError(f"container file transport must not run: {argv}")

    def inbox(self, name: str) -> list[tuple[int, dict[str, Any]]]:
        store = BrokerStore(store_path(self.root))
        uuid = self.containers[name]["config"]["volatile.uuid"]
        return [(row.seq, row.item) for row in store.unacked(uuid)]


def _manifest(incus: FakeIncus, full: str, data: dict[str, Any]) -> None:
    incus.requests[full].append(data)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "state"
    root.mkdir()
    cfg = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
    incus = FakeIncus(root)
    grant = Grant.model_validate({"max_running_children": 1})
    incus.add("proj-orch", running=True, **{labels.GRANT: grant.to_label()})
    incus.add(
        "proj-c1",
        running=True,
        **{labels.SPAWNED_BY: "proj-orch", labels.LAST_IMPORT: HEAD},
    )
    from jailbee.create_flow import CreateStarted

    start_create = MagicMock(return_value=CreateStarted("proj-child", 4242, tmp_path / "log"))
    monkeypatch.setattr(actions, "start_create", start_create)
    monkeypatch.setattr(actions, "live_create_jobs", lambda prefix: set())
    monkeypatch.setattr(broker_state, "repo_configs", lambda memory=None: {"proj": cfg})
    monkeypatch.setattr(broker, "load_global_config", lambda path: (GlobalConfig(), []))
    real_serve = manifests.serve
    store = BrokerStore(store_path(root))

    def serve(ctx, journal, memory):
        real_serve(ctx, journal, memory)
        for manifest in list(incus.requests[ctx.sender.full]):
            socket_ops.handle_send(
                ctx,
                memory,
                store,
                manifest=manifest,
                body=None,
                wait=False,
                is_connected=lambda: True,
            )
            incus.requests[ctx.sender.full].remove(manifest)

    monkeypatch.setattr(manifests, "serve", serve)
    return {"root": root, "cfg": cfg, "incus": incus, "start_create": start_create}


def test_spawn_queues_then_starts_after_a_stop(world: dict[str, Any]) -> None:
    root: Path = world["root"]
    incus: FakeIncus = world["incus"]
    journal = Journal(root)
    memory = broker_state.BrokerMemory()
    _manifest(
        incus,
        "proj-orch",
        {
            "version": 1,
            "id": "a-spawn",
            "type": "spawn",
            "kind": "new",
            "branch": "fix/1",
            "prompt": "fix it",
            "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
        },
    )
    _manifest(incus, "proj-orch", {"version": 1, "id": "b-stop", "type": "stop", "child": "c1"})

    # Round 1: the only slot is taken by c1 -> the spawn queues; c1 stops.
    assert broker.tick(incus, root=root, journal=journal, now=T0, memory=memory)  # type: ignore[arg-type]  # fake
    assert world["start_create"].call_count == 1, _statuses(incus, "proj-orch")
    assert load_queue(root) == {}
    assert incus.containers["proj-c1"]["status"] == "Stopped"
    assert incus.containers["proj-c1"]["config"][labels.STOPPED_HEAD] == HEAD
    assert incus.requests["proj-orch"] == []

    # The refreshed cleanup snapshot frees the slot in the same round.
    # Round 2 with a stopped orchestrator does not start it twice.
    incus.set_running("proj-orch", False)
    broker.tick(incus, root=root, journal=journal, now=T0 + timedelta(seconds=5), memory=memory)  # type: ignore[arg-type]  # fake
    world["start_create"].assert_called_once()
    opts = world["start_create"].call_args.args[3]
    assert (opts.name, opts.network, opts.spawned_by) == ("proj-fix-1", "strict", "proj-orch")
    assert load_queue(root) == {}
    assert not list(pending_dir(root, "proj-orch").glob("*.json"))

    # Round 3, orchestrator back: the parked status is flushed after the others.
    incus.set_running("proj-orch", True)
    broker.tick(incus, root=root, journal=journal, now=T0 + timedelta(seconds=10), memory=memory)  # type: ignore[arg-type]  # fake
    inbox = incus.inbox("proj-orch")
    assert [(m["ref"], m["state"]) for _, m in inbox] == [
        ("a-spawn", "queued"),
        ("b-stop", "done"),
        ("a-spawn", "started"),
    ]
    assert inbox[2][1]["detail"] == "creation of child `fix-1` started; on done: destroy"
    assert [name for name, _ in inbox] == sorted(name for name, _ in inbox)
    assert not list(pending_dir(root, "proj-orch").glob("*.json"))

    # A repeated socket request returns the recorded status, without starting twice.
    ctx = memory.contexts["uuid-proj-orch"]
    store = BrokerStore(store_path(root))
    old = store.lookup_request(ctx.sender.key, "a-spawn")
    assert old is not None and old.state == "queued"
    result = socket_ops.handle_send(
        ctx,
        memory,
        store,
        manifest={"id": "a-spawn"},
        body=None,
        wait=False,
        is_connected=lambda: True,
    )
    assert result["state"] == "queued"
    world["start_create"].assert_called_once()


DONE_SHA = "d" * 40
IMPORT_SHA = "e" * 40
IMPORT_REF = "refs/jailbee/from/c1/c1-branch"


def _tick(world: dict[str, Any], memory: broker_state.BrokerMemory, seconds: int = 0) -> None:
    broker.tick(
        world["incus"],
        root=world["root"],
        journal=Journal(world["root"]),
        now=T0 + timedelta(seconds=seconds),
        memory=memory,
    )


def _statuses(incus: FakeIncus, name: str) -> list[tuple[str, str, str]]:
    return [
        (m["ref"], m["state"], m["detail"]) for _, m in incus.inbox(name) if m["type"] == "status"
    ]


def _messages(incus: FakeIncus, name: str) -> list[dict[str, Any]]:
    return [m for _, m in incus.inbox(name) if m["type"] == "message"]


def test_orchestrator_message_reaches_a_running_child(world: dict[str, Any]) -> None:
    incus: FakeIncus = world["incus"]
    _manifest(
        incus,
        "proj-orch",
        {"version": 1, "id": "m-1", "type": "message", "to": "c1", "body": "look at X"},
    )
    _tick(world, broker_state.BrokerMemory())
    [msg] = _messages(incus, "proj-c1")
    assert (msg["from"], msg["body"], msg["kind"]) == ("orch", "look at X", "note")
    assert _statuses(incus, "proj-orch") == [("m-1", "done", "delivered to c1")]


def test_orchestrator_message_to_a_stopped_child_is_stored_without_start(
    world: dict[str, Any],
) -> None:
    root: Path = world["root"]
    incus: FakeIncus = world["incus"]
    memory = broker_state.BrokerMemory()
    incus.set_running("proj-c1", False)
    _manifest(
        incus,
        "proj-orch",
        {"version": 1, "id": "m-1", "type": "message", "to": "c1", "body": "later"},
    )
    _tick(world, memory)
    assert _messages(incus, "proj-c1")[0]["body"] == "later"
    assert not pending_dir(root, "proj-c1").exists()
    assert _statuses(incus, "proj-orch")[0][1] == "done"
    assert BrokerStore(store_path(root)).recipients() == {}

    incus.set_running("proj-c1", True)
    _tick(world, memory, 5)
    [msg] = _messages(incus, "proj-c1")
    assert msg["body"] == "later"
    assert not list(pending_dir(root, "proj-c1").glob("*.json"))


@pytest.fixture
def git_done(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The git side of a child's `done`: clean tree on branch c1-branch at DONE_SHA."""
    from jailbee import lifecycle, sync

    monkeypatch.setattr(lifecycle, "container_repo_dir", lambda cfg, incus, full: "/repo")
    monkeypatch.setattr(sync, "_container_status_dirty", lambda *a, **k: False)
    monkeypatch.setattr(sync, "_container_current_branch", lambda *a, **k: "c1-branch")
    monkeypatch.setattr(actions, "_child_head", lambda *a, **k: DONE_SHA)
    fetch = MagicMock(
        return_value=FetchResult(
            branch="c1-branch", old_oid=None, new_oid=DONE_SHA, base_oid=None, commits_added=1
        )
    )
    monkeypatch.setattr(sync, "fetch_from_container", fetch)
    push = PushResult(
        source="c1-branch",
        source_ref="refs/jailbee/c1/c1-branch",
        container_ref=IMPORT_REF,
        old_oid=None,
        new_oid=DONE_SHA,
    )
    monkeypatch.setattr(
        sync,
        "merge_container_into_container",
        MagicMock(
            return_value=MergeInContainerResult(
                push=push, container_branch="main", fast_forward_only=False, head_oid=HEAD
            )
        ),
    )
    return fetch


def _done_manifest(incus: FakeIncus) -> None:
    _manifest(
        incus,
        "proj-c1",
        {"version": 1, "id": "d-1", "type": "message", "kind": "done", "body": "finished"},
    )


def test_child_done_while_orchestrator_runs(world: dict[str, Any], git_done: MagicMock) -> None:
    incus: FakeIncus = world["incus"]
    _done_manifest(incus)
    _tick(world, broker_state.BrokerMemory())
    config = incus.containers["proj-c1"]["config"]
    assert (config[labels.DONE_SHA], config[labels.STATE]) == (DONE_SHA, labels.STATE_IMPORTED)
    [msg] = _messages(incus, "proj-orch")
    assert (msg["from"], msg["kind"], msg["body"]) == ("c1", "done", "finished")
    assert _statuses(incus, "proj-c1") == [
        ("d-1", "done", f"imported into orch at {DONE_SHA[:12]}; this container is kept")
    ]
    assert ("bound", actions.CHILD_EXEC_TIMEOUT_S) in incus.events
    assert git_done.call_args.kwargs["timeout"] == actions.CHILD_TRANSPORT_TIMEOUT_S


def test_child_done_while_orchestrator_is_stopped(
    world: dict[str, Any], git_done: MagicMock
) -> None:
    root: Path = world["root"]
    incus: FakeIncus = world["incus"]
    incus.set_running("proj-orch", False)
    _done_manifest(incus)
    _tick(world, broker_state.BrokerMemory())
    config = incus.containers["proj-c1"]["config"]
    assert (config[labels.DONE_SHA], config[labels.STATE]) == (DONE_SHA, labels.STATE_DONE)
    assert _messages(incus, "proj-orch") == []
    assert len(list(pending_dir(root, "proj-orch").iterdir())) == 1
    [(ref, state, detail)] = _statuses(incus, "proj-c1")
    assert (ref, state) == ("d-1", "queued")
    assert DONE_SHA[:12] in detail


def test_orchestrator_import_records_and_reports_the_ref(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee import sync

    incus: FakeIncus = world["incus"]
    push = PushResult(
        source="c1-branch",
        source_ref="refs/jailbee/c1/c1-branch",
        container_ref=IMPORT_REF,
        old_oid=None,
        new_oid=IMPORT_SHA,
    )
    merge = MagicMock(
        return_value=MergeInContainerResult(
            push=push, container_branch="main", fast_forward_only=False, head_oid="f" * 40
        )
    )
    monkeypatch.setattr(sync, "merge_container_into_container", merge)
    _manifest(incus, "proj-orch", {"version": 1, "id": "i-1", "type": "import", "child": "c1"})
    _tick(world, broker_state.BrokerMemory())
    config = incus.containers["proj-c1"]["config"]
    assert (config[labels.LAST_IMPORT], config[labels.STATE]) == (
        IMPORT_SHA,
        labels.STATE_IMPORTED,
    )
    [(ref, state, detail)] = _statuses(incus, "proj-orch")
    assert (ref, state) == ("i-1", "done")
    assert IMPORT_REF in detail
    assert merge.call_args.kwargs["plain"] is True
    assert merge.call_args.kwargs["ff_local_branch"] is False


def test_spawn_fails_when_alive_child_has_no_launcher(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.incus import IncusError
    from jailbee.orchestration import agent_watch

    incus: FakeIncus = world["incus"]
    incus.set_running("proj-c1", False)
    memory = broker_state.BrokerMemory()
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    original_exec = incus.exec

    def exec_missing(name: str, argv: list[str], **kw: object) -> str:
        if name == "proj-k1" and argv == ["test", "-x", CONTAINER_JAILBEE]:
            raise IncusError("missing")
        if name == "proj-k1" and argv[:2] == ["bash", "-lc"]:
            return ""
        return original_exec(name, argv, **kw)

    monkeypatch.setattr(incus, "exec", exec_missing)
    _manifest(
        incus,
        "proj-orch",
        {
            "version": 1,
            "id": "sp1",
            "type": "spawn",
            "kind": "new",
            "branch": "k1",
            "prompt": "go",
            "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
        },
    )
    _tick(world, memory)
    incus.add("proj-k1", running=True, **{labels.SPAWNED_BY: "proj-orch"})
    _tick(world, memory, 5)
    _tick(world, memory, 10)
    assert _statuses(incus, "proj-orch") == [
        ("sp1", "started", "creation of child `k1` started; on done: destroy"),
        (
            "sp1",
            "failed",
            "container image has no in-container jailbee (needed for `jailbee inbox`) — "
            "run `jb base build` on the host, then recreate the container",
        ),
    ]


def test_spawn_reports_ready_after_worker_finishes(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    incus: FakeIncus = world["incus"]
    incus.set_running("proj-c1", False)  # frees the only slot
    memory = broker_state.BrokerMemory()
    jobs: dict[str, watch.JobSnapshot] = {
        "proj-k1": watch.JobSnapshot("clone", 4242, JOB_CREATE, None, T0 + timedelta(seconds=1))
    }
    monkeypatch.setattr(watch, "read_jobs", lambda: jobs)
    monkeypatch.setattr(background, "worker_alive", lambda pid: True)
    _manifest(
        incus,
        "proj-orch",
        {
            "version": 1,
            "id": "sp1",
            "type": "spawn",
            "kind": "new",
            "branch": "k1",
            "prompt": "go",
            "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
        },
    )

    _tick(world, memory)  # round 1: started, worker still running
    assert _statuses(incus, "proj-orch") == [
        ("sp1", "started", "creation of child `k1` started; on done: destroy")
    ]

    from jailbee.orchestration import agent_watch

    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    jobs.clear()  # the worker finished and removed its row
    incus.add("proj-k1", running=True, **{labels.SPAWNED_BY: "proj-orch"})
    _tick(world, memory, 5)  # first live observation is not readiness
    assert len(_statuses(incus, "proj-orch")) == 1
    _tick(world, memory, 10)
    assert _statuses(incus, "proj-orch")[-1] == (
        "sp1",
        "done",
        "child `k1` is ready; agent is alive",
    )
    _tick(world, memory, 15)  # monitoring continues without extra ready messages
    assert len(_statuses(incus, "proj-orch")) == 2
    assert watch.load_watch(world["root"])["proj-orch"][0].confirmed
