"""Eligibility and labeling of adopted orchestration children."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, call

import pytest

from jailbee.config.models_orchestration import OrchestrationView, RepoOrchestration
from jailbee.orchestration import adoption, labels, state, watch
from jailbee.orchestration.grant import Grant


@pytest.fixture
def cfg(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo").model_copy(update={"container_prefix": "myrepo"})
    cfg._orchestration_view = OrchestrationView(repo=RepoOrchestration())
    return cfg


def raw(name, *, status="Running", config=None, own=True):
    return {
        "name": name,
        "status": status,
        "profiles": ["myrepo-base"] if own else [],
        "config": config or {},
    }


@pytest.fixture
def rig(mocker):
    mocker.patch("jailbee.orchestration.commands.repo_registered", return_value=True)
    rows = [
        raw("myrepo-o", config={labels.GRANT: Grant(max_running_children=2).to_label()}),
        raw("myrepo-a"),
    ]
    incus = MagicMock()
    incus.with_exec_timeout.return_value = incus
    incus.list_containers.side_effect = lambda: rows
    incus.config_get.side_effect = lambda name, key: next(
        (r["config"].get(key) for r in rows if r["name"] == name), None
    )
    return rows, incus


def test_adoption_reinstalls_marker_even_when_broker_cached_old_child(cfg, rig):
    _, incus = rig
    adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"])
    assert any(
        c.args[0] == "myrepo-a" and labels.ROLE_MARKER_PATH in str(c)
        for c in incus.exec.call_args_list
    )


def test_adoption_discards_stale_child_outbox_record(cfg, rig):
    rows, incus = rig
    rows[1]["config"]["user.jailbee.orch.stopped_outbox"] = "0"
    adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"])
    incus.config_unset_checked.assert_any_call("myrepo-a", "user.jailbee.orch.stopped_outbox")


def test_adopt_sets_policy_then_parent(cfg, rig):
    _, incus = rig
    result = adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"])
    assert result.adopted == ("myrepo-a",)
    assert incus.config_set.call_args_list[-2:] == [
        call("myrepo-a", labels.ON_DONE, "keep"),
        call("myrepo-a", labels.SPAWNED_BY, "myrepo-o"),
    ]


def test_clears_only_present_stale_labels_before_setting(cfg, rig):
    rows, incus = rig
    rows[1]["config"].update({key: "x" for key in adoption.CHILD_STATE_LABELS})
    adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"])
    assert {c.args[1] for c in incus.config_unset_checked.call_args_list} == set(
        adoption.CHILD_STATE_LABELS
    )
    operations = [c[0] for c in incus.mock_calls if c[0] in {"config_unset_checked", "config_set"}]
    assert operations == ["config_unset_checked"] * 11 + ["config_set"] * 2
    assert not {labels.LAST_IMPORT, labels.RIGHTS, labels.RUNNER} & {
        c.args[1] for c in incus.config_set.call_args_list
    }


@pytest.mark.parametrize(
    "scenario,fragment",
    [
        ("self", "cannot adopt itself"),
        ("other", "is not a container of this repo"),
        ("mount", "mount-mode"),
        ("grant", "holds an orchestration grant"),
        ("children", "has children of its own"),
        ("parent", "is a child of 'p'; release it first"),
        ("no_grant", "holds no orchestration grant"),
        ("orchestrator_child", "children never orchestrate"),
    ],
)
def test_refusals(cfg, rig, scenario, fragment):
    rows, incus = rig
    child = "myrepo-a"
    if scenario == "self":
        child = "myrepo-o"
    if scenario == "other":
        rows[1]["profiles"] = []
    if scenario == "mount":
        rows[1]["config"]["user.jailbee.mode"] = "mount"
    if scenario == "grant":
        rows[1]["config"][labels.GRANT] = "invalid"
    if scenario == "children":
        rows.append(raw("myrepo-c", config={labels.SPAWNED_BY: child}))
    if scenario == "parent":
        rows[1]["config"][labels.SPAWNED_BY] = "myrepo-p"
    if scenario == "no_grant":
        rows[0]["config"].clear()
    if scenario == "orchestrator_child":
        rows[0]["config"][labels.SPAWNED_BY] = "myrepo-p"
    with pytest.raises(adoption.AdoptError, match=fragment):
        adoption.adopt(cfg, incus, "myrepo-o", [child])
    incus.config_set.assert_not_called()
    incus.config_unset_checked.assert_not_called()


def test_multi_refusal_is_atomic(cfg, rig):
    rows, incus = rig
    rows.append(raw("myrepo-m", config={"user.jailbee.mode": "mount"}))
    with pytest.raises(adoption.AdoptError, match="mount-mode"):
        adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a", "myrepo-m"])
    incus.config_set.assert_not_called()
    incus.config_unset_checked.assert_not_called()


def test_already_skipped(cfg, rig):
    rows, incus = rig
    rows[1]["config"][labels.SPAWNED_BY] = "myrepo-o"
    result = adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"])
    assert result.already == ("myrepo-a",) and result.adopted == ()
    incus.config_set.assert_not_called()


def test_cap_counts_running_and_deduplicates(cfg, rig):
    rows, incus = rig
    rows.extend(
        [
            raw("myrepo-existing", config={labels.SPAWNED_BY: "myrepo-o"}),
            raw("myrepo-s", status="Stopped"),
            raw("myrepo-b"),
        ]
    )
    result = adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a", "myrepo-a", "myrepo-s"])
    assert result.adopted == ("myrepo-a", "myrepo-s")
    with pytest.raises(
        adoption.AdoptError, match=r"2 running children.*jb orchestrate grant --children"
    ):
        adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a", "myrepo-b"])


def test_candidates_filter_and_sort(cfg, rig):
    rows, incus = rig
    rows.extend(
        [
            raw("myrepo-z"),
            raw("myrepo-b", config={"user.jailbee.mode": "mount"}),
            raw("myrepo-c", config={labels.GRANT: "x"}),
            raw("myrepo-d", config={labels.SPAWNED_BY: "myrepo-o"}),
            raw("myrepo-e", own=False),
            raw("myrepo-f"),
            raw("myrepo-g", config={labels.SPAWNED_BY: "myrepo-f"}),
        ]
    )
    rows.reverse()
    assert adoption.candidates(cfg, incus, "myrepo-o") == ["myrepo-a", "myrepo-z"]


def test_adopt_notifies_both_without_watching(cfg, rig, tmp_path, mocker):
    _, incus = rig
    deliver = mocker.patch("jailbee.orchestration.outbox.deliver")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"], root=tmp_path, now=now)
    assert not result.warnings
    assert [c.args[2] for c in deliver.call_args_list] == ["myrepo-a", "myrepo-o"]
    child, parent = [c.args[3] for c in deliver.call_args_list]
    assert (child.kind, child.from_) == ("adopted", "o")
    assert (parent.kind, parent.from_) == ("adopted", "a")
    assert child.body == adoption.ADOPTED_CHILD_BODY.format(orchestrator="o")
    assert "background `jailbee inbox wait`" in child.body
    assert parent.body == adoption.ADOPTED_ORCHESTRATOR_BODY.format(child="a")
    assert "close" in parent.body and "send restart --child a --agent-only" in parent.body
    assert [c.kwargs for c in deliver.call_args_list] == [
        {"running": True, "uid": cfg.container_user.uid, "now": now, "seq": 0},
        {"running": True, "uid": cfg.container_user.uid, "now": now, "seq": 1},
    ]
    assert watch.load_watch(tmp_path) == {}


def test_adopt_delivery_failures_are_warnings(cfg, rig, tmp_path, mocker):
    _, incus = rig
    mocker.patch("jailbee.orchestration.outbox.deliver", side_effect=OSError("offline"))
    result = adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"], root=tmp_path)
    assert len(result.warnings) == 2
    assert all("could not notify" in warning for warning in result.warnings)
    incus.config_set.assert_any_call("myrepo-a", labels.SPAWNED_BY, "myrepo-o")


def test_release_live_autostart_preserves_labels(cfg, rig, tmp_path, mocker):
    rows, incus = rig
    rows[1]["config"][labels.SPAWNED_BY] = "myrepo-o"
    mocker.patch("jailbee.autostart_status.live_run", return_value=object())
    with pytest.raises(ValueError, match="autostart is running"):
        adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    incus.config_unset_checked.assert_not_called()


def test_release_cleans_real_state(cfg, rig, tmp_path, mocker):
    rows, incus = rig
    rows[1]["config"].update({key: "x" for key in adoption.CHILD_STATE_LABELS})
    rows[1]["config"].update({labels.SPAWNED_BY: "myrepo-o", labels.RIGHTS: ""})
    pending = state.pending_dir(tmp_path, "myrepo-a")
    pending.mkdir(parents=True)
    (pending / "queued.json").write_text("{}")
    watch.register(
        tmp_path,
        "myrepo-o",
        watch.WatchEntry("ref", "myrepo-a", "spawn", "2026-01-01T00:00:00+00:00"),
    )
    deliver = mocker.patch("jailbee.orchestration.outbox.deliver", side_effect=OSError("offline"))
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    rows[0]["status"] = "Stopped"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = adoption.release(cfg, incus, "myrepo-a", root=tmp_path, now=now)
    assert len(result.warnings) == 1 and "could not notify o" in result.warnings[0]
    assert deliver.call_args.args[2] == "myrepo-o"
    assert deliver.call_args.args[3].kind == "released"
    assert deliver.call_args.args[3].body == adoption.RELEASED_ORCHESTRATOR_BODY.format(child="a")
    assert deliver.call_args.args[3].from_ == "a"
    assert deliver.call_args.kwargs["running"] is False
    assert deliver.call_args.kwargs["now"] == now
    assert deliver.call_args.kwargs["uid"] == cfg.container_user.uid
    assert deliver.call_args.kwargs["seq"] == 0
    assert result.orchestrator == "myrepo-o" and result.revoked == ()
    assert incus.config_unset_checked.call_args_list == [
        call("myrepo-a", labels.SPAWNED_BY),
        *(call("myrepo-a", key) for key in adoption.CHILD_STATE_LABELS),
    ]
    assert not (pending / "queued.json").exists()
    assert watch.load_watch(tmp_path) == {}


def test_release_revokes_only_owned_and_preserves_labels_on_failure(cfg, rig, tmp_path, mocker):
    rows, incus = rig
    from jailbee.orchestration.grant import ChildRights

    rows[1]["config"].update(
        {
            labels.SPAWNED_BY: "myrepo-o",
            labels.RIGHTS: ChildRights(
                mounts=("data",), loose=True, egress=("owned.example",)
            ).to_label(),
            labels.TIMED: '{"mount:timed": "2026-12-01T00:00:00+00:00"}',
        }
    )
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    attached = {"data", "user-data", "timed"}
    allowed_egress = {"owned.example", "user.example"}
    mocker.patch(
        "jailbee.mounts.is_attached", side_effect=lambda incus, full, name: name in attached
    )
    remove = mocker.patch(
        "jailbee.mounts.remove_optional_mount", side_effect=RuntimeError("failed")
    )
    switch = mocker.patch("jailbee.net_switch.switch_mode")
    egress = mocker.patch("jailbee.egress_scope.remove_container_entry")
    with pytest.raises(RuntimeError, match="failed"):
        adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    incus.config_unset_checked.assert_not_called()
    remove.reset_mock()
    egress.reset_mock()
    events = []
    switch.side_effect = lambda *args, **kwargs: events.append("loose")

    def remove_mount(cfg, incus, full, name):
        attached.remove(name)
        events.append(f"mount:{name}")

    def remove_egress(cfg, incus, full, name):
        allowed_egress.discard(name)
        events.append(f"egress:{name}")

    remove.side_effect = remove_mount
    egress.side_effect = remove_egress
    incus.config_unset_checked.side_effect = lambda *args: events.append("unset")
    mocker.patch("jailbee.orchestration.outbox.deliver")
    result = adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    assert set(result.revoked) == {"loose", "mount:data", "egress:owned.example"}
    remove.assert_called_with(cfg, incus, "myrepo-a", "data")
    switch.assert_called_with(cfg, incus, "myrepo-a", "strict", until=None)
    egress.assert_called_with(cfg, incus, "myrepo-a", "owned.example")
    assert events[:4] == ["egress:owned.example", "loose", "mount:data", "unset"]
    assert remove.call_args_list == [call(cfg, incus, "myrepo-a", "data")]
    assert egress.call_args_list == [call(cfg, incus, "myrepo-a", "owned.example")]
    assert attached == {"user-data", "timed"}
    assert allowed_egress == {"user.example"}


def test_release_cleanup_failure_restores_parent_for_retry(cfg, rig, tmp_path, mocker):
    rows, incus = rig
    config = rows[1]["config"]
    config[labels.SPAWNED_BY] = "myrepo-o"
    incus.config_unset_checked.side_effect = lambda name, key: config.pop(key, None)
    incus.config_set.side_effect = lambda name, key, value: config.__setitem__(key, value)
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    cancel = mocker.patch("jailbee.orchestration.watch.cancel", side_effect=OSError("failed"))
    mocker.patch("jailbee.orchestration.outbox.deliver")
    with pytest.raises(OSError, match="failed"):
        adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    assert config[labels.SPAWNED_BY] == "myrepo-o"
    cancel.side_effect = None
    assert adoption.release(cfg, incus, "myrepo-a", root=tmp_path).orchestrator == "myrepo-o"
    assert labels.SPAWNED_BY not in config


@pytest.mark.parametrize("running", [True, False])
def test_release_cleans_role_before_clearing_authority(cfg, rig, tmp_path, mocker, running):
    rows, incus = rig
    rows[1]["config"][labels.SPAWNED_BY] = "myrepo-o"
    rows[1]["status"] = "Running" if running else "Stopped"
    mocker.patch("jailbee.orchestration.rights.revoke_owned", return_value=[])
    mocker.patch("jailbee.orchestration.outbox.deliver")
    if not running:
        with pytest.raises(adoption.AdoptError, match=r"start.*retry"):
            adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
        incus.config_unset_checked.assert_not_called()
        incus.exec.assert_not_called()
        return
    adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    cleanup = next(
        c for c in incus.mock_calls if c[0] == "exec" and labels.ROLE_MARKER_PATH in str(c)
    )
    assert "rm" in str(cleanup)
    assert incus.mock_calls.index(cleanup) < next(
        i for i, c in enumerate(incus.mock_calls) if c[0] == "config_unset_checked"
    )


def test_release_role_cleanup_failure_keeps_parent(cfg, rig, tmp_path, mocker):
    from jailbee.incus import IncusError

    rows, incus = rig
    rows[1]["config"][labels.SPAWNED_BY] = "myrepo-o"
    mocker.patch("jailbee.orchestration.rights.revoke_owned", return_value=[])
    incus.exec.side_effect = IncusError("cannot remove marker")
    with pytest.raises(IncusError):
        adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    incus.config_unset_checked.assert_not_called()


def test_release_failed_parent_unset_restores_role_for_retry(cfg, rig, tmp_path, mocker):
    from jailbee.incus import IncusError

    rows, incus = rig
    rows[1]["config"][labels.SPAWNED_BY] = "myrepo-o"
    mocker.patch("jailbee.orchestration.rights.revoke_owned", return_value=[])
    incus.config_unset_checked.side_effect = IncusError("cannot unset parent")
    with pytest.raises(IncusError):
        adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    assert any(
        "child" in str(c) and labels.ROLE_MARKER_PATH in str(c) for c in incus.exec.call_args_list
    )


def test_release_non_child_refused(cfg, rig, tmp_path):
    _, incus = rig
    with pytest.raises(adoption.AdoptError, match="is not a child"):
        adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    incus.config_unset_checked.assert_not_called()


def test_children_of_repo_filters_and_sorts(cfg, rig):
    rows, incus = rig
    rows[1]["config"][labels.SPAWNED_BY] = "myrepo-o"
    rows.extend(
        [
            raw("myrepo-z", config={labels.SPAWNED_BY: "myrepo-o"}),
            raw("other-x", config={labels.SPAWNED_BY: "myrepo-o"}, own=False),
            raw("myrepo-empty", config={labels.SPAWNED_BY: ""}),
        ]
    )
    assert adoption.children_of_repo(cfg, incus) == ["myrepo-a", "myrepo-z"]


def test_adopt_reservation_counts_toward_capacity(cfg, rig, tmp_path, mocker):
    rows, incus = rig
    rows[0]["config"][labels.GRANT] = Grant(max_running_children=1).to_label()
    state.save_reservations(
        tmp_path, {"myrepo-o": [state.Reservation("myrepo-creating", 123, "t")]}
    )
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    with pytest.raises(adoption.AdoptError, match="limit of 1"):
        adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"], root=tmp_path)
    rows[1]["status"] = "Stopped"
    assert adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"], root=tmp_path).adopted == (
        "myrepo-a",
    )


def test_transferred_child_ignores_old_pending_finish(cfg, rig, tmp_path, mocker):
    import json

    from jailbee.global_config import GlobalConfig
    from jailbee.orchestration import finishing, outbox
    from jailbee.orchestration.actions import ActionContext, Node, destroy_guard
    from jailbee.orchestration.protocol import PendingFinish

    rows, incus = rig
    old, child = rows
    old["status"] = "Stopped"
    child["config"].update({labels.SPAWNED_BY: "myrepo-o", labels.ON_DONE: "destroy"})
    rows.append(raw("myrepo-new", config={labels.GRANT: Grant().to_label()}))
    by_name = {row["name"]: row for row in rows}
    incus.config_set.side_effect = lambda name, key, value: by_name[name]["config"].__setitem__(
        key, value
    )
    incus.config_unset_checked.side_effect = lambda name, key: by_name[name]["config"].pop(
        key, None
    )
    incus.with_exec_timeout.return_value = incus
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    mocker.patch("jailbee.orchestration.actions.live_create_jobs", return_value=set())
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.sync._container_current_branch", return_value="branch")
    mocker.patch("jailbee.orchestration.actions._child_head", return_value="a" * 40)
    transport = mocker.patch("jailbee.sync.merge_container_into_container")
    transport.return_value.push.container_ref = "refs/jailbee/from/a/branch"
    transport.return_value.push.new_oid = "a" * 40
    write = mocker.patch("jailbee.orchestration.outbox._atomic_write")

    def node(row):
        return Node(
            row["name"],
            row["name"].removeprefix("myrepo-"),
            row["status"] == "Running",
            dict(row["config"]),
        )

    ctx = ActionContext(
        cfg=cfg,
        gcfg=GlobalConfig(),
        incus=incus,
        root=tmp_path,
        sender=node(old),
        nodes={r["name"]: node(r) for r in rows},
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    item = PendingFinish(
        version=1,
        type="finish",
        id="done1",
        child_full="myrepo-a",
        child_short="a",
        branch="branch",
        done_sha="a" * 40,
        body="done",
        in_reply_to=None,
        uid=None,
    )
    path = finishing.park(ctx, ctx.sender, item)
    adoption.release(cfg, incus, "myrepo-a", root=tmp_path)
    adoption.adopt(cfg, incus, "myrepo-new", ["myrepo-a"], root=tmp_path)
    assert path.exists()
    ctx.nodes = {r["name"]: node(r) for r in rows}
    ctx.sender = node(old)
    outbox.flush_pending(
        incus, tmp_path, "myrepo-o", uid=None, finish=finishing.flush_callback(ctx)
    )
    messages = [json.loads(c.args[4]) for c in write.call_args_list]
    report = next(m for m in messages if m.get("kind") == "done")
    assert report["import_error"] and report["on_done"] == "keep"
    assert not path.exists()
    transport.assert_not_called()
    assert labels.LAST_IMPORT not in child["config"]
    ctx.sender = node(by_name["myrepo-new"])
    assert destroy_guard(ctx, node(child)) is not None
