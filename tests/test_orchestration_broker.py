from __future__ import annotations

import hashlib
import json
import logging
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, MagicMock
from unittest.mock import call as mock_call

import pytest

from jailbee import background
from jailbee.db.models import JOB_BOOT, JOB_CREATE
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.orchestration import actions, broker, labels, outbox, watch
from jailbee.orchestration.actions import Outcome
from jailbee.orchestration.grant import Grant, RepoCap
from jailbee.orchestration.journal import Journal
from jailbee.orchestration.protocol import InboxStatus, ManifestError, parse_manifest
from jailbee.orchestration.state import QueuedRequest, load_queue, save_queue
from jailbee.orchestration.store import BrokerStore, store_path
from tests.conftest import make_config


@dataclass
class Request:
    file_name: str
    text: str
    files: dict[str, str]


@pytest.fixture(autouse=True)
def _serve_requests_through_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_broker waits via the executor; these tests drive that wait through `_pause`."""
    import time

    from jailbee.orchestration import executor

    def serve(memory: Any, *, until: float, stop: Any) -> None:
        broker._pause(round(until - time.monotonic(), 2), stop)

    monkeypatch.setattr(executor, "serve_requests", serve)


if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
GRANT = Grant.model_validate(
    {"max_running_children": 2, "operations": ["message", "spawn", "stop"]}
)


def _c(name: str, *, running: bool = True, **config: str) -> dict[str, Any]:
    return {"name": name, "status": "Running" if running else "Stopped", "config": config}


def _orch(name: str = "proj-orch", *, running: bool = True, grant: Grant = GRANT) -> dict[str, Any]:
    return _c(name, running=running, **{labels.GRANT: grant.to_label()})


def _child(
    name: str = "proj-kid", *, running: bool = True, of: str = "proj-orch"
) -> dict[str, Any]:
    return _c(name, running=running, **{labels.SPAWNED_BY: of})


@pytest.mark.parametrize("revoked", [False, True])
def test_tick_delivers_publish_result(tmp_path, monkeypatch, revoked):
    from jailbee.orchestration.state import atomic_write_text

    rig = Rig(tmp_path, monkeypatch)
    rig.incus.list_containers.return_value = [_c("proj-orch") if revoked else _orch()]
    atomic_write_text(
        rig.root / "publish-inflight.json",
        json.dumps(
            {"proj-orch": {"ref": "pub-1", "outbox": "issue", "manifest": "a.json", "actions": []}}
        ),
    )
    atomic_write_text(
        rig.root / "publish-results" / f"{hashlib.sha256(b'proj-orch').hexdigest()}-pub-1.json",
        json.dumps({"state": "done", "detail": "https://github.com/org/repo/issues/1"}),
    )
    rig.tick()
    assert [(s.ref, s.state) for _, s in rig.statuses()] == [("pub-1", "done")]
    rig.tick()
    assert len(rig.statuses()) == 1


def test_corrupt_publish_pruning_does_not_stop_tick(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.incus.list_containers.return_value = [_orch()]
    (rig.root / "publish-known.json").write_text("broken json")
    seen = []
    monkeypatch.setattr(broker, "_serve", lambda ctx, *args: seen.append(ctx.sender.full))
    rig.tick()
    assert seen == ["proj-orch"]


def test_former_publisher_failure_does_not_starve_later_node(tmp_path, monkeypatch):
    from jailbee.orchestration.state import atomic_write_text

    rig = Rig(tmp_path, monkeypatch)
    rig.incus.list_containers.return_value = [_c("proj-old"), _orch()]
    atomic_write_text(rig.root / "publish-known.json", json.dumps({"proj-old": "proj-old"}))
    seen = []

    def serve(ctx, *args):
        seen.append(ctx.sender.full)
        if ctx.sender.full == "proj-old":
            raise IncusError("read-only outbox")

    monkeypatch.setattr(broker, "_serve", serve)
    rig.tick()
    assert seen == ["proj-old", "proj-orch"]


def test_serving_grant_without_publish_does_not_remember_sender(tmp_path, monkeypatch):
    from jailbee.orchestration import publishing

    rig = Rig(tmp_path, monkeypatch)
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick()
    assert publishing.known_senders(rig.root) == set()


def test_revoked_orchestrator_new_publish_gets_refusal(tmp_path, monkeypatch):
    from jailbee.orchestration.state import atomic_write_text

    rig = Rig(tmp_path, monkeypatch)
    rig.incus.list_containers.return_value = [_c("proj-orch")]
    atomic_write_text(rig.root / "publish-known.json", json.dumps({"proj-orch": "proj-orch"}))
    data = {
        "version": 1,
        "id": "new-pub",
        "type": "publish",
        "outbox": "issue",
        "manifest": "a.json",
    }
    rig.requests["proj-orch"] = [Request("new-pub.json", json.dumps(data), {})]
    rig.dispatch.side_effect = actions.dispatch
    rig.tick()
    assert [(s.ref, s.state) for _, s in rig.statuses()] == [("new-pub", "refused")]


def test_restart_before_journal_does_not_publish_request_again(tmp_path, monkeypatch):
    from jailbee.orchestration import publishing
    from jailbee.orchestration.protocol import PublishManifest

    rig = Rig(tmp_path, monkeypatch)
    grant = Grant.model_validate({"publish": {"issues": ["comment"]}})
    rig.incus.config_get.return_value = grant.to_label()
    rig.incus.list_containers.return_value = [_orch(grant=grant)]
    data = {"version": 1, "id": "pub-1", "type": "publish", "outbox": "issue", "manifest": "a.json"}
    raw = Request("pub-1.json", json.dumps(data), {})
    rig.requests["proj-orch"] = [raw]
    ctx = actions.ActionContext(
        rig.cfg,
        GlobalConfig(),
        rig.incus,
        rig.root,
        actions.Node("proj-orch", "orch", True, {labels.GRANT: grant.to_label()}),
        {},
        NOW,
    )
    monkeypatch.setattr(
        publishing,
        "_precheck",
        lambda *a: ("issue", [{"kind": "comment", "repo": "org/repo"}], False),
    )
    assert publishing.handle_publish(ctx, PublishManifest.model_validate(data)).state == "started"
    publishing.recover(rig.root)
    activation = MagicMock()
    monkeypatch.setattr(publishing, "start", activation)
    rig.tick()
    assert [s.state for _, s in rig.statuses()] == ["failed"]
    activation.assert_not_called()


def test_publish_worker_starts_after_recorded_status_and_reply(tmp_path, monkeypatch):
    from jailbee.orchestration import publishing

    rig = Rig(tmp_path, monkeypatch)
    rig.incus.list_containers.return_value = [_orch()]
    data = {"version": 1, "id": "pub-1", "type": "publish", "outbox": "issue", "manifest": "a.json"}
    rig.requests["proj-orch"] = [Request("pub-1.json", json.dumps(data), {})]

    worker = MagicMock()

    def start(ctx, manifest):
        assert rig.statuses()[0][1].state == "started"
        assert rig.store.lookup_request(ctx.sender.key, manifest.id) is not None

        return worker

    monkeypatch.setattr(publishing, "start", start)
    memory = broker.BrokerMemory()
    rig.tick(memory)
    assert memory.publish_threads == [worker]


def _spawn_raw(mid: str = "s1", *, prompt_file: bool = True) -> Request:
    data: dict[str, Any] = {
        "version": 1,
        "id": mid,
        "type": "spawn",
        "kind": "new",
        "branch": "fix/1",
        "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
    }
    files: dict[str, str] = {}
    if prompt_file:
        data["prompt_file"] = f"{mid}.md"
        files[f"{mid}.md"] = "do the thing"
    else:
        data["prompt"] = "do the thing"
    text = json.dumps(data)
    files[f"{mid}.json"] = text
    return Request(file_name=f"{mid}.json", text=text, files=files)


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from jailbee.orchestration.child_outbox import PendingGithub

        monkeypatch.setattr(
            "jailbee.orchestration.child_outbox.probe",
            MagicMock(return_value=PendingGithub((), (), ())),
        )
        self.root = tmp_path / "state"
        self.root.mkdir()
        self.cfg = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
        self.journal = Journal(self.root)
        self.incus = MagicMock()
        self.incus.list_containers.return_value = []
        self.incus.config_get_checked.side_effect = lambda full, key: next(
            (
                row["config"].get(key)
                for row in self.incus.list_containers.return_value
                if row["name"] == full
            ),
            None,
        )
        self.incus.exists.return_value = False
        self.memory = broker.BrokerMemory()
        self.store = BrokerStore(store_path(self.root))
        self.requests: dict[str, list[Request] | Exception] = {}
        self.seq = MagicMock()  # parent of every seam, to assert call order
        self.serve_requests = MagicMock(side_effect=self._read)
        self.deliver = MagicMock()
        self.flush_pending = MagicMock(return_value=0)
        self.ensure_env = MagicMock()
        self.dispatch = MagicMock(
            return_value=Outcome("started", "creation of child `fix-1` started")
        )
        self.drain = MagicMock(return_value=[])
        self.drop = MagicMock(return_value=[])
        self.configs = MagicMock(return_value={"proj": self.cfg})
        self.gcfg = MagicMock(return_value=(GlobalConfig(), []))
        for name, mock in [
            ("deliver", self.deliver),
            ("flush_pending", self.flush_pending),
            ("ensure_orchestration_env", self.ensure_env),
            ("dispatch", self.dispatch),
            ("drain_queue", self.drain),
            ("drop_queue", self.drop),
            ("repo_configs", self.configs),
            ("load_global_config", self.gcfg),
        ]:
            monkeypatch.setattr(broker, name, mock)
            self.seq.attach_mock(mock, name)

        real_serve = broker._serve

        def serve(ctx, journal, memory):
            real_serve(ctx, journal, memory)
            self.serve_requests(ctx, memory)

        monkeypatch.setattr(broker, "_serve", serve)
        self.seq.attach_mock(self.serve_requests, "serve_requests")

    def _read(self, ctx, memory):
        got = self.requests.get(ctx.sender.full, [])
        if isinstance(got, Exception):
            raise got
        for request in got:
            data = json.loads(request.text) if not request.text.startswith("{not") else {}
            ref = data.get("id", request.file_name.removesuffix(".json"))
            if self.store.lookup_request(ctx.sender.key, ref) is not None:
                continue
            if broker.publishing.was_accepted(ctx, ref):
                continue
            try:
                manifest = parse_manifest(request.file_name, request.text, request.files)
                outcome = None
            except ManifestError as exc:
                manifest, outcome = None, actions.refused(str(exc))

            def record(ref, outcome):
                self.store.record_request(
                    ctx.sender.key,
                    ref,
                    digest=None,
                    state=outcome.state,
                    detail=outcome.detail,
                    item=None,
                    now=NOW.timestamp(),
                )

            broker._handle_manifest(
                ctx,
                memory,
                ref=ref,
                manifest=manifest,
                outcome=outcome,
                record=record,
                reply=lambda ref, out: broker._reply(ctx, ref, out, memory),
                before_publish=lambda: None,
            )

    def tick(self, memory: broker.BrokerMemory | None = None) -> bool:
        return broker.tick(
            self.incus,
            root=self.root,
            journal=self.journal,
            now=NOW,
            memory=memory if memory is not None else self.memory,
        )

    def statuses(self) -> list[tuple[str, InboxStatus]]:
        out = []
        for c in self.deliver.call_args_list:
            item = c.args[3]
            assert isinstance(item, InboxStatus)
            out.append((c.args[2], item))
        return out


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_ensure_env_runs_before_pending_delivery_without_container_writes(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick()
    rig.ensure_env.assert_called_once_with(rig.incus, "proj-orch", uid=rig.cfg.container_user.uid)
    names = [c[0] for c in rig.seq.mock_calls]
    assert names.index("ensure_orchestration_env") < names.index("flush_pending")
    rig.incus.exec_with_input.assert_not_called()


def test_child_marker_written_once_per_broker_process(rig: Rig) -> None:
    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_child(), _orch()]
    rig.tick(memory)
    rig.tick(memory)
    calls = [
        c
        for c in rig.incus.exec.call_args_list
        if "/home/dev/.jailbee/orchestration/role" in str(c)
    ]
    assert len(calls) == 1
    assert calls[0].args[0] == "proj-kid"
    assert calls[0].kwargs["uid"] == rig.cfg.container_user.uid
    assert "child" in str(calls[0])


def test_ensure_env_handles_orchestrator_to_child_transition(rig: Rig) -> None:
    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    rig.incus.exec.reset_mock()
    rig.incus.list_containers.return_value = [
        _child("proj-orch", of="proj-parent"),
        _orch("proj-parent"),
    ]
    rig.tick(memory)
    assert any(
        c.args[0] == "proj-orch" and labels.ROLE_MARKER_PATH in str(c)
        for c in rig.incus.exec.call_args_list
    )


def test_ensure_env_reads_checked_labels_without_per_node_inventory(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch(), _child(), _child("proj-other")]
    memory = broker.BrokerMemory()
    rig.tick(memory)
    assert rig.incus.list_containers.call_count == 2
    rig.incus.list_containers.reset_mock()
    rig.tick(memory)
    assert rig.incus.list_containers.call_count == 2
    assert memory.env_roles == {
        "proj-orch": "orchestrator",
        "proj-kid": "child",
        "proj-other": "child",
    }


@pytest.mark.parametrize("cached", [False, True])
def test_ensure_env_checked_read_failure_forgets_cache(rig: Rig, cached: bool) -> None:
    rig.incus.list_containers.return_value = [_child()]
    memory = broker.BrokerMemory()
    if cached:
        rig.tick(memory)
    rig.ensure_env.reset_mock()
    rig.incus.exec.reset_mock()
    rig.incus.config_get_checked.side_effect = IncusError("instance disappeared")
    rig.tick(memory)
    assert "proj-kid" not in memory.env_ensured
    assert "proj-kid" not in memory.env_roles
    rig.ensure_env.assert_not_called()
    assert not any(labels.ROLE_MARKER_PATH in str(c) for c in rig.incus.exec.call_args_list)


def test_ensure_env_runs_for_children(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_child()]
    rig.tick()
    rig.ensure_env.assert_called_once_with(rig.incus, "proj-kid", uid=rig.cfg.container_user.uid)


def test_ensure_env_once_per_broker_process(rig: Rig) -> None:
    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    rig.tick(memory)
    assert rig.ensure_env.call_count == 1


def test_ensure_env_failure_is_logged_once_and_retried(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    memory = broker.BrokerMemory()
    rig.ensure_env.side_effect = IncusError("read-only file system")
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    rig.tick(memory)
    assert rig.ensure_env.call_count == 2
    assert rig.serve_requests.call_count == 2
    assert len([r for r in caplog.records if "orchestration inbox" in r.getMessage()]) == 1


def test_broker_warns_once_for_old_image(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    from jailbee.orchestration.outbox import MISSING_JAILBEE_MESSAGE, ContainerJailbeeMissing

    memory = broker.BrokerMemory()
    rig.ensure_env.side_effect = ContainerJailbeeMissing(MISSING_JAILBEE_MESSAGE)
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    rig.tick(memory)
    assert rig.ensure_env.call_count == 2
    assert "proj-orch" not in memory.env_ensured
    warnings = [r.getMessage() for r in caplog.records if "jb base build" in r.getMessage()]
    assert len(warnings) == 1
    assert "install the orchestration inbox helper" not in warnings[0]


def test_ensure_env_rerun_after_container_vanishes(rig: Rig) -> None:
    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    rig.incus.list_containers.return_value = []
    rig.tick(memory)
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    assert rig.ensure_env.call_count == 2


def test_tick_prunes_reservations(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.orchestration.state import Reservation, load_reservations, save_reservations

    save_reservations(rig.root, {"proj-orch": [Reservation("proj-failed", 777, "t")]})
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: False)

    def snapshot() -> list[dict[str, Any]]:
        assert load_reservations(rig.root) == {}
        return []

    rig.incus.list_containers.side_effect = snapshot
    assert rig.tick() is False
    assert load_reservations(rig.root) == {}


def test_config_for_picks_longest_prefix(tmp_path: Path) -> None:
    a = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
    b = make_config(tmp_path).model_copy(update={"container_prefix": "proj-x"})
    configs = {"proj": a, "proj-x": b}
    assert broker.config_for("proj-x-feat", configs) is b
    assert broker.config_for("proj-feat", configs) is a
    assert broker.config_for("projfeat", configs) is None
    assert broker.config_for("other-feat", configs) is None


def test_failed_longer_prefix_is_not_attributed_to_a_shorter_one(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.configs.return_value = {"proj": rig.cfg, "proj-x": None}
    rig.incus.list_containers.return_value = [_orch("proj-x-orch")]
    rig.requests["proj-x-orch"] = [_spawn_raw("s1")]
    memory = broker.BrokerMemory()
    with caplog.at_level(logging.WARNING, logger="jailbee.orchestration.broker"):
        rig.tick(memory)
        rig.tick(memory)
    rig.serve_requests.assert_not_called()
    rig.flush_pending.assert_not_called()
    rig.incus.exec.assert_not_called()
    rig.incus.exec_with_input.assert_not_called()
    assert len([r for r in caplog.records if "proj-x-orch" in r.getMessage()]) == 1


def test_child_resolves_through_its_orchestrators_config(rig: Rig, tmp_path: Path) -> None:
    other = make_config(tmp_path).model_copy(update={"container_prefix": "proj-x"})
    rig.configs.return_value = {"proj": rig.cfg, "proj-x": other}
    rig.incus.list_containers.return_value = [_orch(), _child("proj-x-feat", of="proj-orch")]
    msg = json.dumps({"version": 1, "id": "m1", "type": "message", "body": "hi"})
    rig.requests["proj-x-feat"] = [Request("m1.json", msg, {"m1.json": msg})]
    rig.dispatch.return_value = Outcome("done", "delivered")
    rig.tick()
    ctx = rig.dispatch.call_args.args[0]
    assert ctx.sender.full == "proj-x-feat"
    assert ctx.sender.short == "x-feat"
    assert ctx.cfg is rig.cfg
    assert ctx.nodes["proj-x-feat"].short == "x-feat"


def test_repo_configs_keeps_failed_prefixes_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlmodel import Session, SQLModel, create_engine

    from jailbee.db.models import RegisteredRepo

    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for prefix, root in [("proj", good), ("proj-x", bad), ("gone", tmp_path / "nope")]:
            session.add(
                RegisteredRepo(container_prefix=prefix, repo_root=str(root), registered_at=NOW)
            )
        session.commit()
    monkeypatch.setattr("jailbee.db.get_engine", lambda: engine)
    cfg = make_config(good).model_copy(update={"container_prefix": "proj"})

    def load(root: Path) -> object:
        if root == bad:
            raise ValueError("broken yaml")
        return cfg

    monkeypatch.setattr(broker, "load_repo_config", load)
    out = broker.repo_configs()
    assert out == {"proj": cfg, "proj-x": None, "gone": None}
    assert broker.config_for("proj-x-orch", out) is None
    assert broker.config_for("proj-orch", out) is cfg
    engine.dispose()


def test_unparseable_grant_label_is_logged_once(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.incus.list_containers.return_value = [_c("proj-orch", **{labels.GRANT: "{nope"})]
    rig.requests["proj-orch"] = [_spawn_raw("s1")]
    memory = broker.BrokerMemory()
    with caplog.at_level(logging.WARNING, logger="jailbee.orchestration.broker"):
        rig.tick(memory)
        rig.tick(memory)
    rig.serve_requests.assert_not_called()
    hits = [r for r in caplog.records if "unparseable" in r.getMessage()]
    assert len(hits) == 1 and "proj-orch" in hits[0].getMessage()


def test_tick_ignores_ordinary_containers(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_c("proj-plain"), _c("proj-other", running=False)]
    assert rig.tick() is False
    rig.incus.exec.assert_not_called()
    rig.incus.exec_with_input.assert_not_called()
    rig.serve_requests.assert_not_called()
    rig.flush_pending.assert_not_called()
    rig.dispatch.assert_not_called()


def test_parsed_request_dispatches_records_and_replies(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    raw = _spawn_raw("s1")
    rig.requests["proj-orch"] = [raw]
    assert rig.tick() is True

    rig.dispatch.assert_called_once()
    ctx, manifest = rig.dispatch.call_args.args
    assert manifest.id == "s1" and manifest.prompt == "do the thing"
    assert ctx.sender.full == "proj-orch" and ctx.sender.short == "orch"
    assert ctx.cfg is rig.cfg and ctx.root == rig.root and ctx.now == NOW

    [(to, status)] = rig.statuses()
    assert to == "proj-orch"
    assert (status.ref, status.state, status.detail) == (
        "s1",
        "started",
        "creation of child `fix-1` started",
    )
    assert status.id.startswith("h-")
    assert rig.deliver.call_args.kwargs["running"] is True

    assert rig.store.lookup_request("proj-orch", "s1").state == "started"


@pytest.mark.parametrize("ref", ["refs/heads/../bad", "HEAD~1", "abc123", "refs/heads/missing"])
def test_spawn_ref_refused_by_broker_without_creation(rig: Rig, mocker, monkeypatch, ref):
    raw = _spawn_raw()
    data = json.loads(raw.text)
    data.update(version=2, kind="fork", ref=ref)
    text = json.dumps(data)
    rig.requests["proj-orch"] = [Request(raw.file_name, text, raw.files | {raw.file_name: text})]
    rig.incus.list_containers.return_value = [_orch()]
    monkeypatch.setattr(broker, "dispatch", actions.dispatch)
    mocker.patch(
        "jailbee.orchestration.actions.profiles.repo_cap", return_value=RepoCap(allowed=True)
    )
    mocker.patch("jailbee.orchestration.actions.profiles.default_runner", return_value=(None, None))
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    mocker.patch("jailbee.sync.assert_container_publishable", return_value="proj-orch")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    rig.incus.exec.side_effect = IncusError("unknown revision")
    # Git's syntax probe is the only host subprocess this path may request.
    run = mocker.patch(
        "jailbee.git.subprocess.run", return_value=mocker.Mock(returncode=int(".." in ref))
    )
    fetch = mocker.patch("jailbee.sync.fetch_ref_from_container")
    fetch_sha = mocker.patch("jailbee.sync.fetch_sha_from_container")
    pin = mocker.patch("jailbee.forking.pin_fork_commit")
    create = mocker.patch("jailbee.orchestration.actions.start_create")
    start = mocker.patch("jailbee.orchestration.actions.start_child")
    assert rig.tick() is True
    [(to, status)] = rig.statuses()
    assert to == "proj-orch" and status.state == "refused"
    if ref == "refs/heads/missing":
        assert ref in status.detail and "resolve" in status.detail
        rig.incus.exec.assert_called_once_with(
            "proj-orch",
            ["git", "-C", "/repo", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            uid=rig.cfg.container_user.uid,
        )
    else:
        assert "ref" in status.detail.lower()
        rig.incus.exec.assert_not_called()
    for effect in (fetch, fetch_sha, pin, create, start):
        effect.assert_not_called()
    for call in run.call_args_list:
        assert call.args[0][:2] == ["git", "check-ref-format"]
    assert load_queue(rig.root) == {}


def test_bad_manifest_gets_refused_status(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.requests["proj-orch"] = [Request("bad-1.json", "{not json", {"bad-1.json": "{"})]
    rig.tick()
    rig.dispatch.assert_not_called()
    [(to, status)] = rig.statuses()
    assert to == "proj-orch"
    assert status.state == "refused" and status.ref == "bad-1"
    assert "invalid JSON" in status.detail
    assert rig.store.lookup_request("proj-orch", "bad-1").state == "refused"


def test_queued_outcome_is_sent_as_queued_status(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.requests["proj-orch"] = [_spawn_raw("s1")]
    rig.dispatch.return_value = Outcome("queued", "queued at position 1")
    rig.tick()
    [(_, status)] = rig.statuses()
    assert (status.state, status.detail, status.ref) == ("queued", "queued at position 1", "s1")


def test_handler_crash_is_refused_not_retried(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.requests["proj-orch"] = [_spawn_raw("s1")]
    rig.dispatch.side_effect = RuntimeError("secret /host/path boom")
    rig.tick()
    [(_, status)] = rig.statuses()
    # Generic detail for the container; the exception text stays in the host log.
    assert (status.state, status.detail) == ("refused", "broker error")


def test_flush_pending_runs_before_any_new_status(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.requests["proj-orch"] = [_spawn_raw("s1")]
    rig.tick()
    names = [c[0] for c in rig.seq.mock_calls if c[0] in {"flush_pending", "deliver"}]
    assert names == ["flush_pending", "deliver"]
    rig.flush_pending.assert_called_once_with(
        rig.incus,
        rig.root,
        "proj-orch",
        uid=rig.cfg.container_user.uid,
        finish=ANY,
        recipient_uuid=None,
    )


def test_children_are_served_too(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch(running=False), _child()]
    msg = json.dumps({"version": 1, "id": "m1", "type": "message", "body": "hi"})
    rig.requests["proj-kid"] = [Request("m1.json", msg, {"m1.json": msg})]
    rig.dispatch.return_value = Outcome("done", "delivered")
    assert rig.tick() is True
    ctx, manifest = rig.dispatch.call_args.args
    assert ctx.sender.full == "proj-kid" and manifest.type == "message"
    # The stopped orchestrator flushes host state but never serves requests.
    assert [c.args[0].sender.full for c in rig.serve_requests.call_args_list] == ["proj-kid"]
    assert [c.args[2] for c in rig.flush_pending.call_args_list] == ["proj-orch", "proj-kid"]


def test_one_bad_container_does_not_stop_the_round(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.incus.list_containers.return_value = [_orch("proj-a"), _orch("proj-b")]
    rig.requests["proj-a"] = RuntimeError("exec exploded")
    rig.requests["proj-b"] = [_spawn_raw("s1")]
    with caplog.at_level(logging.ERROR, logger="jailbee.orchestration.broker"):
        rig.tick()
    rig.dispatch.assert_called_once()
    assert rig.dispatch.call_args.args[0].sender.full == "proj-b"
    assert any("proj-a" in r.getMessage() and r.exc_info for r in caplog.records)
    # The queue phase still runs for both orchestrators.
    assert {c.args[0].sender.full for c in rig.drain.call_args_list} == {"proj-a", "proj-b"}


def test_tick_returns_false_when_idle(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_c("proj-plain"), _orch(running=False)]
    assert rig.tick() is False
    rig.serve_requests.assert_not_called()


def test_tick_returns_true_with_a_running_child(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_child()]
    assert rig.tick() is True


def test_global_config_failure_skips_the_round(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.requests["proj-orch"] = [_spawn_raw("s1")]
    rig.gcfg.side_effect = ValueError("bad yaml")
    assert rig.tick() is True
    rig.serve_requests.assert_not_called()
    rig.dispatch.assert_not_called()


def test_gcfg_is_loaded_once_and_shared(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch("proj-a"), _orch("proj-b")]
    rig.requests["proj-a"] = [_spawn_raw("s1")]
    rig.requests["proj-b"] = [_spawn_raw("s2")]
    sentinel = GlobalConfig()
    rig.gcfg.return_value = (sentinel, [])
    rig.tick()
    rig.gcfg.assert_called_once()
    assert all(c.args[0].gcfg is sentinel for c in rig.dispatch.call_args_list)


def test_unregistered_repo_container_skipped(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.incus.list_containers.return_value = [_orch("stranger-orch")]
    rig.requests["stranger-orch"] = [_spawn_raw("s1")]
    memory = broker.BrokerMemory()
    with caplog.at_level(logging.WARNING, logger="jailbee.orchestration.broker"):
        rig.tick(memory)
        rig.tick(memory)
    rig.serve_requests.assert_not_called()
    rig.flush_pending.assert_not_called()
    rig.incus.exec.assert_not_called()
    rig.incus.exec_with_input.assert_not_called()
    hits = [r for r in caplog.records if "stranger-orch" in r.getMessage()]
    assert len(hits) == 1  # logged once, not every round


def test_grant_changes_are_visible_without_container_files(rig: Rig) -> None:
    from jailbee.orchestration import socket_ops

    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    data = socket_ops.handle_grant(memory, "proj-orch")
    assert (
        Grant.model_validate({k: v for k, v in data.items() if k != "default_runner_source"})
        == GRANT
    )
    wider = Grant(max_running_children=3)
    rig.incus.list_containers.return_value = [_orch(grant=wider)]
    rig.tick(memory)
    assert (
        socket_ops.handle_grant(memory, "proj-orch")["max_running_children"]
        == wider.max_running_children
    )
    rig.incus.exec_with_input.assert_not_called()


def _req(mid: str, child: str = "proj-fix-2") -> QueuedRequest:
    return QueuedRequest(
        manifest_id=mid,
        child_full=child,
        kind="new",
        commit=None,
        queued_at=NOW.isoformat(),
        branch="fix/2",
        base=None,
        prompt="p",
        rights={},
    )


def test_drain_after_a_stop_only_round_reports_each_outcome(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch(), _child(running=False)]
    stop = json.dumps({"version": 1, "id": "st1", "type": "stop", "child": "kid"})
    rig.requests["proj-orch"] = [Request("st1.json", stop, {"st1.json": stop})]
    rig.dispatch.return_value = Outcome("done", "child `kid` stopped")
    rig.drain.return_value = [
        (_req("q1"), Outcome("started", "creation of child `fix-2` started")),
        (_req("q2", "proj-fix-3"), Outcome("refused", "container `fix-3` already exists")),
    ]
    rig.tick()
    rig.drain.assert_called_once()
    assert rig.drain.call_args.args[0].sender.full == "proj-orch"
    got = [(to, s.ref, s.state, s.detail) for to, s in rig.statuses()]
    assert got == [
        ("proj-orch", "st1", "done", "child `kid` stopped"),
        ("proj-orch", "q1", "started", "creation of child `fix-2` started"),
        ("proj-orch", "q2", "refused", "container `fix-3` already exists"),
    ]
    # Drain comes after the round's manifests.
    order = [c[0] for c in rig.seq.mock_calls if c[0] in {"dispatch", "drain_queue"}]
    assert order == ["dispatch", "drain_queue"]


def test_stopped_orchestrator_queue_still_drains_and_parks_replies(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch(running=False)]
    save_queue(rig.root, {"proj-orch": [_req("q1")]})
    rig.drain.return_value = [(_req("q1"), Outcome("started", "ok"))]
    rig.tick()
    rig.drain.assert_called_once()
    assert rig.deliver.call_args.kwargs["running"] is False


def test_disallowed_repo_drops_queue_saying_so(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions.profiles, "repo_cap", lambda cfg: RepoCap(allowed=False))
    rig.incus.list_containers.return_value = [_orch()]
    save_queue(rig.root, {"proj-orch": [_req("q1"), _req("q2", "proj-fix-3")]})
    rig.drop.return_value = [_req("q1"), _req("q2", "proj-fix-3")]
    rig.tick()
    rig.drain.assert_not_called()
    rig.drop.assert_called_once()
    got = [(s.ref, s.state, s.detail) for _, s in rig.statuses()]
    assert got == [
        ("q1", "refused", "orchestration is not allowed in this repo"),
        ("q2", "refused", "orchestration is not allowed in this repo"),
    ]


def test_removed_grant_label_drops_queue_with_revoked_statuses(rig: Rig) -> None:
    """The grant label is gone, so the container is no orchestrator, but its queue remains."""
    rig.incus.list_containers.return_value = [_c("proj-orch")]
    save_queue(rig.root, {"proj-orch": [_req("q1")]})
    rig.drop.return_value = [_req("q1")]
    rig.tick()
    rig.drain.assert_not_called()
    rig.drop.assert_called_once()
    assert rig.drop.call_args.args[0].sender.full == "proj-orch"
    [(to, s)] = rig.statuses()
    assert (to, s.ref, s.state, s.detail) == (
        "proj-orch",
        "q1",
        "refused",
        "orchestration grant revoked",
    )


def test_vanished_orchestrator_queue_is_discarded(rig: Rig) -> None:
    save_queue(rig.root, {"proj-gone": [_req("q1")]})
    rig.tick()
    assert load_queue(rig.root) == {}
    rig.deliver.assert_not_called()


def _sigterm() -> None:
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)


@pytest.mark.parametrize("upgrade", [False, True])
def test_broker_waits_for_publish_workers_before_exit(tmp_path, monkeypatch, upgrade):
    from jailbee.orchestration import publishing

    rig = Rig(tmp_path, monkeypatch)
    monkeypatch.setattr(broker, "state_root", lambda: rig.root)
    worker = MagicMock()
    worker.is_alive.return_value = True
    rig.incus.list_containers.return_value = [_orch()]
    data = {"version": 1, "id": "pub-join", "type": "publish", "outbox": "pr", "manifest": "a.json"}
    rig.requests["proj-orch"] = [Request("pub-join.json", json.dumps(data), {})]
    # Run the actual broker handler/record/reply/activation path; only the
    # asynchronous launch is replaced, never the tracking list under test.
    monkeypatch.setattr(publishing, "start", lambda *args: worker)
    real_tick = broker.tick

    def tick(*args, **kwargs):
        result = real_tick(*args, **kwargs)
        if not upgrade:
            _sigterm()
        return result

    monkeypatch.setattr(broker, "tick", tick)
    monkeypatch.setattr(broker, "_installed_version", lambda: "different" if upgrade else None)
    monkeypatch.setattr(broker, "_pause", lambda *args: None)
    broker.run_broker(rig.incus)
    worker.join.assert_called_once()
    assert 0 < worker.join.call_args.kwargs["timeout"] <= 30


def test_run_broker_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    monkeypatch.setattr(broker, "tick", MagicMock(side_effect=[False, True, False]))
    pauses: list[float] = []

    def fake_pause(seconds: float, stop: broker.StopFlag) -> None:
        pauses.append(seconds)
        if len(pauses) == 2:
            _sigterm()

    monkeypatch.setattr(broker, "_pause", fake_pause)
    assert broker.run_broker(MagicMock()) == 0
    assert pauses == [60.0, 5.0]


def test_run_broker_exits_for_a_restart_when_another_version_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    tick = MagicMock(return_value=True)
    monkeypatch.setattr(broker, "tick", tick)
    monkeypatch.setattr(broker, "__version__", "1.7.8")
    installed = iter(["1.7.8", "1.8.0"])
    monkeypatch.setattr(broker, "_installed_version", lambda: next(installed))
    monkeypatch.setattr(broker, "_pause", lambda seconds, stop: None)

    assert broker.run_broker(MagicMock()) == broker.UPGRADED_EXIT
    # The round that saw the new version finished before the exit.
    assert tick.call_count == 2


def test_run_broker_keeps_running_when_the_installed_version_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    monkeypatch.setattr(broker, "tick", MagicMock(return_value=False))
    monkeypatch.setattr(broker, "_installed_version", lambda: None)
    pauses: list[float] = []

    def fake_pause(seconds: float, stop: broker.StopFlag) -> None:
        pauses.append(seconds)
        if len(pauses) == 2:
            _sigterm()

    monkeypatch.setattr(broker, "_pause", fake_pause)
    assert broker.run_broker(MagicMock()) == 0
    assert len(pauses) == 2


def test_installed_version_is_none_without_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(broker, "_metadata_version", missing)
    assert broker._installed_version() is None


def test_pause_sleeps_in_short_slices_and_stops_early(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    slept: list[float] = []
    stop = broker.StopFlag()

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds
        if clock[0] >= 3.0:
            stop(signal.SIGTERM, None)

    monkeypatch.setattr(broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker.time, "sleep", fake_sleep)
    broker._pause(60.0, stop)
    assert slept == [1.0, 1.0, 1.0]  # interrupted after 3 s, not 60

    slept.clear()
    clock[0] = 0.0
    broker._pause(2.5, broker.StopFlag())
    assert slept == [1.0, 1.0, 0.5]


def test_sigterm_during_dispatch_finishes_the_round_then_exits(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "state_root", lambda: rig.root)
    pause = MagicMock()
    monkeypatch.setattr(broker, "_pause", pause)
    rig.incus.list_containers.return_value = [_orch()]
    raw = _spawn_raw("s1")
    rig.requests["proj-orch"] = [raw]

    def act(ctx: object, manifest: object) -> Outcome:
        _sigterm()  # arrives mid-handler
        return Outcome("started", "ok")

    rig.dispatch.side_effect = act
    before = signal.getsignal(signal.SIGTERM)
    assert broker.run_broker(rig.incus) == 0
    assert rig.store.lookup_request("proj-orch", "s1").state == "started"
    [(_, status)] = rig.statuses()
    assert status.state == "started"
    # incl. action and watch refresh, and the socket check's all-projects listing
    assert rig.incus.list_containers.call_count >= 4
    pause.assert_called_once()  # returns at once: the flag is already set
    assert signal.getsignal(signal.SIGTERM) == before


def test_run_broker_survives_a_failed_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    tick = MagicMock(side_effect=[RuntimeError("incus down"), True])
    monkeypatch.setattr(broker, "tick", tick)
    pauses: list[float] = []

    def fake_pause(seconds: float, stop: broker.StopFlag) -> None:
        pauses.append(seconds)
        if len(pauses) == 2:
            _sigterm()

    monkeypatch.setattr(broker, "_pause", fake_pause)
    assert broker.run_broker(MagicMock()) == 0
    assert pauses == [60.0, 5.0]
    assert tick.call_count == 2


def test_cli_broker_command_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from jailbee.cli import app

    run = MagicMock(return_value=0)
    monkeypatch.setattr(broker, "run_broker", run)
    monkeypatch.setattr("jailbee.incus.Incus", MagicMock(name="Incus"))
    result = CliRunner().invoke(app, ["_broker"])
    assert result.exit_code == 0, result.output
    run.assert_called_once()


def test_recreated_container_acts_on_a_manifest_identical_to_the_old_ones(rig: Rig) -> None:
    """Same name, new Incus instance: the old instance's journal must not swallow it."""
    raw = _spawn_raw("s1")
    old, new = _orch(), _orch()
    old["config"]["volatile.uuid"] = "uuid-old"
    new["config"]["volatile.uuid"] = "uuid-new"
    rig.requests["proj-orch"] = [raw]
    rig.incus.list_containers.return_value = [old]
    rig.tick()
    rig.incus.list_containers.return_value = [new]  # destroyed and recreated
    rig.tick()
    assert rig.dispatch.call_count == 2


