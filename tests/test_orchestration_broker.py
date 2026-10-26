from __future__ import annotations

import json
import logging
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, MagicMock

import pytest

from jailbee import background
from jailbee.db.models import JOB_CREATE
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.orchestration import actions, broker, labels, watch
from jailbee.orchestration.actions import Outcome
from jailbee.orchestration.grant import Grant, RepoCap
from jailbee.orchestration.journal import Journal, manifest_digest
from jailbee.orchestration.outbox import RawManifest
from jailbee.orchestration.protocol import InboxStatus
from jailbee.orchestration.state import QueuedRequest, load_queue, save_queue
from tests.conftest import make_config

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


def _spawn_raw(mid: str = "s1", *, prompt_file: bool = True) -> RawManifest:
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
    return RawManifest(file_name=f"{mid}.json", text=text, files=files)


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path / "state"
        self.root.mkdir()
        self.cfg = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
        self.journal = Journal(self.root)
        self.incus = MagicMock()
        self.incus.list_containers.return_value = []
        self.incus.exists.return_value = False
        self.outboxes: dict[str, list[RawManifest] | Exception] = {}
        self.seq = MagicMock()  # parent of every seam, to assert call order
        self.read_outbox = MagicMock(side_effect=self._read)
        self.deliver = MagicMock()
        self.flush_pending = MagicMock(return_value=0)
        self.delete = MagicMock()
        self.write_grant = MagicMock()
        self.ensure_env = MagicMock()
        self.dispatch = MagicMock(
            return_value=Outcome("started", "creation of child `fix-1` started")
        )
        self.drain = MagicMock(return_value=[])
        self.drop = MagicMock(return_value=[])
        self.configs = MagicMock(return_value={"proj": self.cfg})
        self.gcfg = MagicMock(return_value=(GlobalConfig(), []))
        for name, mock in [
            ("read_outbox", self.read_outbox),
            ("deliver", self.deliver),
            ("flush_pending", self.flush_pending),
            ("delete_outbox_files", self.delete),
            ("write_grant_json", self.write_grant),
            ("ensure_orchestration_env", self.ensure_env),
            ("dispatch", self.dispatch),
            ("drain_queue", self.drain),
            ("drop_queue", self.drop),
            ("repo_configs", self.configs),
            ("load_global_config", self.gcfg),
        ]:
            monkeypatch.setattr(broker, name, mock)
            self.seq.attach_mock(mock, name)

    def _read(self, incus: object, full: str, *, uid: int | None) -> list[RawManifest]:
        got = self.outboxes.get(full, [])
        if isinstance(got, Exception):
            raise got
        return got

    def tick(self, memory: broker.BrokerMemory | None = None) -> bool:
        return broker.tick(self.incus, root=self.root, journal=self.journal, now=NOW, memory=memory)

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


