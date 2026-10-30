from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels
from jailbee.orchestration.actions import ActionContext, Node, Outcome
from jailbee.orchestration.grant import Grant
from jailbee.orchestration.protocol import DestroyManifest, RestartManifest, StopManifest
from jailbee.orchestration.state import load_queue
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        running: bool = False,
        limit: int = 1,
        config: dict[str, str] | None = None,
    ) -> None:
        self.cfg = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
        self.sender = Node(
            "proj-orch",
            "orch",
            True,
            {labels.GRANT: Grant.model_validate({"max_running_children": limit}).to_label()},
        )
        self.child = Node(
            "proj-c1", "c1", running, {labels.SPAWNED_BY: "proj-orch", **(config or {})}
        )
        self.stranger = Node("proj-x", "x", True, {})
        self.incus = MagicMock()
        self.incus.config_get.return_value = ""
        self.root = tmp_path

    def ctx(self) -> ActionContext:
        nodes = {n.full: n for n in [self.sender, self.child, self.stranger]}
        return ActionContext(
            self.cfg, GlobalConfig(), self.incus, self.root, self.sender, nodes,
            datetime(2026, 10, 25, tzinfo=UTC),
        )  # fmt: skip


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    m = {
        "jobs": MagicMock(return_value=set()),
        "stop": MagicMock(return_value=False),
        "boot": MagicMock(
            return_value=__import__("types").SimpleNamespace(pid=12345, log_path="/tmp/boot.log")
        ),
        "destroy": MagicMock(),
        "head": MagicMock(return_value="aaa"),
    }
    monkeypatch.setattr(actions, "live_create_jobs", m["jobs"])
    monkeypatch.setattr(actions.stopping, "stop_container", m["stop"])
    monkeypatch.setattr("jailbee.create_flow.launch_boot_background", m["boot"])
    monkeypatch.setattr(actions.lifecycle, "destroy_container", m["destroy"])
    monkeypatch.setattr(actions.sync, "_container_head_oid", m["head"])
    monkeypatch.setattr(actions.sync, "_container_status_dirty", MagicMock(return_value=False))
    monkeypatch.setattr(actions.lifecycle, "container_repo_dir", MagicMock(return_value="/r"))
    return m


def test_destroy_refused_when_running_child_tree_dirty(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, running=True, config={labels.LAST_IMPORT: "aaa"})
    monkeypatch.setattr(actions.sync, "_container_status_dirty", MagicMock(return_value=True))
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out.state == "refused" and "uncommitted changes" in out.detail
    seams["destroy"].assert_not_called()


def test_destroy_refused_when_tree_probe_fails(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, running=True, config={labels.LAST_IMPORT: "aaa"})
    monkeypatch.setattr(
        actions.sync, "_container_status_dirty", MagicMock(side_effect=RuntimeError("probe"))
    )
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out.state == "refused" and "could not be checked" in out.detail
    seams["destroy"].assert_not_called()


def test_destroy_force_skips_dirty_guard(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, running=True)
    probe = MagicMock(side_effect=RuntimeError("must not probe"))
    monkeypatch.setattr(actions.sync, "_container_status_dirty", probe)
    assert actions.handle_destroy(rig.ctx(), _destroy(force=True)).state == "done"
    probe.assert_not_called()