def test_vanished_container_pending_is_purged(rig: Rig) -> None:
    from jailbee.orchestration.state import pending_dir

    for name in ("proj-gone", "proj-orch"):
        d = pending_dir(rig.root, name)
        d.mkdir(parents=True)
        (d / "x.json").write_text("{}")
    rig.incus.list_containers.return_value = [_orch(running=False)]
    rig.tick()
    assert not pending_dir(rig.root, "proj-gone").exists()
    assert (pending_dir(rig.root, "proj-orch") / "x.json").exists()


def test_replies_carry_an_increasing_sequence(rig: Rig) -> None:
    """Statuses emitted in one round keep their order whatever their random ids."""
    rig.incus.list_containers.return_value = [_orch()]
    rig.requests["proj-orch"] = [_spawn_raw("s1"), _spawn_raw("s2")]
    rig.drain.return_value = [(_req("q1"), Outcome("started", "x"))]
    memory = broker.BrokerMemory()
    rig.tick(memory)
    seqs = [c.kwargs["seq"] for c in rig.deliver.call_args_list]
    assert len(seqs) == 3
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    rig.tick(memory)  # the counter lives in memory and keeps rising
    assert rig.deliver.call_args_list[-1].kwargs["seq"] > seqs[-1]


def test_second_broker_instance_exits_while_the_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import fcntl

    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    tick = MagicMock(return_value=False)
    monkeypatch.setattr(broker, "tick", tick)
    monkeypatch.setattr(broker, "_pause", lambda seconds, stop: _sigterm())
    with (tmp_path / "broker.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with caplog.at_level(logging.INFO, logger="jailbee.orchestration.broker"):
            assert broker.run_broker(MagicMock()) == 0
    tick.assert_not_called()
    assert "another orchestration broker is running" in caplog.text


def test_broker_holds_the_lock_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    held: list[bool] = []

    def tick(*a: object, **k: object) -> bool:
        with (tmp_path / "broker.lock").open("w") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
        _sigterm()
        return False

    monkeypatch.setattr(broker, "tick", tick)
    monkeypatch.setattr(broker, "_pause", lambda seconds, stop: None)
    assert broker.run_broker(MagicMock()) == 0
    assert held == [True]


def test_tick_flushes_pending_for_revoked_orchestrator(rig: Rig) -> None:
    from jailbee.orchestration.state import pending_dir

    d = pending_dir(rig.root, "proj-orch")
    d.mkdir(parents=True)
    (d / "x.json").write_text("{}")
    # A grant label that no longer parses: role "none", yet still labelled.
    rig.incus.list_containers.return_value = [_c("proj-orch", **{labels.GRANT: "garbage"})]
    rig.requests["proj-orch"] = [_spawn_raw("s1")]
    rig.tick()
    rig.flush_pending.assert_called_once_with(
        rig.incus,
        rig.root,
        "proj-orch",
        uid=rig.cfg.container_user.uid,
        finish=ANY,
        recipient_uuid=None,
    )
    rig.serve_requests.assert_not_called()
    rig.dispatch.assert_not_called()


def test_tick_does_not_flush_role_none_without_pending(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_c("proj-orch", **{labels.GRANT: "garbage"})]
    rig.tick()
    rig.flush_pending.assert_not_called()


def test_tick_context_shares_the_memory_sequence(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.requests["proj-orch"] = [_spawn_raw("s1")]
    memory = broker.BrokerMemory()
    rig.tick(memory)
    ctx = memory.contexts["proj-orch"]
    assert ctx.seq is memory.seq


def test_tick_flushes_pending_for_container_with_no_labels(rig: Rig) -> None:
    from jailbee.orchestration.state import pending_dir

    d = pending_dir(rig.root, "proj-orch")
    d.mkdir(parents=True)
    (d / "x.json").write_text("{}")
    # `jailbee grant revoke` removed the label entirely.
    rig.incus.list_containers.return_value = [_c("proj-orch")]
    rig.requests["proj-orch"] = [_spawn_raw("s1")]
    rig.tick()
    rig.flush_pending.assert_called_once_with(
        rig.incus,
        rig.root,
        "proj-orch",
        uid=rig.cfg.container_user.uid,
        finish=ANY,
        recipient_uuid=None,
    )
    rig.serve_requests.assert_not_called()
    rig.dispatch.assert_not_called()


def test_tick_skips_unlabelled_pending_without_repo_config(rig: Rig) -> None:
    from jailbee.orchestration.state import pending_dir

    d = pending_dir(rig.root, "other-box")
    d.mkdir(parents=True)
    (d / "x.json").write_text("{}")
    rig.incus.list_containers.return_value = [_c("other-box")]
    rig.tick()
    rig.flush_pending.assert_not_called()


def test_tick_completes_parked_finish_when_orchestrator_starts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration import finishing, importing, outbox
    from jailbee.orchestration.actions import ActionContext, Node
    from jailbee.orchestration.protocol import PendingFinish

    orch = Node(
        "proj-orch", "orch", True, {labels.GRANT: Grant().to_label(), "volatile.uuid": "uuid-orch"}
    )
    child = Node("proj-kid", "kid", True, {labels.SPAWNED_BY: orch.full})
    ctx = ActionContext(
        rig.cfg,
        GlobalConfig(),
        rig.incus,
        rig.root,
        orch,
        {orch.full: orch, child.full: child},
        NOW,
    )
    item = PendingFinish(
        version=1,
        type="finish",
        id="d1",
        child_full=child.full,
        child_short="kid",
        branch="b",
        done_sha="a" * 40,
        body="done",
        in_reply_to=None,
        uid=1000,
    )
    finishing.park(ctx, orch, item)
    relay = MagicMock(return_value=importing.Imported("refs/jailbee/from/kid/b", "a" * 40))
    monkeypatch.setattr(importing, "import_child", relay)
    monkeypatch.setattr(actions, "_child_head", MagicMock(return_value="a" * 40))
    monkeypatch.setattr(actions.sync, "_container_current_branch", MagicMock(return_value="b"))
    monkeypatch.setattr(broker, "flush_pending", outbox.flush_pending)
    orch_row = _orch(grant=Grant())
    orch_row["config"]["volatile.uuid"] = "uuid-orch"
    rig.incus.list_containers.return_value = [orch_row, _child()]
    rig.tick()
    relay.assert_called_once()
    items = rig.store.unacked("uuid-orch")
    assert items[0].item["kind"] == "done" and items[0].item["imported_sha"] == "a" * 40


def _marked_child(rig: Rig) -> dict[str, Any]:
    kid = _child()
    kid["config"].update(
        {labels.ON_DONE: "destroy", labels.LAST_IMPORT: "a" * 40, labels.PENDING_DESTROY: "a" * 40}
    )
    rig.incus.list_containers.return_value = [_orch(grant=Grant()), kid]
    return kid


def test_stale_completed_finish_keeps_child_in_same_tick(rig: Rig, monkeypatch) -> None:
    from jailbee.orchestration.protocol import InboxMessage, PendingFinish
    from jailbee.orchestration.state import pending_dir

    _marked_child(rig)
    for row in rig.incus.list_containers.return_value:
        row["config"]["volatile.uuid"] = "new-" + row["name"]
    destroy = _destroy_seams(monkeypatch)

    def unset(full, key):
        for row in rig.incus.list_containers.return_value:
            if row["name"] == full:
                row["config"].pop(key, None)

    rig.incus.config_unset_checked.side_effect = unset
    monkeypatch.setattr(broker, "flush_pending", outbox.flush_pending)
    item = PendingFinish(
        version=1,
        type="finish",
        id="done-1",
        child_full="proj-kid",
        child_short="kid",
        branch="b",
        done_sha="a" * 40,
        body="done",
        in_reply_to=None,
        uid=1000,
    )
    completed = InboxMessage(version=1, id="h-done", type="message", from_="kid", body="done")
    path = pending_dir(rig.root, "proj-orch") / "a.finish.json"
    outbox.save_pending(path, item, recipient_uuid="old-orch", completed=completed)
    rig.tick()
    destroy.assert_not_called()
    assert path.with_name(path.name + ".bad").exists()
    rig.tick()
    destroy.assert_not_called()
    assert rig.store.unacked("new-proj-kid")


def _destroy_seams(monkeypatch: pytest.MonkeyPatch):
    destroy = MagicMock()
    monkeypatch.setattr(actions.lifecycle, "destroy_container", destroy)
    monkeypatch.setattr(actions, "_child_head", MagicMock(return_value="a" * 40))
    monkeypatch.setattr(actions.lifecycle, "container_repo_dir", MagicMock(return_value="/r"))
    monkeypatch.setattr(actions.sync, "_container_status_dirty", MagicMock(return_value=False))
    return destroy


def test_settle_watch_ignores_stale_finished_snapshot(rig: Rig, monkeypatch):
    orch = actions.Node("proj-orch", "orch", True, {})
    child = actions.Node(
        "proj-kid", "kid", True, {labels.SPAWNED_BY: orch.full, labels.STATE: labels.STATE_DONE}
    )
    ctx = actions.ActionContext(
        rig.cfg,
        GlobalConfig(),
        rig.incus,
        rig.root,
        orch,
        {orch.full: orch, child.full: child},
        NOW,
    )
    entry = watch.WatchEntry(
        "r1", child.full, "restart", NOW.isoformat(), supervise=True, agent_only=True
    )
    watch.register(rig.root, orch.full, entry)
    rig.incus.list_containers.return_value = [_orch(), _child()]
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr("jailbee.orchestration.agent_watch.probe", lambda *a, **k: None)
    broker._settle_watches(ctx, broker.BrokerMemory(), {})
    assert watch.load_watch(rig.root)[orch.full] == [entry]


def test_pending_destroy_waits_for_active_refresh(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.db.models import JOB_REFRESH

    _marked_child(rig)
    destroy = _destroy_seams(monkeypatch)
    monkeypatch.setattr(actions, "live_job_kind", lambda _: JOB_REFRESH)
    rig.tick()
    destroy.assert_not_called()


def test_tick_destroys_child_marked_for_destroy(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.orchestration.state import pending_dir

    _marked_child(rig)
    destroy = _destroy_seams(monkeypatch)
    directory = pending_dir(rig.root, "proj-kid")
    directory.mkdir(parents=True)
    rig.tick()
    destroy.assert_called_once_with(rig.cfg, rig.incus, "proj-kid", force=True)
    assert not directory.exists()


@pytest.mark.parametrize("unknown", [False, True])
def test_tick_rechecks_outbox_before_deferred_destroy(rig, monkeypatch, unknown):
    from jailbee.orchestration import child_outbox, outbox

    _marked_child(rig)
    destroy = _destroy_seams(monkeypatch)
    probe = MagicMock(return_value=child_outbox.PendingGithub((), ("review.json",), ()))
    if unknown:
        probe.side_effect = child_outbox.ChildOutboxError("permission")
    monkeypatch.setattr(child_outbox, "probe", probe)
    notice = MagicMock()
    monkeypatch.setattr(outbox, "deliver", notice)
    rig.tick()
    destroy.assert_not_called()
    rig.incus.config_unset_checked.assert_any_call("proj-kid", labels.PENDING_DESTROY)
    body = notice.call_args.args[3].body
    assert ("outbox could not be checked" if unknown else "review.json") in body


@pytest.mark.parametrize("dirty", [False, True])
def test_tick_keeps_marked_child_whose_head_moved_or_tree_dirty(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, dirty: bool
) -> None:
    from jailbee.orchestration import outbox

    _marked_child(rig)
    destroy = _destroy_seams(monkeypatch)
    monkeypatch.setattr(
        actions, "_child_head", MagicMock(return_value=("a" if dirty else "b") * 40)
    )
    monkeypatch.setattr(actions.sync, "_container_status_dirty", MagicMock(return_value=dirty))
    notice = MagicMock()
    monkeypatch.setattr(outbox, "deliver", notice)
    rig.tick()
    destroy.assert_not_called()
    rig.incus.config_unset_checked.assert_any_call("proj-kid", labels.PENDING_DESTROY)
    assert "kept" in notice.call_args.args[3].body


def test_destroy_rechecks_revoked_grant(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.orchestration import outbox

    kid = _marked_child(rig)
    grant = Grant(operations=("spawn", "message", "import"))
    rig.incus.list_containers.return_value = [_orch(grant=grant), kid]
    destroy = _destroy_seams(monkeypatch)
    notice = MagicMock()
    monkeypatch.setattr(outbox, "deliver", notice)
    rig.tick()
    destroy.assert_not_called()
    rig.incus.config_unset_checked.assert_any_call("proj-kid", labels.PENDING_DESTROY)
    assert "destroy permission" in notice.call_args.args[3].body


def test_destroy_failure_unsets_marker_and_notifies(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration import outbox

    _marked_child(rig)
    destroy = _destroy_seams(monkeypatch)
    destroy.side_effect = RuntimeError("busy")
    notice = MagicMock()
    monkeypatch.setattr(outbox, "deliver", notice)
    rig.tick()
    rig.incus.config_unset_checked.assert_any_call("proj-kid", labels.PENDING_DESTROY)
    assert "could not be destroyed: busy" in notice.call_args.args[3].body


def test_destroy_waits_for_parent_pending_delivery(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration.state import pending_dir

    _marked_child(rig)
    destroy = _destroy_seams(monkeypatch)
    directory = pending_dir(rig.root, "proj-orch")
    directory.mkdir(parents=True)
    (directory / "report.json").write_text("{}")
    rig.tick()
    destroy.assert_not_called()


def test_destroy_step_runs_after_done_reply(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.orchestration import importing, messages
    from jailbee.sync import FetchResult

    kid = _marked_child(rig)
    for row in rig.incus.list_containers.return_value:
        row["config"]["volatile.uuid"] = row["name"]
    kid["config"].pop(labels.PENDING_DESTROY)
    kid["config"].pop(labels.LAST_IMPORT)
    destroy = _destroy_seams(monkeypatch)
    monkeypatch.setattr(broker, "dispatch", actions.dispatch)
    monkeypatch.setattr(messages.sync, "_container_current_branch", MagicMock(return_value="b"))
    monkeypatch.setattr(
        messages.sync,
        "fetch_from_container",
        MagicMock(
            return_value=FetchResult(
                branch="b", old_oid=None, new_oid="a" * 40, base_oid=None, commits_added=1
            )
        ),
    )

    def imported(*a, **k):
        kid["config"][labels.LAST_IMPORT] = "a" * 40
        return importing.Imported("refs/jailbee/from/kid/b", "a" * 40)

    monkeypatch.setattr(importing, "import_child", imported)
    rig.incus.config_set.side_effect = lambda full, key, value: kid["config"].__setitem__(
        key, value
    )
    raw = {"version": 1, "type": "message", "id": "d1", "kind": "done", "body": "finished"}
    rig.requests["proj-kid"] = [Request("d1.json", json.dumps(raw), {})]
    rig.seq.attach_mock(destroy, "destroy")
    rig.tick()
    names = [c[0] for c in rig.seq.mock_calls]
    assert "destroy" in names and names.index("deliver") < names.index("destroy")


@pytest.mark.parametrize("failure", ["parked_reply", "crash_after_journal"])
def test_done_cleanup_waits_for_durable_reply(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from jailbee.orchestration import messages, outbox
    from jailbee.sync import FetchResult

    kid = _marked_child(rig)
    for row in rig.incus.list_containers.return_value:
        row["config"]["volatile.uuid"] = row["name"]
    kid["config"].pop(labels.PENDING_DESTROY)
    kid["config"].pop(labels.LAST_IMPORT)
    destroy = _destroy_seams(monkeypatch)
    monkeypatch.setattr(broker, "dispatch", actions.dispatch)
    monkeypatch.setattr(messages.sync, "_container_current_branch", MagicMock(return_value="b"))
    monkeypatch.setattr(
        messages.sync,
        "fetch_from_container",
        MagicMock(
            return_value=FetchResult(
                branch="b", old_oid=None, new_oid="a" * 40, base_oid=None, commits_added=1
            )
        ),
    )
    from jailbee.orchestration import importing

    def imported(*a, **k):
        kid["config"][labels.LAST_IMPORT] = "a" * 40
        return importing.Imported("refs/jailbee/from/kid/b", "a" * 40)

    monkeypatch.setattr(importing, "import_child", imported)
    rig.incus.config_set.side_effect = lambda full, key, value: kid["config"].__setitem__(
        key, value
    )
    raw = {"version": 1, "type": "message", "id": "d1", "kind": "done", "body": "finished"}
    rig.requests["proj-kid"] = [Request("d1.json", json.dumps(raw), {})]
    if failure == "parked_reply":
        monkeypatch.setattr(broker, "deliver", outbox.deliver)

        monkeypatch.setattr(outbox, "insert_into_store", lambda *a, **k: False)
    else:
        rig.deliver.side_effect = RuntimeError("crash after journal")
    rig.tick()
    destroy.assert_not_called()
    # A fresh broker round must replay the reply without dispatching done again.
    rig.deliver.side_effect = None
    rig.deliver.return_value = "delivered"
    monkeypatch.setattr(outbox, "insert_into_store", lambda *a, **k: True)
    monkeypatch.setattr(broker, "flush_pending", outbox.flush_pending)
    rig.tick()
    assert not broker.has_pending(rig.root, "proj-orch")
    assert not broker.has_pending(rig.root, "proj-kid")
    destroy.assert_called_once()


def test_vanished_orchestrator_finish_notifies_child(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration import outbox
    from jailbee.orchestration.protocol import PendingFinish
    from jailbee.orchestration.state import pending_dir

    destroy = _destroy_seams(monkeypatch)
    directory = pending_dir(rig.root, "proj-orch")
    directory.mkdir(parents=True)
    item = PendingFinish(
        version=1,
        type="finish",
        id="d1",
        child_full="proj-kid",
        child_short="kid",
        branch="b",
        done_sha="a" * 40,
        body="finished",
        in_reply_to=None,
        uid=1000,
    )
    outbox.save_pending(directory / "a.finish.json", item, recipient_uuid="uuid-orch")
    notice = MagicMock()
    monkeypatch.setattr(outbox, "deliver", notice)
    rig.incus.list_containers.return_value = [_child()]
    rig.tick()
    assert "was destroyed" in notice.call_args.args[3].body
    assert notice.call_args.args[2] == "proj-kid"
    destroy.assert_not_called()
    assert not directory.exists()


@pytest.mark.parametrize("key", [labels.STOPPED_DIRTY, "user.jailbee.orch.stopped_outbox"])
def test_tick_clears_stale_stop_record(rig: Rig, key: str) -> None:
    kid = _child()
    kid["config"][key] = "0"
    rig.incus.list_containers.return_value = [_orch(), kid]
    rig.tick()
    rig.incus.config_unset_checked.assert_any_call("proj-kid", key)


def test_tick_clears_stopped_head_of_a_running_child(rig: Rig) -> None:
    """A child started outside the broker (manual `jb start`) must not keep a
    STOPPED_HEAD that no longer describes it (spec 7.3 as amended)."""
    kid = _child()
    kid["config"][labels.STOPPED_HEAD] = "a" * 40
    stopped = _child("proj-kid2", running=False)
    stopped["config"][labels.STOPPED_HEAD] = "b" * 40
    rig.incus.list_containers.return_value = [_orch(), kid, stopped]
    rig.tick()
    rig.incus.config_unset_checked.assert_called_once_with("proj-kid", labels.STOPPED_HEAD)


def test_stale_stopped_head_is_cleared_before_anything_is_served(rig: Rig) -> None:
    """Cleared up front, so a `stop` served later in the same round keeps the
    STOPPED_HEAD it records."""
    kid = _child()
    kid["config"][labels.STOPPED_HEAD] = "a" * 40
    rig.incus.list_containers.return_value = [_orch(), kid]
    rig.seq.attach_mock(rig.incus.config_unset_checked, "unset")
    rig.tick()
    names = [c[0] for c in rig.seq.mock_calls]
    assert names.index("unset") < names.index("serve_requests")


def test_failed_stopped_head_clear_does_not_stop_the_round(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    from jailbee.incus import IncusError

    kid = _child()
    kid["config"][labels.STOPPED_HEAD] = "a" * 40
    rig.incus.list_containers.return_value = [_orch(), kid]
    rig.incus.config_unset_checked.side_effect = IncusError("daemon gone")
    rig.tick()
    assert rig.serve_requests.call_count == 2


# -- watch list: started children ------------------------------------------------


def _restart_raw(mid: str = "r1", child: str = "kid") -> Request:
    text = json.dumps(
        {"version": 1, "id": mid, "type": "restart", "child": child, "prompt": "again"}
    )
    return Request(f"{mid}.json", text, {f"{mid}.json": text})


def _watches(rig: Rig) -> list[watch.WatchEntry]:
    return watch.load_watch(rig.root).get("proj-orch", [])


def _job(
    phase: str = "failed", *, started_at: datetime = NOW, error_msg: str | None = "boom"
) -> Any:
    return watch.JobSnapshot(phase, 7, JOB_CREATE, error_msg, started_at)


def _spawn_round(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.requests["proj-orch"] = [_spawn_raw()]
    rig.tick()


@pytest.mark.parametrize("accepted", [True, False])
def test_only_accepted_done_cancels_supervision(rig: Rig, monkeypatch, accepted) -> None:
    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, confirmed=True
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    text = json.dumps(
        {"version": 1, "id": "done1", "type": "message", "kind": "done", "body": "finished"}
    )
    rig.requests["proj-kid"] = [Request("done1.json", text, {"done1.json": text})]
    rig.dispatch.return_value = Outcome("done" if accepted else "refused", "result")
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    rig.tick()
    assert _watches(rig) == ([] if accepted else [entry])


def test_idle_report_contains_last_accepted_message_and_tail(rig: Rig, monkeypatch) -> None:
    from datetime import timedelta

    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, confirmed=True
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    text = json.dumps(
        {
            "version": 1,
            "id": "n1",
            "type": "message",
            "kind": "note",
            "body": "Need your answer\nsecond line",
        }
    )
    rig.requests["proj-kid"] = [Request("n1.json", text, {"n1.json": text})]
    rig.dispatch.return_value = Outcome("done", "delivered")
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    monkeypatch.setattr(agent_watch, "idle_signal", lambda *a, **k: "turn")
    monkeypatch.setattr(agent_watch, "output_tail", lambda *a, **k: "prompt pane")
    rig.tick()
    assert _watches(rig)[0].last_message_kind == "note"
    assert _watches(rig)[0].last_message_line == "Need your answer"
    rig.requests.clear()
    rig.deliver.reset_mock()
    broker.tick(rig.incus, root=rig.root, journal=rig.journal, now=NOW + timedelta(seconds=900))
    msg = rig.deliver.call_args.args[3]
    assert msg.kind == "idle" and msg.from_ == "kid"
    assert "15m" in msg.body and "note Need your answer" in msg.body and "prompt pane" in msg.body
    rig.deliver.reset_mock()
    broker.tick(rig.incus, root=rig.root, journal=rig.journal, now=NOW + timedelta(seconds=901))
    rig.deliver.assert_not_called()


@pytest.mark.parametrize("kind,accepted", [("blocked", True), ("note", True), ("done", False)])
def test_child_message_idle_reset_only_when_accepted(rig: Rig, monkeypatch, kind, accepted) -> None:
    entry = watch.WatchEntry(
        "s1",
        "proj-kid",
        "spawn",
        NOW.isoformat(),
        supervise=True,
        confirmed=True,
        idle_since=NOW.isoformat(),
        idle_token="turn",
        last_message_kind="blocked",
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    text = json.dumps(
        {"version": 1, "id": "m1", "type": "message", "kind": kind, "body": "line\nmore"}
    )
    rig.requests["proj-kid"] = [Request("m1.json", text, {"m1.json": text})]
    rig.dispatch.return_value = Outcome("done" if accepted else "refused", "result")
    monkeypatch.setattr(broker, "_settle_all_watches", lambda *a, **k: None)
    rig.tick()
    saved = _watches(rig)[0]
    assert saved.last_message_kind == (kind if accepted else "blocked")
    assert (saved.idle_since is None) is accepted


@pytest.mark.parametrize("signal", [None, "turn"])
@pytest.mark.parametrize("lifecycle", ["active", "done", "stop", "destroy", "blocked"])
def test_idle_supervision_suppression(rig: Rig, monkeypatch, signal, lifecycle) -> None:
    from datetime import timedelta

    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry(
        "s1",
        "proj-kid",
        "spawn",
        NOW.isoformat(),
        supervise=True,
        confirmed=True,
        idle_since=(NOW - timedelta(hours=1)).isoformat(),
        idle_token="turn",
        last_message_kind="blocked" if lifecycle == "blocked" else "note",
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    child = _child()
    if lifecycle == "done":
        child["config"][labels.STATE] = labels.STATE_DONE
    if lifecycle in ("stop", "destroy"):
        watch.cancel(rig.root, "proj-orch", "proj-kid")
    rig.incus.list_containers.return_value = [_orch(), child]
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    monkeypatch.setattr(agent_watch, "idle_signal", lambda *a, **k: signal)
    monkeypatch.setattr(agent_watch, "output_tail", lambda *a, **k: "")
    rig.tick()
    messages = [
        c.args[3] for c in rig.deliver.call_args_list if getattr(c.args[3], "kind", None) == "idle"
    ]
    assert bool(messages) is (lifecycle == "active" and signal is not None)


def test_idle_uses_repo_threshold_and_retries_delivery(rig: Rig, monkeypatch) -> None:
    from datetime import timedelta

    from jailbee.config.models_orchestration import OrchestrationView, RepoOrchestration
    from jailbee.orchestration import agent_watch

    monkeypatch.setattr(
        type(rig.cfg),
        "orchestration_view",
        lambda self: OrchestrationView(repo=RepoOrchestration(idle_after_seconds=60)),
    )
    entry = watch.WatchEntry(
        "s1",
        "proj-kid",
        "spawn",
        NOW.isoformat(),
        supervise=True,
        confirmed=True,
        idle_since=(NOW - timedelta(seconds=60)).isoformat(),
        idle_token="turn",
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    monkeypatch.setattr(agent_watch, "idle_signal", lambda *a, **k: "turn")
    monkeypatch.setattr(agent_watch, "output_tail", lambda *a, **k: "")
    rig.deliver.side_effect = RuntimeError("delivery unavailable")
    rig.tick()
    assert _watches(rig) == [entry]
    rig.deliver.side_effect = None
    rig.tick()
    assert rig.deliver.call_args.args[3].kind == "idle"
    assert "1m" in rig.deliver.call_args.args[3].body
    assert _watches(rig)[0].idle_reported_at == NOW.isoformat()


def test_startup_rechecks_container_after_queue_launch(rig: Rig, monkeypatch) -> None:
    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry("s1", "proj-kid", "restart", NOW.isoformat(), supervise=True)
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.side_effect = [
        [_orch(), _child(running=False)],
        [_orch(), _child(running=False)],
        [_orch(), _child(running=False)],  # env authority recheck, before queue launch
        [_orch(), _child()],
        [_orch(), _child()],
    ]
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    rig.tick()
    assert _watches(rig)[0].live_since is not None
    rig.deliver.assert_not_called()


def test_imported_done_survives_crash_before_watch_cancel(rig: Rig, monkeypatch) -> None:
    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, confirmed=True
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    child = _child(running=False)
    child["config"].update(
        {
            labels.STATE: labels.STATE_IMPORTED,
            labels.DONE_SHA: "a" * 40,
            labels.LAST_IMPORT: "a" * 40,
        }
    )
    rig.incus.list_containers.return_value = [_orch(), child]
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    rig.tick()
    assert _watches(rig) == []
    rig.deliver.assert_not_called()


def test_confirmed_agent_watch_deferred_if_job_database_is_unreadable(
    rig: Rig, monkeypatch
) -> None:
    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, confirmed=True
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(False, "claude", 0)
    )
    monkeypatch.setattr(agent_watch, "output_tail", lambda *a, **k: "")
    rig.tick()
    rig.deliver.assert_not_called()
    assert _watches(rig) == [entry]


@pytest.mark.parametrize(
    "phase,pid,blocked", [("refresh", 123, False), ("failed", 123, True), ("refresh", 0, True)]
)
def test_refresh_job_suppresses_dead_agent_only_while_live(rig, monkeypatch, phase, pid, blocked):
    from jailbee import background
    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, confirmed=True
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    monkeypatch.setattr(
        watch,
        "read_jobs",
        lambda: {"proj-kid": watch.JobSnapshot(phase, pid, "refresh", None, NOW)},
    )
    monkeypatch.setattr(background, "worker_alive", lambda pid: True)
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(False, "claude", 0)
    )
    monkeypatch.setattr(agent_watch, "output_tail", lambda *a, **k: "")
    rig.tick()
    assert bool(rig.deliver.call_count) is blocked
    assert bool(_watches(rig)) is not blocked


def test_refresh_claim_after_initial_snapshot_is_rechecked(rig, monkeypatch):
    from jailbee import background
    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, confirmed=True
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    snapshots = iter([{}, {"proj-kid": watch.JobSnapshot("refresh", 123, "refresh", None, NOW)}])
    monkeypatch.setattr(watch, "read_jobs", lambda: next(snapshots))
    monkeypatch.setattr(background, "worker_alive", lambda pid: True)
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(False, "claude", 0)
    )
    rig.tick()
    rig.deliver.assert_not_called()
    assert _watches(rig) == [entry]


@pytest.mark.parametrize("confirmed", [False, True])
def test_supervised_exit_reports_and_survives_delivery_failure(
    rig: Rig, monkeypatch, confirmed
) -> None:
    from jailbee.orchestration import agent_watch
    from jailbee.orchestration.protocol import InboxMessage

    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, confirmed=confirmed
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(False, "claude", 137)
    )
    monkeypatch.setattr(agent_watch, "output_tail", lambda *a, **k: "last output")
    rig.deliver.side_effect = RuntimeError("down")
    rig.tick()
    assert _watches(rig) == [entry]
    rig.deliver.side_effect = None
    rig.tick()
    item = rig.deliver.call_args.args[3]
    assert "SIGKILL" in item.body if confirmed else "SIGKILL" in item.detail
    assert "last output" in (item.body if confirmed else item.detail)
    if confirmed:
        assert isinstance(item, InboxMessage) and item.kind == "blocked"
    else:
        assert item.state == "failed"
    assert _watches(rig) == []


def test_started_spawn_is_watched(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-fix-1": _job("clone")})
    monkeypatch.setattr(background, "worker_alive", lambda pid: True)
    _spawn_round(rig)
    assert _watches(rig) == [
        watch.WatchEntry("s1", "proj-fix-1", "spawn", NOW.isoformat(), supervise=True)
    ]


def test_watch_since_is_not_after_a_row_created_during_dispatch(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job row is created inside dispatch, so `since` (the round's start) must not follow it."""
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-fix-1": _job(started_at=NOW)})
    _spawn_round(rig)
    [(_, status)] = [x for x in rig.statuses() if x[1].state == "failed"]
    assert status.detail == "creation of child `fix-1` failed: boom — the child was removed"
    assert _watches(rig) == []


def test_refused_spawn_is_not_watched(rig: Rig) -> None:
    rig.dispatch.return_value = Outcome("refused", "nope")
    _spawn_round(rig)
    assert _watches(rig) == []


def test_inline_restart_also_requires_agent_confirmation(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    rig.incus.list_containers.return_value = [_orch(), _child(running=False)]
    rig.requests["proj-orch"] = [_restart_raw()]
    rig.dispatch.return_value = Outcome("done", "child `kid` restarted")
    rig.tick()
    assert _watches(rig) == [
        watch.WatchEntry("r1", "proj-kid", "restart", NOW.isoformat(), supervise=True)
    ]


def test_detached_restart_watched(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    rig.incus.list_containers.return_value = [_orch(), _child(running=False)]
    rig.requests["proj-orch"] = [_restart_raw()]
    rig.dispatch.return_value = Outcome("started", "child `kid` restarting")
    rig.tick()
    assert _watches(rig) == [
        watch.WatchEntry("r1", "proj-kid", "restart", NOW.isoformat(), supervise=True)
    ]


def test_drained_spawn_is_watched(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    rig.incus.list_containers.return_value = [_orch()]
    save_queue(rig.root, {"proj-orch": [_req("q1")]})
    rig.drain.return_value = [(_req("q1"), Outcome("started", "ok"))]
    rig.tick()
    assert _watches(rig) == [
        watch.WatchEntry("q1", "proj-fix-2", "spawn", NOW.isoformat(), supervise=True)
    ]


def _seed(rig: Rig, orchestrator: str = "proj-orch") -> None:
    watch.save_watch(
        rig.root,
        {orchestrator: [watch.WatchEntry("s1", "proj-fix-1", "spawn", NOW.isoformat())]},
    )


def test_failed_create_reported_to_running_orchestrator(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(rig)
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-fix-1": _job()})
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick()
    [(to, s)] = rig.statuses()
    assert (to, s.ref, s.state, s.detail) == (
        "proj-orch",
        "s1",
        "failed",
        "creation of child `fix-1` failed: boom — the child was removed",
    )
    assert rig.deliver.call_args.kwargs["running"] is True
    assert _watches(rig) == []


def test_failed_create_parked_for_stopped_orchestrator(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(rig)
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-fix-1": _job()})
    rig.incus.list_containers.return_value = [_orch(running=False)]
    rig.tick()
    assert rig.deliver.call_args.kwargs["running"] is False
    assert _watches(rig) == []


def _worker_finishing_on_probe(
    monkeypatch: pytest.MonkeyPatch, name: str, phase: str = "autostart"
) -> dict[str, watch.JobSnapshot]:
    """A worker that completes between the job snapshot and the liveness probe.

    `_new-worker` deletes its row and only then exits, so by the time a probe
    finds its pid gone the row is gone too. The probe is where the race shows:
    it is the first moment after the snapshot that sees the exited worker.
    """
    jobs = {name: watch.JobSnapshot(phase, 7, JOB_CREATE, None, NOW)}

    def alive(pid: int) -> bool:
        jobs.pop(name, None)
        return False

    monkeypatch.setattr(watch, "read_jobs", lambda: dict(jobs))
    monkeypatch.setattr(background, "worker_alive", alive)
    return jobs


def test_create_finishing_after_job_snapshot_is_not_reported_failed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #71: a supervised spawn whose worker finishes mid-settlement is ready, not failed."""
    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry("s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True)
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    _worker_finishing_on_probe(monkeypatch, "proj-kid")
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    rig.tick()
    assert [s for _, s in rig.statuses() if s.state == "failed"] == []
    assert [e.child_full for e in _watches(rig)] == ["proj-kid"]


def test_unsupervised_create_finishing_after_job_snapshot_is_ready(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch.save_watch(
        rig.root, {"proj-orch": [watch.WatchEntry("s1", "proj-kid", "spawn", NOW.isoformat())]}
    )
    rig.incus.list_containers.return_value = [_orch(), _child()]
    _worker_finishing_on_probe(monkeypatch, "proj-kid")
    rig.tick()
    [(_, s)] = rig.statuses()
    assert (s.state, s.detail) == ("done", "child `kid` is ready")


def test_create_worker_gone_with_its_row_left_is_still_reported(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recheck must not hide a worker that really died mid-create."""
    watch.save_watch(
        rig.root, {"proj-orch": [watch.WatchEntry("s1", "proj-kid", "spawn", NOW.isoformat())]}
    )
    rig.incus.list_containers.return_value = [_orch(), _child()]
    row = watch.JobSnapshot("autostart", 7, JOB_CREATE, None, NOW)
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-kid": row})
    monkeypatch.setattr(background, "worker_alive", lambda pid: False)
    rig.tick()
    [(_, s)] = rig.statuses()
    assert s.state == "failed"
    assert "stopped unexpectedly during autostart" in s.detail


def test_vanished_worker_log_path_goes_to_the_broker_log(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    watch.save_watch(
        rig.root,
        {
            "proj-orch": [
                watch.WatchEntry("s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True)
            ]
        },
    )
    rig.incus.list_containers.return_value = [_orch(), _child()]
    row = watch.JobSnapshot("autostart", 7, JOB_CREATE, None, NOW, "/state/logs/proj-kid-1.log")
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-kid": row})
    monkeypatch.setattr(background, "worker_alive", lambda pid: False)
    with caplog.at_level(logging.WARNING):
        rig.tick()
    [(_, s)] = rig.statuses()
    assert "/state/logs" not in s.detail
    assert any(
        "proj-kid" in r.getMessage() and "/state/logs/proj-kid-1.log" in r.getMessage()
        for r in caplog.records
    )


def test_unreadable_job_db_reports_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(rig)
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    rig.incus.list_containers.return_value = [_orch()]
    memory = broker.BrokerMemory()
    with caplog.at_level(logging.WARNING):
        rig.tick(memory)
        rig.tick(memory)
    rig.deliver.assert_not_called()
    assert len(_watches(rig)) == 1
    assert sum("job database" in r.message for r in caplog.records) == 1


def test_reply_before_save(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(rig)
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-fix-1": _job()})
    rig.incus.list_containers.return_value = [_orch()]
    rig.deliver.side_effect = RuntimeError("inbox down")
    rig.tick()
    rig.deliver.assert_called_once()
    assert len(_watches(rig)) == 1


def test_vanished_orchestrator_watches_dropped(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(rig, "proj-gone")
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick()
    assert watch.load_watch(rig.root) == {}
    rig.deliver.assert_not_called()


def test_vanished_orchestrator_watches_dropped_on_early_return(rig: Rig) -> None:
    """No candidates, queues or parked messages: the round returns early, yet cleans up."""
    _seed(rig, "proj-gone")
    rig.incus.list_containers.return_value = []
    assert rig.tick() is False
    assert watch.load_watch(rig.root) == {}


def test_tick_destroys_marked_child_despite_quarantined_finish_file(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration.state import pending_dir

    _marked_child(rig)
    destroy = _destroy_seams(monkeypatch)
    directory = pending_dir(rig.root, "proj-orch")
    directory.mkdir(parents=True)
    (directory / "a.finish.json.bad").write_text("invalid")
    rig.tick()
    destroy.assert_called_once_with(rig.cfg, rig.incus, "proj-kid", force=True)


def _orphan_finish(root: Path, orch_full: str, child_full: str) -> Path:
    from jailbee.orchestration.protocol import PendingFinish, finish_file_name
    from jailbee.orchestration.state import pending_dir

    item = PendingFinish(
        version=1,
        type="finish",
        id=f"f-{child_full}",
        child_full=child_full,
        child_short=child_full.removeprefix("proj-"),
        branch="b",
        done_sha="a" * 40,
        body="done",
        in_reply_to=None,
        uid=1000,
    )
    path = pending_dir(root, orch_full) / finish_file_name(item, datetime.now(UTC), seq=0)
    outbox.save_pending(path, item, recipient_uuid="uuid-orch")
    return path


def test_orphaned_finish_failure_does_not_block_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from jailbee.orchestration import finishing

    first = _orphan_finish(tmp_path, "proj-gone", "proj-kid1")
    second = _orphan_finish(tmp_path, "proj-gone", "proj-kid2")
    incus = MagicMock()

    def unset(full: str, key: str) -> None:
        if full == "proj-kid1":
            raise IncusError("no such container")

    incus.config_unset_checked.side_effect = unset
    with caplog.at_level(logging.WARNING):
        retry = finishing.notify_orphaned_finishes(
            incus,
            tmp_path,
            {"proj-kid1", "proj-kid2"},
            {"proj-kid1", "proj-kid2"},
            now=datetime.now(UTC),
            seq=iter(range(100)),
            identities={"proj-kid1": "uuid-1", "proj-kid2": "uuid-2"},
        )
    store = BrokerStore(store_path(tmp_path))
    assert store.unacked("uuid-1") == []
    assert store.unacked("uuid-2")[0].item["kind"] == "note"
    incus.exec_with_input.assert_not_called()
    assert first.exists() and not second.exists()
    assert retry == {"proj-gone"}
    assert "proj-kid1" in caplog.text


def test_tick_survives_orphaned_finish_failure(rig: Rig) -> None:
    path = _orphan_finish(rig.root, "proj-gone", "proj-kid")
    rig.incus.list_containers.return_value = [_orch(grant=Grant()), _child()]
    rig.incus.config_unset_checked.side_effect = IncusError("deleted meanwhile")
    rig.tick()
    assert path.exists()


def test_grant_view_tracks_effective_default(rig):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    rig.incus.list_containers.return_value = [_orch()]
    memory = broker.BrokerMemory()
    rig.cfg._orchestration_view = OrchestrationView(
        default_runner=Runner(agent="claude", model="haiku"), default_runner_source="repo"
    )
    rig.tick(memory)
    data = broker_grant(memory)
    assert data["default_runner"] == {"agent": "claude", "model": "haiku"}
    assert data["default_runner_source"] == "repo"
    rig.cfg._orchestration_view = OrchestrationView(
        default_runner=Runner(agent="claude", model="opus"), default_runner_source="repo"
    )
    rig.tick(memory)
    rig.incus.exec_with_input.assert_not_called()


def test_grant_view_tracks_source_changes(rig):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    runner = Runner(agent="claude", model="haiku")
    rig.incus.list_containers.return_value = [_orch()]
    memory = broker.BrokerMemory()
    rig.cfg._orchestration_view = OrchestrationView(
        default_runner=runner, default_runner_source="repo"
    )
    rig.tick(memory)
    first = broker_grant(memory)
    rig.cfg._orchestration_view = OrchestrationView(
        default_runner=runner, default_runner_source="local"
    )
    rig.tick(memory)
    second = broker_grant(memory)
    rig.incus.exec_with_input.assert_not_called()
    assert first["default_runner"] == second["default_runner"]
    assert first["default_runner_source"] == "repo"
    assert second["default_runner_source"] == "local"


def test_failed_profile_restart_boot_reports_failed_for_the_same_ref(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart answers `started`; its boot worker failing later adds `failed` for that ref."""
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    rig.incus.list_containers.return_value = [_orch(), _child(running=False)]
    rig.requests["proj-orch"] = [_restart_raw()]
    rig.dispatch.return_value = Outcome("started", "child `kid` restarting")
    rig.tick()
    assert [s.state for _, s in rig.statuses()] == ["started"]

    failed_boot = watch.JobSnapshot("failed", 7, JOB_BOOT, "setup failed after boot", NOW)
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-kid": failed_boot})
    rig.requests["proj-orch"] = []
    rig.tick()
    final = [(s.ref, s.detail) for _, s in rig.statuses() if s.state == "failed"]
    assert final == [("r1", "restart of child `kid` failed: setup failed after boot")]
    assert _watches(rig) == []


def test_supervision_records_new_session_once(rig: Rig, monkeypatch) -> None:
    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, confirmed=True
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    child = _child()
    rig.incus.list_containers.return_value = [_orch(), child]
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    ids = ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]
    current = [ids[0]]
    monkeypatch.setattr(agent_watch, "current_session", lambda *a: current[0], raising=False)

    def write(name, key, value):
        child["config"][key] = value

    rig.incus.config_set.side_effect = write
    rig.tick()
    rig.tick()
    assert rig.incus.config_set.call_count == 1
    current[0] = ids[1]
    rig.tick()
    assert rig.incus.config_set.call_count == 2
    assert rig.incus.config_set.call_args.args == (
        "proj-kid",
        labels.AGENT_SESSIONS,
        json.dumps(ids),
    )


def test_session_write_failure_does_not_break_supervision(rig: Rig, monkeypatch) -> None:
    from jailbee.orchestration import agent_watch

    entry = watch.WatchEntry(
        "s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, agent_only=True
    )
    watch.save_watch(rig.root, {"proj-orch": [entry]})
    rig.incus.list_containers.return_value = [_orch(), _child()]
    monkeypatch.setattr(watch, "read_jobs", lambda: {})
    monkeypatch.setattr(
        agent_watch, "probe", lambda *a, **k: agent_watch.AgentStatus(True, "claude")
    )
    monkeypatch.setattr(
        agent_watch,
        "current_session",
        lambda *a: "00000000-0000-0000-0000-000000000001",
        raising=False,
    )
    rig.incus.config_set.side_effect = RuntimeError("write failure")
    rig.tick()
    assert _watches(rig)[0].live_since == NOW.isoformat()
    rig.incus.config_set.assert_called_once_with(
        "proj-kid", labels.AGENT_SESSIONS, json.dumps(["00000000-0000-0000-0000-000000000001"])
    )


@pytest.mark.parametrize("changed,failure", [(True, False), (False, False), (False, True)])
def test_broker_start_stages_and_syncs_registered_repos(
    tmp_path, monkeypatch, caplog, changed, failure
):
    from jailbee import container_app

    cfg1, cfg2 = MagicMock(), MagicMock()
    sync = MagicMock()
    stage = MagicMock(
        side_effect=OSError("stage broke") if failure else None,
        return_value=container_app.StageResult(tmp_path, changed),
    )
    monkeypatch.setattr(container_app, "stage", stage)
    monkeypatch.setattr("jailbee.agent_skills.sync_agent_skills", sync)
    monkeypatch.setattr(broker, "repo_configs", lambda memory: {"a": cfg1, "b": cfg2})
    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    tick = MagicMock(return_value=False)
    monkeypatch.setattr(broker, "tick", tick)
    monkeypatch.setattr(broker, "_installed_version", lambda: None)
    monkeypatch.setattr(broker, "_pause", lambda seconds, stop: _sigterm())
    assert broker.run_broker(MagicMock()) == 0
    stage.assert_called_once_with()
    tick.assert_called_once()
    assert sync.call_args_list == ([mock_call(cfg1), mock_call(cfg2)] if not failure else [])
    if failure:
        assert "staging failed" in caplog.text


def test_nodes_carry_state_sampling_fields() -> None:
    raw: list[dict[str, object]] = [
        {
            "name": "proj-a",
            "status": "Running",
            "state": {"pid": 42, "cpu": {"usage": 7}, "memory": {"usage": 1024}},
        },
        {"name": "proj-b", "status": "Stopped", "state": None},
        {
            "name": "proj-c",
            "status": "Running",
            "state": {"pid": True, "cpu": {"usage": 0}, "memory": {"usage": -1}},
        },
    ]
    nodes = broker._nodes(raw, {})
    assert nodes["proj-a"].init_pid == 42
    assert nodes["proj-a"].cpu_usage_ns == 7
    assert nodes["proj-a"].memory_usage == 1024
    assert nodes["proj-b"].init_pid is None
    assert nodes["proj-b"].cpu_usage_ns is None
    assert nodes["proj-b"].memory_usage is None
    assert nodes["proj-c"].init_pid is None
    assert nodes["proj-c"].cpu_usage_ns is None
    assert nodes["proj-c"].memory_usage is None


def test_tick_prunes_status_cache_of_vanished_containers(rig: Rig) -> None:
    memory = broker.BrokerMemory()
    memory.status.host_at.update({"proj-kid": 1.0, "proj-gone": 1.0})
    memory.status.exec_at.update({"proj-gone": 1.0})
    memory.status.sampler_for("proj-gone-orch")
    rig.incus.list_containers.return_value = [_orch(), _child()]
    rig.tick(memory)
    assert set(memory.status.host_at) == {"proj-kid"}
    assert set(memory.status.exec_at) == {"proj-kid"}
    assert "proj-gone-orch" not in memory.status.samplers


@pytest.mark.parametrize("kind", ["stop", "destroy", "restart", "spawn"])
def test_lifecycle_action_refreshes_children_snapshot_before_the_reply(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    sync = MagicMock()
    monkeypatch.setattr(broker.inbox_state, "sync_children", sync)
    rig.seq.attach_mock(sync, "sync_children")
    before = [_orch(), _child()]
    after = [_orch(), _child(running=False)]
    rig.incus.list_containers.side_effect = [before] + [after] * 10
    extra: dict[str, Any] = {"restart": {"prompt": "p"}}.get(kind, {})
    text = json.dumps({"version": 1, "id": "m1", "type": kind, "child": "kid", **extra})
    if kind == "spawn":
        text = _spawn_raw("m1", prompt_file=False).text
    rig.requests["proj-orch"] = [Request("m1.json", text, {"m1.json": text})]
    rig.dispatch.return_value = Outcome("done", "child `kid` handled")
    rig.tick()
    names = [c[0] for c in rig.seq.mock_calls]
    assert names.index("sync_children") < names.index("deliver")
    seen = sync.call_args_list[0].args[0]
    assert seen.nodes["proj-kid"].running is False


def test_refused_lifecycle_action_does_not_resync_early(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync = MagicMock()
    monkeypatch.setattr(broker.inbox_state, "sync_children", sync)
    rig.seq.attach_mock(sync, "sync_children")
    rig.incus.list_containers.return_value = [_orch(), _child()]
    text = json.dumps({"version": 1, "id": "m1", "type": "stop", "child": "kid"})
    rig.requests["proj-orch"] = [Request("m1.json", text, {"m1.json": text})]
    rig.dispatch.return_value = Outcome("refused", "nope")
    rig.tick()
    names = [c[0] for c in rig.seq.mock_calls]
    assert names.index("deliver") < names.index("sync_children")


def broker_grant(memory):
    from jailbee.orchestration.socket_ops import handle_grant

    return handle_grant(memory, "proj-orch")


@pytest.mark.parametrize("kind", ["labelled", "unlabelled", "bad-grant", "former-publisher"])
@pytest.mark.parametrize("running", [True, False])
def test_pending_reaches_store_without_socket_registration(rig, monkeypatch, kind, running):
    from jailbee.orchestration import outbox
    from jailbee.orchestration.protocol import InboxStatus
    from jailbee.orchestration.state import atomic_write_text, pending_dir

    config = {"volatile.uuid": "uuid-current"}
    if kind == "labelled":
        config[labels.GRANT] = GRANT.to_label()
    elif kind == "bad-grant":
        config[labels.GRANT] = "{invalid"
    elif kind == "former-publisher":
        atomic_write_text(rig.root / "publish-known.json", '{"proj-orch":"uuid-current"}')
    rig.incus.list_containers.return_value = [_c("proj-orch", running=running, **config)]
    # A previous same-named instance's row must never select the delivery identity.
    rig.store.set_recipient("proj-orch", "uuid-stale", now=1)
    item = InboxStatus(
        version=1, id="h-queued", type="status", ref="queued", state="done", detail="ok"
    )
    path = pending_dir(rig.root, "proj-orch") / "a.json"
    outbox.save_pending(path, item, recipient_uuid="uuid-current")
    monkeypatch.setattr(broker, "flush_pending", outbox.flush_pending)
    rig.tick()
    assert [row.id for row in rig.store.unacked("uuid-current")] == [item.id]
    assert rig.store.unacked("uuid-stale") == []
    assert not path.exists()
    # Store delivery never authorizes a new socket identity.
    assert rig.store.recipients() == {"proj-orch": "uuid-stale"}
    rig.incus.exec_with_input.assert_not_called()


@pytest.mark.parametrize("labelled", [True, False])
def test_pending_without_inventory_uuid_never_uses_stale_socket_identity(
    rig, monkeypatch, labelled
):
    from jailbee.orchestration import outbox
    from jailbee.orchestration.state import atomic_write_text, pending_dir

    rig.incus.list_containers.return_value = [
        _orch(running=False) if labelled else _c("proj-orch", running=False)
    ]
    rig.store.set_recipient("proj-orch", "uuid-stale", now=1)
    path = pending_dir(rig.root, "proj-orch") / "a.json"
    atomic_write_text(path, '{"id":"h-queued","type":"status"}')
    monkeypatch.setattr(broker, "flush_pending", outbox.flush_pending)
    rig.tick()
    assert path.exists()
    assert rig.store.unacked("uuid-stale") == []
    rig.incus.exec_with_input.assert_not_called()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("finish_item", [False, True])
def test_recreated_namesake_never_receives_old_spool_or_executes_finish(
    rig, monkeypatch, restart, finish_item
):
    from jailbee.orchestration import finishing
    from jailbee.orchestration.protocol import InboxStatus, PendingFinish
    from jailbee.orchestration.store import BrokerStore, StoreError

    old = _orch(running=False)
    old["config"]["volatile.uuid"] = "uuid-old"
    rig.incus.list_containers.return_value = [old]
    rig.tick()
    ctx = rig.memory.contexts["uuid-old"]
    if finish_item:
        item = PendingFinish(
            version=1,
            type="finish",
            id="done-old",
            child_full="proj-kid",
            child_short="kid",
            branch="b",
            done_sha="a" * 40,
            body="old",
            uid=None,
            in_reply_to=None,
        )
        path = finishing.park(ctx, ctx.sender, item)
    else:
        item = InboxStatus(
            version=1, type="status", id="h-old", ref="old", state="done", detail="private"
        )
        with monkeypatch.context() as fail:
            fail.setattr(BrokerStore, "insert_item", MagicMock(side_effect=StoreError("offline")))
            assert (
                outbox.deliver(
                    rig.incus,
                    rig.root,
                    "proj-orch",
                    item,
                    running=False,
                    uid=None,
                    now=NOW,
                    recipient_uuid="uuid-old",
                )
                == "parked"
            )
        path = next((rig.root / "pending" / "proj-orch").glob("*.json"))
    assert outbox.load_pending(path).recipient_uuid == "uuid-old"
    new = _orch()  # No intermediate listing ever observes the name absent.
    new["config"]["volatile.uuid"] = "uuid-new"
    rig.incus.list_containers.return_value = [new]
    complete = MagicMock(side_effect=AssertionError("old work must not run for a new parent"))
    monkeypatch.setattr(finishing, "complete", complete)
    monkeypatch.setattr(broker, "flush_pending", outbox.flush_pending)
    memory = broker.BrokerMemory() if restart else rig.memory
    rig.tick(memory)
    assert rig.store.unacked("uuid-new") == []
    assert rig.store.unacked("uuid-old") == []
    complete.assert_not_called()
    assert not path.exists() and path.with_name(path.name + ".bad").exists()
    rig.tick(broker.BrokerMemory())
    assert rig.store.unacked("uuid-new") == []
    complete.assert_not_called()
