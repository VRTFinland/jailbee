"""Eligibility and labeling of adopted orchestration children."""

from unittest.mock import MagicMock, call

import pytest

from jailbee.config.models_orchestration import OrchestrationView, RepoOrchestration
from jailbee.orchestration import adoption, labels
from jailbee.orchestration.grant import Grant


@pytest.fixture
def cfg(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo").model_copy(update={"container_prefix": "myrepo"})
    cfg._orchestration_view = OrchestrationView(repo=RepoOrchestration())
    return cfg


def raw(name, *, status="Running", config=None, own=True):
    return {"name": name, "status": status, "profiles": ["myrepo-base"] if own else [], "config": config or {}}


@pytest.fixture
def rig(mocker):
    mocker.patch("jailbee.orchestration.commands.repo_registered", return_value=True)
    rows = [raw("myrepo-o", config={labels.GRANT: Grant(max_running_children=2).to_label()}), raw("myrepo-a")]
    incus = MagicMock()
    incus.list_containers.side_effect = lambda: rows
    incus.config_get.side_effect = lambda name, key: next((r["config"].get(key) for r in rows if r["name"] == name), None)
    return rows, incus


def test_adopt_sets_policy_then_parent(cfg, rig):
    _, incus = rig
    result = adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"])
    assert result.adopted == ("myrepo-a",)
    assert incus.config_set.call_args_list[-2:] == [call("myrepo-a", labels.ON_DONE, "keep"), call("myrepo-a", labels.SPAWNED_BY, "myrepo-o")]


def test_clears_only_present_stale_labels_before_setting(cfg, rig):
    rows, incus = rig
    rows[1]["config"].update({labels.STATE: "done", labels.DONE_SHA: "x", labels.LAST_IMPORT: "x", labels.TIMED: "x"})
    adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a"])
    assert {c.args[1] for c in incus.config_unset_checked.call_args_list} == {labels.STATE, labels.DONE_SHA, labels.LAST_IMPORT, labels.TIMED}
    operations = [c[0] for c in incus.mock_calls if c[0] in {"config_unset_checked", "config_set"}]
    assert operations == ["config_unset_checked"] * 4 + ["config_set"] * 2
    assert not {labels.LAST_IMPORT, labels.RIGHTS, labels.RUNNER} & {c.args[1] for c in incus.config_set.call_args_list}


@pytest.mark.parametrize("scenario,fragment", [("self", "cannot adopt itself"), ("other", "is not a container of this repo"), ("mount", "mount-mode"), ("grant", "holds an orchestration grant"), ("children", "has children of its own"), ("parent", "is a child of 'p'; release it first"), ("no_grant", "holds no orchestration grant"), ("orchestrator_child", "children never orchestrate")])
def test_refusals(cfg, rig, scenario, fragment):
    rows, incus = rig
    child = "myrepo-a"
    if scenario == "self": child = "myrepo-o"
    if scenario == "other": rows[1]["profiles"] = []
    if scenario == "mount": rows[1]["config"]["user.jailbee.mode"] = "mount"
    if scenario == "grant": rows[1]["config"][labels.GRANT] = "invalid"
    if scenario == "children": rows.append(raw("myrepo-c", config={labels.SPAWNED_BY: child}))
    if scenario == "parent": rows[1]["config"][labels.SPAWNED_BY] = "myrepo-p"
    if scenario == "no_grant": rows[0]["config"].clear()
    if scenario == "orchestrator_child": rows[0]["config"][labels.SPAWNED_BY] = "myrepo-p"
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
    rows.extend([raw("myrepo-existing", config={labels.SPAWNED_BY: "myrepo-o"}), raw("myrepo-s", status="Stopped"), raw("myrepo-b")])
    result = adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a", "myrepo-a", "myrepo-s"])
    assert result.adopted == ("myrepo-a", "myrepo-s")
    with pytest.raises(adoption.AdoptError, match="2 running children.*jb orchestrate grant --children"):
        adoption.adopt(cfg, incus, "myrepo-o", ["myrepo-a", "myrepo-b"])


def test_candidates_filter_and_sort(cfg, rig):
    rows, incus = rig
    rows.extend([raw("myrepo-z"), raw("myrepo-b", config={"user.jailbee.mode": "mount"}), raw("myrepo-c", config={labels.GRANT: "x"}), raw("myrepo-d", config={labels.SPAWNED_BY: "myrepo-o"}), raw("myrepo-e", own=False), raw("myrepo-f"), raw("myrepo-g", config={labels.SPAWNED_BY: "myrepo-f"})])
    assert adoption.candidates(cfg, incus, "myrepo-o") == ["myrepo-a", "myrepo-z"]