def test_destroy_refused_for_stopped_child_recorded_dirty(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(
        tmp_path,
        config={labels.LAST_IMPORT: "aaa", labels.STOPPED_HEAD: "aaa", labels.STOPPED_DIRTY: "1"},
    )
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out.state == "refused" and "uncommitted changes" in out.detail
    seams["destroy"].assert_not_called()


@pytest.mark.parametrize("dirty", [True, False, None])
def test_stop_records_stopped_dirty(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch, dirty: bool | None
) -> None:
    rig = Rig(tmp_path, running=True)
    monkeypatch.setattr(actions.sync, "_container_dirty_quiet", MagicMock(return_value=dirty))
    m = StopManifest(version=1, id="s1", type="stop", child="c1")
    assert actions.handle_stop(rig.ctx(), m).state == "done"
    if dirty is False:
        rig.incus.config_unset_checked.assert_any_call(rig.child.full, labels.STOPPED_DIRTY)
    else:
        rig.incus.config_set.assert_any_call(rig.child.full, labels.STOPPED_DIRTY, "1")


def _restart(child: str = "c1") -> RestartManifest:
    return RestartManifest.model_validate(
        {"version": 1, "id": "r1", "type": "restart", "child": child, "prompt": "go on"}
    )


def _destroy(force: bool = False, child: str = "c1") -> DestroyManifest:
    return DestroyManifest.model_validate(
        {"version": 1, "id": "d1", "type": "destroy", "child": child, "force": force}
    )


@pytest.mark.parametrize("policy", [None, "keep", "destroy"])
def test_restart_sets_on_done_label_only_when_given(
    tmp_path: Path, seams: dict[str, MagicMock], policy: str | None
) -> None:
    rig = Rig(tmp_path)
    m = _restart().model_copy(update={"on_done": policy})
    assert actions.handle_restart(rig.ctx(), m).state == "started"
    calls = [c.args for c in rig.incus.config_set.call_args_list if c.args[1] == labels.ON_DONE]
    assert calls == ([] if policy is None else [(rig.child.full, labels.ON_DONE, policy)])


def test_restart_clears_pending_destroy_and_stopped_dirty(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path)
    actions.handle_restart(rig.ctx(), _restart())
    unset = [c.args[1] for c in rig.incus.config_unset_checked.call_args_list]
    assert labels.PENDING_DESTROY in unset and labels.STOPPED_DIRTY in unset


def test_restart_explicit_destroy_needs_grant(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path)
    rig.sender = Node(
        "proj-orch",
        "orch",
        True,
        {labels.GRANT: Grant(operations=("spawn", "message", "restart")).to_label()},
    )
    out = actions.handle_restart(rig.ctx(), _restart().model_copy(update={"on_done": "destroy"}))
    assert out.state == "refused" and "destroy` operation" in out.detail
    seams["boot"].assert_not_called()


@pytest.mark.parametrize("operation", ["stop", "destroy"])
@pytest.mark.parametrize("fails", [False, True])
def test_intentional_shutdown_cancels_watch_only_on_success(
    tmp_path, seams, operation, fails
) -> None:
    from jailbee.orchestration.watch import WatchEntry, load_watch, save_watch

    rig = Rig(tmp_path, running=True)
    entry = WatchEntry(
        "s", rig.child.full, "spawn", rig.ctx().now.isoformat(), supervise=True, confirmed=True
    )
    save_watch(tmp_path, {rig.sender.full: [entry]})
    if fails:
        seams[operation].side_effect = RuntimeError("cannot stop")
    manifest = (
        StopManifest(version=1, id="s1", type="stop", child="c1")
        if operation == "stop"
        else _destroy(force=True)
    )
    outcome = getattr(actions, f"handle_{operation}")(rig.ctx(), manifest)
    assert outcome.state == ("refused" if fails else "done")
    assert load_watch(tmp_path).get(rig.sender.full, []) == ([entry] if fails else [])


def test_stop_running_child(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, running=True)
    m = StopManifest.model_validate({"version": 1, "id": "s1", "type": "stop", "child": "c1"})
    out = actions.handle_stop(rig.ctx(), m)
    assert out == Outcome("done", "child `c1` stopped")
    seams["stop"].assert_called_once_with(rig.incus, "proj-c1", show_progress=False)


def test_stop_stopped_child_and_stranger(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path)
    m = StopManifest.model_validate({"version": 1, "id": "s1", "type": "stop", "child": "c1"})
    assert actions.handle_stop(rig.ctx(), m) == Outcome("done", "child `c1` stopped")
    seams["stop"].assert_not_called()
    other = StopManifest.model_validate({"version": 1, "id": "s2", "type": "stop", "child": "x"})
    assert actions.handle_stop(rig.ctx(), other).state == "refused"
    seams["stop"].assert_not_called()


def test_restart_refuses_running_child(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, running=True, limit=3)
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out == Outcome("refused", "child `c1` is running; stop it first")
    seams["boot"].assert_not_called()


def test_restart_does_not_launch_without_durable_watch(tmp_path, seams, monkeypatch) -> None:
    monkeypatch.setattr(
        "jailbee.orchestration.watch.save_watch", MagicMock(side_effect=OSError("disk unavailable"))
    )
    rig = Rig(tmp_path, limit=2)
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out.state == "refused" and "disk unavailable" in out.detail
    seams["boot"].assert_not_called()


def test_restart_refused_launch_drops_the_watch(tmp_path, seams, monkeypatch) -> None:
    from jailbee.orchestration.watch import load_watch

    monkeypatch.setattr(
        "jailbee.create_flow.launch_boot_background", MagicMock(side_effect=OSError("no fork"))
    )
    rig = Rig(tmp_path, limit=2)
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out.state == "refused" and "no fork" in out.detail
    assert load_watch(tmp_path).get(rig.sender.full, []) == []


def test_restart_boots_with_preambled_prompt(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch
) -> None:
    from types import SimpleNamespace

    launch = MagicMock(return_value=SimpleNamespace(pid=12345, log_path="/tmp/boot.log"))
    monkeypatch.setattr("jailbee.create_flow.launch_boot_background", launch)
    rig = Rig(tmp_path, limit=2)
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out.state == "started" and "restarting" in out.detail
    launch.assert_called_once_with(
        rig.cfg,
        container_name="proj-c1",
        initial_prompt=actions.child_preamble("orch") + "\n\ngo on",
        config_path=None,
    )
    seams["boot"].assert_not_called()


def test_restart_queues_when_full(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1)
    seams["jobs"].return_value = {"proj-other"}  # a live create fills the only slot
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out == Outcome("queued", "queued at position 1")
    seams["boot"].assert_not_called()
    q = load_queue(tmp_path)["proj-orch"]
    assert [(r.kind, r.child_full) for r in q] == [("restart", "proj-c1")]
    assert q[0].prompt.endswith("\n\ngo on")


def test_drain_starts_queued_restart(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1)
    seams["jobs"].return_value = {"proj-other"}
    actions.handle_restart(rig.ctx(), _restart())
    seams["jobs"].return_value = set()
    results = actions.drain_queue(rig.ctx())
    assert [o.state for _, o in results] == ["started"]
    seams["boot"].assert_called_once()
    assert load_queue(tmp_path).get("proj-orch", []) == []


def test_restart_queues_behind_nonempty_queue(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2)
    seams["jobs"].return_value = {"proj-o1", "proj-o2"}
    actions.handle_restart(rig.ctx(), _restart())  # queued: slots full
    seams["jobs"].return_value = set()  # a slot frees, but the queue is not empty
    again = RestartManifest.model_validate(
        {"version": 1, "id": "r2", "type": "restart", "child": "c2", "prompt": "p"}
    )
    ctx = rig.ctx()
    c2 = Node("proj-c2", "c2", False, {labels.SPAWNED_BY: "proj-orch"})
    ctx.nodes = {**ctx.nodes, c2.full: c2}
    out = actions.handle_restart(ctx, again)
    assert out == Outcome("queued", "queued at position 2")
    seams["boot"].assert_not_called()


def test_destroy_refused_without_import(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, running=True)
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out == Outcome(
        "refused",
        "child `c1` has commits that were never imported (use force: true to destroy anyway)",
    )
    seams["destroy"].assert_not_called()


def test_destroy_refused_when_head_moved_after_import(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, running=True, config={labels.LAST_IMPORT: "old"})
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out.state == "refused"
    assert "never imported" in out.detail
    seams["destroy"].assert_not_called()


def test_destroy_allowed_when_head_equals_last_import(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, running=True, config={labels.LAST_IMPORT: "aaa"})
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out == Outcome("done", "child `c1` destroyed")
    seams["destroy"].assert_called_once_with(rig.cfg, rig.incus, "proj-c1", force=True)


def test_destroy_force_skips_guard(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, running=True)
    out = actions.handle_destroy(rig.ctx(), _destroy(force=True))
    assert out.state == "done"
    seams["head"].assert_not_called()
    seams["destroy"].assert_called_once()


def test_destroy_stopped_child_without_stopped_head_asks_for_a_restart(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    """Spec 7.3 as amended: a stopped child is judged by STOPPED_HEAD only.
    DONE_SHA is no fallback — the child may have committed after its `done`."""
    for config in (
        {labels.LAST_IMPORT: "bbb", labels.DONE_SHA: "bbb"},
        {labels.LAST_IMPORT: "bbb", labels.DONE_SHA: "ccc"},
        {labels.LAST_IMPORT: "bbb"},
    ):
        rig = Rig(tmp_path, config=config)
        out = actions.handle_destroy(rig.ctx(), _destroy())
        assert out.state == "refused"
        assert "restart" in out.detail
        assert "force: true" in out.detail
    seams["head"].assert_not_called()
    seams["destroy"].assert_not_called()


def test_destroy_force_still_skips_the_guard_for_a_stopped_child(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, config={labels.LAST_IMPORT: "bbb", labels.DONE_SHA: "bbb"})
    assert actions.handle_destroy(rig.ctx(), _destroy(force=True)).state == "done"
    seams["destroy"].assert_called_once()


def test_commits_after_import_survive_a_stop_outside_the_broker(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    """done at X, import at X, child commits Y, child stops outside the broker
    (reboot, `jb stop`, OOM: no STOPPED_HEAD), destroy: refused."""
    x = "a" * 40
    rig = Rig(tmp_path, config={labels.DONE_SHA: x, labels.LAST_IMPORT: x})
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out.state == "refused"
    seams["destroy"].assert_not_called()


def test_destroy_stranger_refused(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path)
    out = actions.handle_destroy(rig.ctx(), _destroy(force=True, child="x"))
    assert out.state == "refused"
    seams["destroy"].assert_not_called()


class Labels:
    """A label store behind `incus.config_set` / `config_unset_checked`."""

    def __init__(self, rig: Rig) -> None:
        self.store: dict[str, str] = dict(rig.child.config)
        rig.incus.config_set.side_effect = lambda _n, k, v: self.store.__setitem__(k, v)
        rig.incus.config_unset_checked.side_effect = lambda _n, k: self.store.pop(k, None)

    def node(self, rig: Rig, running: bool) -> Node:
        return Node(rig.child.full, rig.child.short, running, dict(self.store))


def _stop() -> StopManifest:
    return StopManifest.model_validate({"version": 1, "id": "s1", "type": "stop", "child": "c1"})


def test_stopped_child_commits_after_restart_are_not_lost(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    """done at X, import at X, stop, restart, child commits Y, stop, destroy: refused."""
    x, y = "a" * 40, "b" * 40
    rig = Rig(tmp_path, running=True, limit=2, config={labels.DONE_SHA: x, labels.LAST_IMPORT: x})
    store = Labels(rig)
    seams["head"].return_value = x
    assert actions.handle_stop(rig.ctx(), _stop()).state == "done"
    assert store.store[labels.STOPPED_HEAD] == x
    rig.child = store.node(rig, running=False)
    assert actions.handle_restart(rig.ctx(), _restart()).state == "started"
    assert labels.STOPPED_HEAD not in store.store
    assert labels.DONE_SHA not in store.store
    rig.child = store.node(rig, running=True)
    seams["head"].return_value = y  # the child committed Y
    assert actions.handle_stop(rig.ctx(), _stop()).state == "done"
    rig.child = store.node(rig, running=False)
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out.state == "refused"
    assert "never imported" in out.detail
    seams["destroy"].assert_not_called()


def test_stopped_child_prefers_stopped_head_over_done_sha(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    x, y = "a" * 40, "b" * 40
    stale = Rig(
        tmp_path, config={labels.LAST_IMPORT: x, labels.DONE_SHA: x, labels.STOPPED_HEAD: y}
    )
    assert actions.handle_destroy(stale.ctx(), _destroy()).state == "refused"
    clean = Rig(
        tmp_path, config={labels.LAST_IMPORT: y, labels.DONE_SHA: x, labels.STOPPED_HEAD: y}
    )
    assert actions.handle_destroy(clean.ctx(), _destroy()).state == "done"
    seams["head"].assert_not_called()


def test_stop_with_unreadable_head_clears_stopped_head(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, running=True, config={labels.STOPPED_HEAD: "a" * 40})
    store = Labels(rig)
    seams["head"].side_effect = RuntimeError("no repo")
    assert actions.handle_stop(rig.ctx(), _stop()).state == "done"
    assert labels.STOPPED_HEAD not in store.store
    seams["stop"].assert_called_once()


def test_stop_refused_when_head_cannot_be_recorded(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    from jailbee.incus import IncusError

    rig = Rig(tmp_path, running=True)
    rig.incus.config_set.side_effect = IncusError("daemon gone")
    out = actions.handle_stop(rig.ctx(), _stop())
    assert out.state == "refused"
    seams["stop"].assert_not_called()


def test_restart_clears_labels_before_booting(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    from jailbee.incus import IncusError

    rig = Rig(tmp_path, limit=2, config={labels.STOPPED_HEAD: "a" * 40})
    rig.incus.config_unset_checked.side_effect = IncusError("daemon gone")
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out.state == "refused"
    seams["boot"].assert_not_called()


def test_destroy_purges_the_childs_pending_messages(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    from jailbee.orchestration.state import pending_dir

    rig = Rig(tmp_path, running=True)
    parked = pending_dir(rig.root, "proj-c1")
    parked.mkdir(parents=True)
    (parked / "20261025T000000Z-h-1.json").write_text("{}")
    assert actions.handle_destroy(rig.ctx(), _destroy(force=True)).state == "done"
    assert not parked.exists()


def test_failed_destroy_keeps_pending(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    from jailbee.orchestration.state import pending_dir

    rig = Rig(tmp_path, running=True)
    parked = pending_dir(rig.root, "proj-c1")
    parked.mkdir(parents=True)
    seams["destroy"].side_effect = RuntimeError("busy")
    assert actions.handle_destroy(rig.ctx(), _destroy(force=True)).state == "refused"
    assert parked.exists()


def test_restart_clears_state_stopped_head_and_done_sha(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    """A restarted child is `working` again (absent STATE = working)."""
    x = "a" * 40
    rig = Rig(
        tmp_path,
        limit=2,
        config={
            labels.STATE: labels.STATE_IMPORTED,
            labels.DONE_SHA: x,
            labels.STOPPED_HEAD: x,
            labels.LAST_IMPORT: x,
        },
    )
    store = Labels(rig)
    assert actions.handle_restart(rig.ctx(), _restart()).state == "started"
    for key in (labels.STATE, labels.DONE_SHA, labels.STOPPED_HEAD):
        assert key not in store.store
    assert store.store[labels.LAST_IMPORT] == x
    seams["boot"].assert_called_once()


def _restart_with(**extra: object) -> RestartManifest:
    return RestartManifest.model_validate(
        {"version": 1, "id": "r1", "type": "restart", "child": "c1", "prompt": "go on", **extra}
    )


def _runner_calls(rig: Rig) -> list[object]:
    return [
        c
        for c in rig.incus.mock_calls
        if c[0] in ("config_set", "config_unset_checked") and c.args[1] == labels.RUNNER
    ]


def test_restart_without_runner_keeps_label(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actions, "runner_problems", lambda *a, **k: [])
    rig = Rig(tmp_path, limit=2, config={labels.RUNNER: '{"agent":"claude"}'})
    assert actions.handle_restart(rig.ctx(), _restart()).state == "started"
    assert _runner_calls(rig) == []
    seams["boot"].assert_called_once()


def test_restart_with_runner_replaces_label(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actions, "runner_problems", lambda *a, **k: [])
    rig = Rig(tmp_path, limit=2)
    order: list[str] = []
    rig.incus.config_set.side_effect = lambda *a, **k: order.append("set")
    rig.incus.config_unset_checked.side_effect = lambda *a, **k: order.append("unset")
    seams["boot"].side_effect = lambda *a, **k: (order.append("boot"), seams["boot"].return_value)[
        1
    ]
    m = _restart_with(runner={"agent": "claude-jb", "profile": "grok"})
    assert actions.handle_restart(rig.ctx(), m).state == "started"
    rig.incus.config_set.assert_called_once_with(
        "proj-c1", labels.RUNNER, '{"agent":"claude-jb","profile":"grok"}'
    )
    assert order == ["unset", "unset", "unset", "unset", "unset", "set", "boot"]


def test_restart_with_null_runner_clears_label(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2, config={labels.RUNNER: '{"agent":"claude"}'})
    order: list[str] = []
    seams["boot"].side_effect = lambda *a, **k: (order.append("boot"), seams["boot"].return_value)[
        1
    ]
    rig.incus.config_unset_checked.side_effect = lambda c, k: order.append(k)
    assert actions.handle_restart(rig.ctx(), _restart_with(runner=None)).state == "started"
    assert order[-2:] == [labels.RUNNER, "boot"]
    rig.incus.config_set.assert_not_called()


def test_restart_refused_for_bad_runner_leaves_label(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, limit=2, config={labels.RUNNER: '{"agent":"claude"}'})
    out = actions.handle_restart(rig.ctx(), _restart_with(runner={"agent": "claude"}))
    assert out.state == "refused"
    assert "agent `claude` is not enabled in this repo" in out.detail
    rig.incus.config_set.assert_not_called()
    rig.incus.config_unset_checked.assert_not_called()
    seams["boot"].assert_not_called()
    # Refused before the slot check: nothing was queued either.
    seams["jobs"].return_value = {"proj-other"}
    out = actions.handle_restart(rig.ctx(), _restart_with(runner={"agent": "claude"}))
    assert out.state == "refused"
    assert load_queue(tmp_path).get("proj-orch", []) == []


def test_restart_runner_label_failure_is_a_refusal(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.incus import IncusError

    monkeypatch.setattr(actions, "runner_problems", lambda *a, **k: [])
    rig = Rig(tmp_path, limit=2)
    rig.incus.config_set.side_effect = IncusError("daemon gone")
    out = actions.handle_restart(rig.ctx(), _restart_with(runner={"agent": "claude"}))
    assert out.state == "refused"
    seams["boot"].assert_not_called()


_KEPT = '{"agent":"claude-jb","profile":"grok"}'


def test_restart_rechecks_kept_runner_at_handle(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def problems(cfg: object, incus: object, gcfg: object, runner: object) -> list[str]:
        seen.append(runner)
        return ["profile `grok` is not served"]

    monkeypatch.setattr(actions, "runner_problems", problems)
    rig = Rig(tmp_path, limit=1, config={labels.RUNNER: _KEPT})
    seams["jobs"].return_value = {"proj-other"}  # would queue if it passed
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out.state == "refused"
    assert "grok" in out.detail
    assert [getattr(r, "profile", None) for r in seen] == ["grok"]
    assert load_queue(tmp_path).get("proj-orch", []) == []
    rig.incus.config_set.assert_not_called()
    rig.incus.config_unset_checked.assert_not_called()
    seams["boot"].assert_not_called()


def test_restart_rechecks_kept_runner_at_dequeue(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actions, "runner_problems", lambda *a, **k: [])
    rig = Rig(tmp_path, limit=1, config={labels.RUNNER: _KEPT})
    seams["jobs"].return_value = {"proj-other"}
    assert actions.handle_restart(rig.ctx(), _restart()).state == "queued"
    monkeypatch.setattr(actions, "runner_problems", lambda *a, **k: ["grok gone"])
    seams["jobs"].return_value = set()
    results = actions.drain_queue(rig.ctx())
    assert [(o.state, "grok gone" in o.detail) for _, o in results] == [("refused", True)]
    rig.incus.config_set.assert_not_called()
    rig.incus.config_unset_checked.assert_not_called()
    seams["boot"].assert_not_called()


def test_restart_without_label_or_runner_is_not_checked(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    check = MagicMock(return_value=["boom"])
    monkeypatch.setattr(actions, "runner_problems", check)
    rig = Rig(tmp_path, limit=2)
    assert actions.handle_restart(rig.ctx(), _restart()).state == "started"
    check.assert_not_called()


def test_restart_with_null_runner_does_not_check_the_old_label(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    check = MagicMock(return_value=["boom"])
    monkeypatch.setattr(actions, "runner_problems", check)
    rig = Rig(tmp_path, limit=2, config={labels.RUNNER: _KEPT})
    assert actions.handle_restart(rig.ctx(), _restart_with(runner=None)).state == "started"
    check.assert_not_called()


def test_restart_null_uses_current_default(tmp_path, seams, monkeypatch):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    runner = Runner(agent="claude", model="haiku")
    rig = Rig(tmp_path, config={labels.RUNNER: Runner(agent="claude", model="opus").to_label()})
    rig.cfg._orchestration_view = OrchestrationView(
        default_runner=runner, default_runner_source="repo"
    )
    monkeypatch.setattr(actions, "runner_problems", lambda *args: [])
    assert actions.handle_restart(rig.ctx(), _restart_with(runner=None)).state == "started"
    rig.incus.config_set.assert_any_call(rig.child.full, labels.RUNNER, runner.to_label())


def test_restart_omitted_runner_keeps_label_despite_different_default(tmp_path, seams, monkeypatch):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    old = Runner(agent="claude", model="opus")
    default = Runner(agent="claude", model="haiku")
    rig = Rig(tmp_path, config={labels.RUNNER: old.to_label()})
    rig.cfg._orchestration_view = OrchestrationView(
        default_runner=default, default_runner_source="repo"
    )
    check = MagicMock(return_value=[])
    monkeypatch.setattr(actions, "runner_problems", check)
    assert actions.handle_restart(rig.ctx(), _restart()).state == "started"
    check.assert_called_once_with(rig.cfg, rig.incus, rig.ctx().gcfg, old)
    assert _runner_calls(rig) == []
    seams["boot"].assert_called_once()


def test_restart_null_refuses_invalid_default(tmp_path, seams, monkeypatch):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    runner = Runner(agent="claude-jb", profile="grok")
    rig = Rig(tmp_path, config={labels.RUNNER: Runner(agent="claude", model="opus").to_label()})
    rig.cfg._orchestration_view = OrchestrationView(
        default_runner=runner, default_runner_source="repo"
    )
    detail = "LiteLLM profile `grok` is not served"
    check = MagicMock(return_value=[detail])
    monkeypatch.setattr(actions, "runner_problems", check)
    out = actions.handle_restart(rig.ctx(), _restart_with(runner=None))
    assert out == Outcome("refused", detail)
    assert check.call_args.args[3] == runner
    assert _runner_calls(rig) == []
    seams["boot"].assert_not_called()
    assert load_queue(tmp_path).get("proj-orch", []) == []
