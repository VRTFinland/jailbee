from __future__ import annotations

import json
import logging
import signal
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, broker, labels
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
        self.dispatch = MagicMock(return_value=Outcome("started", "child `fix-1` created"))
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


def test_config_for_picks_longest_prefix(tmp_path: Path) -> None:
    a = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
    b = make_config(tmp_path).model_copy(update={"container_prefix": "proj-x"})
    configs = {"proj": a, "proj-x": b}
    assert broker.config_for("proj-x-feat", configs) is b
    assert broker.config_for("proj-feat", configs) is a
    assert broker.config_for("projfeat", configs) is None
    assert broker.config_for("other-feat", configs) is None


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
    assert (status.ref, status.state, status.detail) == ("s1", "started", "child `fix-1` created")
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
    rig.dispatch.side_effect = RuntimeError("boom")
    rig.tick()
    [(_, status)] = rig.statuses()
    assert status.state == "refused" and "boom" in status.detail
    rig.delete.assert_called_once()


def test_flush_pending_runs_before_any_new_status(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch()]
    rig.outboxes["proj-orch"] = [_spawn_raw("s1")]
    rig.tick()
    names = [c[0] for c in rig.seq.mock_calls if c[0] in {"flush_pending", "deliver"}]
    assert names == ["flush_pending", "deliver"]
    rig.flush_pending.assert_called_once_with(
        rig.incus, rig.root, "proj-orch", uid=rig.cfg.container_user.uid
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
        (_req("q1"), Outcome("started", "child `fix-2` created")),
        (_req("q2", "proj-fix-3"), Outcome("refused", "container `fix-3` already exists")),
    ]
    rig.tick()
    rig.drain.assert_called_once()
    assert rig.drain.call_args.args[0].sender.full == "proj-orch"
    got = [(to, s.ref, s.state, s.detail) for to, s in rig.statuses()]
    assert got == [
        ("proj-orch", "st1", "done", "child `kid` stopped"),
        ("proj-orch", "q1", "started", "child `fix-2` created"),
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


def test_disallowed_repo_drops_queue_with_revoked_statuses(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        ("q1", "refused", "orchestration grant revoked"),
        ("q2", "refused", "orchestration grant revoked"),
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


def test_run_broker_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    monkeypatch.setattr(broker, "tick", MagicMock(side_effect=[False, True, False]))
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise broker.StopBroker

    monkeypatch.setattr(broker.time, "sleep", fake_sleep)
    assert broker.run_broker(MagicMock()) == 0
    assert sleeps == [60.0, 5.0]


def test_run_broker_exits_zero_on_sigterm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    monkeypatch.setattr(broker, "tick", MagicMock(return_value=True))
    before = signal.getsignal(signal.SIGTERM)

    def deliver_sigterm(seconds: float) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(broker.time, "sleep", deliver_sigterm)
    assert broker.run_broker(MagicMock()) == 0
    assert signal.getsignal(signal.SIGTERM) == before


def test_run_broker_survives_a_failed_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(broker, "state_root", lambda: tmp_path)
    tick = MagicMock(side_effect=[RuntimeError("incus down"), True])
    monkeypatch.setattr(broker, "tick", tick)
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise broker.StopBroker

    monkeypatch.setattr(broker.time, "sleep", fake_sleep)
    assert broker.run_broker(MagicMock()) == 0
    assert sleeps == [60.0, 5.0]
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
