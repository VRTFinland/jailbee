# ruff: noqa: F811
from __future__ import annotations

import pytest

from jailbee.config.models_orchestration import OrchestrationView, RepoOrchestration
from jailbee.orchestration import broker, labels
from tests.test_orchestration_broker import GRANT, NOW, _c
from tests.test_orchestration_socket_device import Env, env  # noqa: F401

DAY = 86400.0


def _orch(name: str, uuid: str) -> dict:
    return _c(name, **{labels.GRANT: GRANT.to_label(), "volatile.uuid": uuid})


def _item(i: str) -> dict:
    return {"version": 1, "id": i, "type": "message", "kind": "info", "text": "x"}


class Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.t = 1000.0
        monkeypatch.setattr(broker, "_monotonic", lambda: self.t)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def _put(env: Env, uuid: str, full: str, id_: str, age_days: float, *, acked: bool = True) -> None:
    now = NOW.timestamp()
    env.store.insert_item(
        uuid,
        full,
        _item(id_),
        now=now - age_days * DAY,
        acked_at=now - age_days * DAY if acked else None,
    )


def _ids(env: Env, uuid: str) -> list[str]:
    return [r.id for r in env.store.history(uuid)]


def test_purge_waits_an_hour_and_uses_per_repo_days(env: Env, clock: Clock) -> None:
    cfg_a = env.rig.cfg
    cfg_b = cfg_a.model_copy(update={"container_prefix": "other"})
    cfg_a._orchestration_view = OrchestrationView(repo=RepoOrchestration(message_retention_days=2))
    cfg_b._orchestration_view = OrchestrationView(repo=RepoOrchestration(message_retention_days=10))
    env.rig.configs.return_value = {"proj": cfg_a, "other": cfg_b}
    env.set(_orch("proj-orch", "u-a"), _orch("other-orch", "u-b"))
    env.tick()  # first round purges (nothing stored yet) and serves both
    for uuid, full in (("u-a", "proj-orch"), ("u-b", "other-orch")):
        _put(env, uuid, full, "old", 5)
        _put(env, uuid, full, "fresh", 1)
        _put(env, uuid, full, "unread-old", 50, acked=False)
    clock.t += 3599
    env.tick()
    assert _ids(env, "u-a") == ["old", "fresh", "unread-old"]  # no purge yet
    clock.t += 2
    env.tick()
    assert _ids(env, "u-a") == ["fresh", "unread-old"]  # 2-day repo
    assert _ids(env, "u-b") == ["old", "fresh", "unread-old"]  # 10-day repo
    # the next purge is an hour later again
    _put(env, "u-a", "proj-orch", "older", 9)
    env.tick()
    assert "older" in _ids(env, "u-a")


def test_default_days_without_config(env: Env, clock: Clock) -> None:
    env.rig.configs.return_value = {"proj": None}
    env.set(_orch("proj-orch", "u-a"))
    env.store.set_recipient("proj-orch", "u-a", now=0.0)
    _put(env, "u-a", "proj-orch", "mid", 20)
    _put(env, "u-a", "proj-orch", "old", 31)
    env.tick()
    assert _ids(env, "u-a") == ["mid"]


def test_vanished_container_rows_are_purged_after_two_absent_passes(env: Env, clock: Clock) -> None:
    env.set(_orch("proj-orch", "u-a"))
    _put(env, "u-gone", "proj-gone", "acked", 0)
    _put(env, "u-gone", "proj-gone", "unread-old", 31, acked=False)
    _put(env, "u-gone", "proj-gone", "unread-young", 0, acked=False)
    _put(env, "u-a", "proj-orch", "keep", 0, acked=False)
    env.tick()  # first absent pass: nothing is purged yet
    assert _ids(env, "u-gone") == ["acked", "unread-old", "unread-young"]
    clock.t += 3601
    env.tick()  # second absent pass: gone, but young unread items stay
    assert _ids(env, "u-gone") == ["unread-young"]
    assert _ids(env, "u-a") == ["keep"]


def test_project_switch_loses_nothing(env: Env, clock: Clock) -> None:
    """`incus project switch` hides every container from a plain `incus list`."""
    env.set(_orch("proj-orch", "u-a"))
    env.tick()
    _put(env, "u-a", "proj-orch", "read", 0)
    _put(env, "u-a", "proj-orch", "unread", 40, acked=False)
    env.set(_orch("elsewhere", "u-x"))  # the current project's listing now
    env.elsewhere = [{**_orch("proj-orch", "u-a"), "project": "default-old"}]
    for _ in range(4):  # far more than two hourly purges
        clock.t += 3601
        env.tick()
    assert _ids(env, "u-a") == ["read", "unread"]


def test_no_uuid_purge_without_an_all_projects_listing(env: Env, clock: Clock) -> None:
    env.rig.incus.current_project.side_effect = broker.IncusError("boom")
    env.set(_orch("proj-orch", "u-a"))
    _put(env, "u-gone", "proj-gone", "acked", 0)
    for _ in range(3):
        env.tick()
        clock.t += 3601
    assert _ids(env, "u-gone") == ["acked"]


def test_unread_items_survive_a_long_absence(env: Env, clock: Clock) -> None:
    env.set(_orch("elsewhere", "u-x"))
    _put(env, "u-a", "proj-orch", "unread", 0, acked=False)
    for _ in range(3):
        env.tick()
        clock.t += 3601
    assert _ids(env, "u-a") == ["unread"]


def test_a_vanished_recipient_s_unread_items_keep_its_repo_retention(
    env: Env, clock: Clock
) -> None:
    cfg = env.rig.cfg
    cfg._orchestration_view = OrchestrationView(repo=RepoOrchestration(message_retention_days=60))
    env.rig.configs.return_value = {"proj": cfg}
    env.set(_orch("other-orch", "u-a"))
    _put(env, "u-gone", "proj-gone", "unread-40", 40, acked=False)
    _put(env, "u-gone", "proj-gone", "unread-70", 70, acked=False)
    for _ in range(3):
        env.tick()
        clock.t += 3601
    assert _ids(env, "u-gone") == ["unread-40"]


def test_empty_listing_never_purges_vanished(env: Env, clock: Clock) -> None:
    env.set()
    _put(env, "u-gone", "proj-gone", "g", 0, acked=False)
    env.tick()
    assert _ids(env, "u-gone") == ["g"]


def test_failure_is_logged_and_does_not_crash(
    env: Env, clock: Clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(self: object, *a: object) -> int:
        raise RuntimeError("db locked")

    monkeypatch.setattr(type(env.store), "known_uuids", boom)
    env.set(_orch("proj-orch", "u-a"))
    env.tick()
    assert "message purge failed" in caplog.text
    assert env.store.recipients()["proj-orch"] == "u-a"  # the round still served