def test_ensure_env_runs_for_orchestrator_before_grant_json(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick()
    rig.ensure_env.assert_called_once_with(rig.incus, "proj-orch", uid=rig.cfg.container_user.uid)
    names = [c[0] for c in rig.seq.mock_calls]
    assert names.index("ensure_orchestration_env") < names.index("flush_pending")
    assert names.index("ensure_orchestration_env") < names.index("write_grant_json")


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
    assert rig.write_grant.call_count == 1
    assert rig.read_outbox.call_count == 2
    assert len([r for r in caplog.records if "inbox helper" in r.getMessage()]) == 1


def test_ensure_env_rerun_after_container_vanishes(rig: Rig) -> None:
    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    rig.incus.list_containers.return_value = []
    rig.tick(memory)
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    assert rig.ensure_env.call_count == 2


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
    rig.outboxes["proj-x-orch"] = [_spawn_raw("s1")]
    memory = broker.BrokerMemory()
    with caplog.at_level(logging.WARNING, logger="jailbee.orchestration.broker"):
        rig.tick(memory)
        rig.tick(memory)
    rig.read_outbox.assert_not_called()
    rig.flush_pending.assert_not_called()
    rig.write_grant.assert_not_called()
    rig.incus.exec.assert_not_called()
    rig.incus.exec_with_input.assert_not_called()
    assert len([r for r in caplog.records if "proj-x-orch" in r.getMessage()]) == 1


def test_child_resolves_through_its_orchestrators_config(rig: Rig, tmp_path: Path) -> None:
    other = make_config(tmp_path).model_copy(update={"container_prefix": "proj-x"})
    rig.configs.return_value = {"proj": rig.cfg, "proj-x": other}
    rig.incus.list_containers.return_value = [_orch(), _child("proj-x-feat", of="proj-orch")]
    msg = json.dumps({"version": 1, "id": "m1", "type": "message", "body": "hi"})
    rig.outboxes["proj-x-feat"] = [RawManifest("m1.json", msg, {"m1.json": msg})]
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


def test_invalid_manifest_name_is_skipped_and_logged_once(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    text = json.dumps({"version": 1, "id": "a", "type": "stop", "child": "kid"})
    rig.outboxes["proj-orch"] = [RawManifest("a\\b.json", text, {"a\\b.json": text})]
    memory = broker.BrokerMemory()
    with caplog.at_level(logging.WARNING, logger="jailbee.orchestration.broker"):
        rig.tick(memory)
        rig.tick(memory)
    rig.dispatch.assert_not_called()
    rig.delete.assert_not_called()
    rig.deliver.assert_not_called()
    assert len([r for r in caplog.records if "invalid name" in r.getMessage()]) == 1


def test_unparseable_grant_label_is_logged_once(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.incus.list_containers.return_value = [_c("proj-orch", **{labels.GRANT: "{nope"})]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    memory = broker.BrokerMemory()
    with caplog.at_level(logging.WARNING, logger="jailbee.orchestration.broker"):
        rig.tick(memory)
        rig.tick(memory)
    rig.read_outbox.assert_not_called()
    hits = [r for r in caplog.records if "unparseable" in r.getMessage()]
    assert len(hits) == 1 and "proj-orch" in hits[0].getMessage()


def test_grant_json_rewritten_after_container_vanishes(rig: Rig) -> None:
    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    rig.incus.list_containers.return_value = []
    rig.tick(memory)
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    assert rig.write_grant.call_count == 2


def test_grant_json_rewritten_after_grant_becomes_ineffective(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    with monkeypatch.context() as m:
        m.setattr(actions.profiles, "repo_cap", lambda cfg: RepoCap(allowed=False))
        rig.tick(memory)
    assert "proj-orch" not in memory.grant_written
    rig.tick(memory)
    assert rig.write_grant.call_count == 2


def test_tick_ignores_ordinary_containers(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_c("proj-plain"), _c("proj-other", running=False)]
    assert rig.tick() is False
    rig.incus.exec.assert_not_called()
    rig.incus.exec_with_input.assert_not_called()
    rig.read_outbox.assert_not_called()
    rig.flush_pending.assert_not_called()
    rig.dispatch.assert_not_called()


def test_tick_dispatches_and_replies_and_deletes(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    raw = _spawn_raw("s1")
    rig.outboxes["proj-orch"] = [raw]
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

    digest = manifest_digest("s1.json", raw.text, {"s1.md": "do the thing"})
    assert rig.journal.seen("proj-orch", digest)
    assert Journal(rig.root).seen("proj-orch", digest)  # on disk
    rig.delete.assert_called_once_with(
        rig.incus, "proj-orch", ["s1.json", "s1.md"], uid=rig.cfg.container_user.uid
    )


def test_journal_is_written_before_the_manifest_is_deleted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    seen_at_delete: list[bool] = []
    raw = rig.outboxes["proj-orch"][0]
    digest = manifest_digest("s1.json", raw.text, {"s1.md": "do the thing"})
    rig.delete.side_effect = lambda *a, **k: seen_at_delete.append(
        Journal(rig.root).seen("proj-orch", digest)
    )
    rig.tick()
    assert seen_at_delete == [True]


def test_tick_skips_journalled_manifest_and_deletes_it(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    raw = _spawn_raw("s1")
    rig.outboxes["proj-orch"] = [raw]
    digest = manifest_digest("s1.json", raw.text, {"s1.md": "do the thing"})
    rig.journal.record("proj-orch", digest, "s1", "started", NOW)

    rig.tick()
    rig.dispatch.assert_not_called()
    rig.deliver.assert_not_called()
    rig.delete.assert_called_once_with(
        rig.incus, "proj-orch", ["s1.json", "s1.md"], uid=rig.cfg.container_user.uid
    )


def test_digest_ignores_unrelated_outbox_files(rig: Rig) -> None:
    """A second manifest beside the first must not change the first one's digest."""
    rig.incus.list_containers.return_value = [_orch()]
    a = _spawn_raw("s1", prompt_file=False)
    rig.journal.record("proj-orch", manifest_digest("s1.json", a.text, {}), "s1", "done", NOW)
    crowded = RawManifest(a.file_name, a.text, {**a.files, "s2.json": "{}"})
    rig.outboxes["proj-orch"] = [crowded]
    rig.tick()
    rig.dispatch.assert_not_called()


def test_bad_manifest_gets_refused_status(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [RawManifest("bad-1.json", "{not json", {"bad-1.json": "{"})]
    rig.tick()
    rig.dispatch.assert_not_called()
    [(to, status)] = rig.statuses()
    assert to == "proj-orch"
    assert status.state == "refused" and status.ref == "bad-1"
    assert "invalid JSON" in status.detail
    rig.delete.assert_called_once_with(
        rig.incus, "proj-orch", ["bad-1.json"], uid=rig.cfg.container_user.uid
    )
    assert rig.journal.seen("proj-orch", manifest_digest("bad-1.json", "{not json", {}))


def test_queued_outcome_is_sent_as_queued_status(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    rig.dispatch.return_value = Outcome("queued", "queued at position 1")
    rig.tick()
    [(_, status)] = rig.statuses()
    assert (status.state, status.detail, status.ref) == ("queued", "queued at position 1", "s1")


def test_handler_crash_is_refused_not_retried(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    rig.dispatch.side_effect = RuntimeError("secret /host/path boom")
    rig.tick()
    [(_, status)] = rig.statuses()
    # Generic detail for the container; the exception text stays in the host log.
    assert (status.state, status.detail) == ("refused", "broker error")
    rig.delete.assert_called_once()


def test_flush_pending_runs_before_any_new_status(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    rig.tick()
    names = [c[0] for c in rig.seq.mock_calls if c[0] in {"flush_pending", "deliver"}]
    assert names == ["flush_pending", "deliver"]
    rig.flush_pending.assert_called_once_with(
        rig.incus, rig.root, "proj-orch", uid=rig.cfg.container_user.uid, finish=ANY
    )


def test_children_are_served_too(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch(running=False), _child()]
    msg = json.dumps({"version": 1, "id": "m1", "type": "message", "body": "hi"})
    rig.outboxes["proj-kid"] = [RawManifest("m1.json", msg, {"m1.json": msg})]
    rig.dispatch.return_value = Outcome("done", "delivered")
    assert rig.tick() is True
    ctx, manifest = rig.dispatch.call_args.args
    assert ctx.sender.full == "proj-kid" and manifest.type == "message"
    # The stopped orchestrator is neither read nor flushed.
    assert [c.args[1] for c in rig.read_outbox.call_args_list] == ["proj-kid"]
    assert [c.args[2] for c in rig.flush_pending.call_args_list] == ["proj-kid"]


def test_one_bad_container_does_not_stop_the_round(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.incus.list_containers.return_value = [_orch("proj-a"), _orch("proj-b")]
    rig.outboxes["proj-a"] = RuntimeError("exec exploded")
    rig.outboxes["proj-b"] = [_spawn_raw("s1")]
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
    rig.read_outbox.assert_not_called()


def test_tick_returns_true_with_a_running_child(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_child()]
    assert rig.tick() is True


def test_global_config_failure_skips_the_round(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    rig.gcfg.side_effect = ValueError("bad yaml")
    assert rig.tick() is True
    rig.read_outbox.assert_not_called()
    rig.dispatch.assert_not_called()


def test_gcfg_is_loaded_once_and_shared(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch("proj-a"), _orch("proj-b")]
    rig.outboxes["proj-a"] = [_spawn_raw("s1")]
    rig.outboxes["proj-b"] = [_spawn_raw("s2")]
    sentinel = GlobalConfig()
    rig.gcfg.return_value = (sentinel, [])
    rig.tick()
    rig.gcfg.assert_called_once()
    assert all(c.args[0].gcfg is sentinel for c in rig.dispatch.call_args_list)


def test_unregistered_repo_container_skipped(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.incus.list_containers.return_value = [_orch("stranger-orch")]
    rig.outboxes["stranger-orch"] = [_spawn_raw("s1")]
    memory = broker.BrokerMemory()
    with caplog.at_level(logging.WARNING, logger="jailbee.orchestration.broker"):
        rig.tick(memory)
        rig.tick(memory)
    rig.read_outbox.assert_not_called()
    rig.flush_pending.assert_not_called()
    rig.incus.exec.assert_not_called()
    rig.incus.exec_with_input.assert_not_called()
    hits = [r for r in caplog.records if "stranger-orch" in r.getMessage()]
    assert len(hits) == 1  # logged once, not every round


def test_grant_json_written_once_per_change(rig: Rig) -> None:
    memory = broker.BrokerMemory()
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    rig.tick(memory)
    assert rig.write_grant.call_count == 1
    full, text = rig.write_grant.call_args.args[1:3]
    assert full == "proj-orch"
    assert Grant.model_validate_json(text) == GRANT

    wider = Grant.model_validate({"max_running_children": 3, "operations": ["message", "spawn"]})
    rig.incus.list_containers.return_value = [_orch(grant=wider)]
    rig.tick(memory)
    rig.tick(memory)
    assert rig.write_grant.call_count == 2
    assert Grant.model_validate_json(rig.write_grant.call_args.args[2]) == wider


def test_grant_json_not_written_into_children(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_child()]
    rig.tick()
    rig.write_grant.assert_not_called()


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
    rig.outboxes["proj-orch"] = [RawManifest("st1.json", stop, {"st1.json": stop})]
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
    rig.write_grant.assert_not_called()
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
    rig.outboxes["proj-orch"] = [raw]

    def act(ctx: object, manifest: object) -> Outcome:
        _sigterm()  # arrives mid-handler
        return Outcome("started", "ok")

    rig.dispatch.side_effect = act
    before = signal.getsignal(signal.SIGTERM)
    assert broker.run_broker(rig.incus) == 0
    digest = manifest_digest("s1.json", raw.text, {"s1.md": "do the thing"})
    assert Journal(rig.root).seen("proj-orch", digest)
    rig.delete.assert_called_once()
    [(_, status)] = rig.statuses()
    assert status.state == "started"
    rig.incus.list_containers.assert_called_once()  # exactly one round
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
    rig.outboxes["proj-orch"] = [raw]
    rig.incus.list_containers.return_value = [old]
    rig.tick()
    rig.incus.list_containers.return_value = [new]  # destroyed and recreated
    rig.tick()
    assert rig.dispatch.call_count == 2


def test_journal_keyed_by_instance_uuid(rig: Rig) -> None:
    raw = _spawn_raw("s1")
    digest = manifest_digest(raw.file_name, raw.text, {"s1.md": raw.files["s1.md"]})
    rig.journal.record("uuid-1", digest, "s1", "started", NOW)
    orch = _orch()
    orch["config"]["volatile.uuid"] = "uuid-1"
    rig.incus.list_containers.return_value = [orch]
    rig.outboxes["proj-orch"] = [raw]
    rig.tick()
    rig.dispatch.assert_not_called()
    rig.delete.assert_called_once()


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
    rig.outboxes["proj-orch"] = [_spawn_raw("s1"), _spawn_raw("s2")]
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
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    rig.tick()
    rig.flush_pending.assert_called_once_with(
        rig.incus, rig.root, "proj-orch", uid=rig.cfg.container_user.uid, finish=ANY
    )
    rig.read_outbox.assert_not_called()
    rig.dispatch.assert_not_called()


def test_tick_does_not_flush_role_none_without_pending(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_c("proj-orch", **{labels.GRANT: "garbage"})]
    rig.tick()
    rig.flush_pending.assert_not_called()


def test_tick_context_shares_the_memory_sequence(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    memory = broker.BrokerMemory()
    rig.tick(memory)
    ctx = rig.dispatch.call_args.args[0]
    assert ctx.seq is memory.seq


def test_tick_flushes_pending_for_container_with_no_labels(rig: Rig) -> None:
    from jailbee.orchestration.state import pending_dir

    d = pending_dir(rig.root, "proj-orch")
    d.mkdir(parents=True)
    (d / "x.json").write_text("{}")
    # `jailbee grant revoke` removed the label entirely.
    rig.incus.list_containers.return_value = [_c("proj-orch")]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    rig.tick()
    rig.flush_pending.assert_called_once_with(
        rig.incus, rig.root, "proj-orch", uid=rig.cfg.container_user.uid, finish=ANY
    )
    rig.read_outbox.assert_not_called()
    rig.dispatch.assert_not_called()


def test_tick_skips_unlabelled_pending_without_repo_config(rig: Rig) -> None:
    from jailbee.orchestration.state import pending_dir

    d = pending_dir(rig.root, "other-box")
    d.mkdir(parents=True)
    (d / "x.json").write_text("{}")
    rig.incus.list_containers.return_value = [_c("other-box")]
    rig.tick()
    rig.flush_pending.assert_not_called()


def test_tick_completes_parked_finish_when_orchestrator_starts(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.orchestration import finishing, importing, outbox
    from jailbee.orchestration.actions import ActionContext, Node
    from jailbee.orchestration.protocol import PendingFinish

    orch = Node("proj-orch", "orch", True, {labels.GRANT: Grant().to_label()})
    child = Node("proj-kid", "kid", True, {labels.SPAWNED_BY: orch.full})
    ctx = ActionContext(rig.cfg, GlobalConfig(), rig.incus, rig.root, orch,
                        {orch.full: orch, child.full: child}, NOW)
    item = PendingFinish(version=1, type="finish", id="d1", child_full=child.full,
                         child_short="kid", branch="b", done_sha="a" * 40,
                         body="done", in_reply_to=None, uid=1000)
    finishing.park(ctx, orch, item)
    relay = MagicMock(return_value=importing.Imported("refs/jailbee/from/kid/b", "a" * 40))
    monkeypatch.setattr(importing, "import_child", relay)
    monkeypatch.setattr(broker, "flush_pending", outbox.flush_pending)
    rig.incus.list_containers.return_value = [_orch(grant=Grant()), _child()]
    rig.tick()
    relay.assert_called_once()
    delivered = [json.loads(c.args[2]) for c in rig.incus.exec_with_input.call_args_list]
    assert delivered[0]["kind"] == "done" and delivered[0]["imported_sha"] == "a" * 40


def test_tick_clears_stale_stopped_dirty(rig: Rig) -> None:
    kid = _child()
    kid["config"][labels.STOPPED_DIRTY] = "1"
    rig.incus.list_containers.return_value = [_orch(), kid]
    rig.tick()
    rig.incus.config_unset_checked.assert_any_call("proj-kid", labels.STOPPED_DIRTY)


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
    assert names.index("unset") < names.index("read_outbox")


def test_failed_stopped_head_clear_does_not_stop_the_round(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    from jailbee.incus import IncusError

    kid = _child()
    kid["config"][labels.STOPPED_HEAD] = "a" * 40
    rig.incus.list_containers.return_value = [_orch(), kid]
    rig.incus.config_unset_checked.side_effect = IncusError("daemon gone")
    rig.tick()
    assert rig.read_outbox.call_count == 2


# -- watch list: started children ------------------------------------------------


def _restart_raw(mid: str = "r1", child: str = "kid") -> RawManifest:
    text = json.dumps(
        {"version": 1, "id": mid, "type": "restart", "child": child, "prompt": "again"}
    )
    return RawManifest(f"{mid}.json", text, {f"{mid}.json": text})


def _watches(rig: Rig) -> list[watch.WatchEntry]:
    return watch.load_watch(rig.root).get("proj-orch", [])


def _job(
    phase: str = "failed", *, started_at: datetime = NOW, error_msg: str | None = "boom"
) -> Any:
    return watch.JobSnapshot(phase, 7, JOB_CREATE, error_msg, started_at)


def _spawn_round(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [_spawn_raw()]
    rig.tick()


def test_started_spawn_is_watched(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "read_jobs", lambda: {"proj-fix-1": _job("clone")})
    monkeypatch.setattr(background, "worker_alive", lambda pid: True)
    _spawn_round(rig)
    assert _watches(rig) == [watch.WatchEntry("s1", "proj-fix-1", "spawn", NOW.isoformat())]


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


def test_inline_restart_not_watched_when_no_live_job(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "live_job_kind", lambda name: None)
    rig.incus.list_containers.return_value = [_orch(), _child(running=False)]
    rig.outboxes["proj-orch"] = [_restart_raw()]
    rig.dispatch.return_value = Outcome("done", "child `kid` restarted")
    rig.tick()
    assert _watches(rig) == []


def test_detached_restart_watched(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "live_job_kind", lambda name: "boot")
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    rig.incus.list_containers.return_value = [_orch(), _child(running=False)]
    rig.outboxes["proj-orch"] = [_restart_raw()]
    rig.dispatch.return_value = Outcome("started", "child `kid` restarting")
    rig.tick()
    assert _watches(rig) == [watch.WatchEntry("r1", "proj-kid", "restart", NOW.isoformat())]


def test_drained_spawn_is_watched(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "read_jobs", lambda: None)
    rig.incus.list_containers.return_value = [_orch()]
    save_queue(rig.root, {"proj-orch": [_req("q1")]})
    rig.drain.return_value = [(_req("q1"), Outcome("started", "ok"))]
    rig.tick()
    assert _watches(rig) == [watch.WatchEntry("q1", "proj-fix-2", "spawn", NOW.isoformat())]


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
