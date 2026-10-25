"""Grant, revoke, show and list logic (no CLI, mocked Incus)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jailbee.config import Config
from jailbee.config.models_host import OptionalMount
from jailbee.config.models_orchestration import (
    GrantableConfig,
    OrchestrationProfile,
    OrchestrationView,
    RepoOrchestration,
)
from jailbee.orchestration import commands, labels
from jailbee.orchestration.commands import GrantFlags
from jailbee.orchestration.grant import Grant, Grantable, GrantError

PROFILES = {
    "bugfixer": OrchestrationProfile(
        max_running_children=5, grantable=GrantableConfig(mounts=["data"])
    )
}


def _cfg(make_cfg, tmp_path, *, view: OrchestrationView | None = None, mounts=False) -> Config:
    cfg = make_cfg(tmp_path / "repo").model_copy(update={"container_prefix": "myrepo"})
    if mounts:
        cfg = cfg.model_copy(
            update={"optional_mounts": {"data": OptionalMount(host=tmp_path, container="/data")}}
        )
    cfg._orchestration_view = view or OrchestrationView(
        profiles=PROFILES, repo=RepoOrchestration(mounts=["data"])
    )
    return cfg


@pytest.fixture
def cfg(make_cfg, tmp_path):
    return _cfg(make_cfg, tmp_path, mounts=True)


@pytest.fixture
def cfg_with_mount_data(cfg):
    return cfg


@pytest.fixture
def cfg_with_profile_bugfixer(cfg):
    return cfg


@pytest.fixture
def cfg_with_profile_bugfixer_default(make_cfg, tmp_path):
    view = OrchestrationView(
        profiles=PROFILES, default_profile="bugfixer", repo=RepoOrchestration(mounts=["data"])
    )
    return _cfg(make_cfg, tmp_path, view=view, mounts=True)


def _cfg_cap(make_cfg, tmp_path, **repo):
    return _cfg(
        make_cfg,
        tmp_path,
        view=OrchestrationView(profiles=PROFILES, repo=RepoOrchestration(**repo)),
        mounts=True,
    )


@pytest.fixture
def cfg_with_cap_limit_1(make_cfg, tmp_path):
    return _cfg_cap(make_cfg, tmp_path, max_running_children=1)


@pytest.fixture
def make_incus():
    def _make(config: dict[str, str]) -> MagicMock:
        m = MagicMock()
        m.config_get.side_effect = lambda _name, key: config.get(key)
        return m

    return _make


@pytest.fixture
def incus(make_incus):
    return make_incus({})


@pytest.fixture(autouse=True)
def _registered(mocker):
    return mocker.patch("jailbee.orchestration.commands.repo_registered", return_value=True)


@pytest.fixture
def ensure(mocker):
    return mocker.patch("jailbee.orchestration.service.ensure_running")


def test_grant_writes_label_and_starts_broker(cfg, incus, ensure):
    g = commands.grant(cfg, incus, "myrepo-a", GrantFlags(children=3, allow=("destroy",)))
    incus.config_set.assert_called_once_with("myrepo-a", labels.GRANT, g.to_label())
    assert g.max_running_children == 3 and "destroy" in g.operations
    ensure.assert_called_once()


@pytest.mark.parametrize(
    ("labels_", "registered", "match"),
    [
        ({"user.jailbee.mode": "mount"}, True, "mount-mode"),
        ({labels.SPAWNED_BY: "myrepo-orch"}, True, "child of 'orch'"),
        ({}, False, "jb apply"),
    ],
)
def test_refusals_write_nothing(cfg, make_incus, mocker, labels_, registered, match):
    mocker.patch("jailbee.orchestration.commands.repo_registered", return_value=registered)
    incus = make_incus(labels_)
    with pytest.raises(GrantError, match=match):
        commands.grant(cfg, incus, "myrepo-a", GrantFlags())
    incus.config_set.assert_not_called()


def test_repo_not_allowed_refuses(make_cfg, tmp_path, incus):
    cfg = _cfg_cap(make_cfg, tmp_path, allowed=False)
    with pytest.raises(GrantError, match="not allowed"):
        commands.grant(cfg, incus, "myrepo-a", GrantFlags())
    incus.config_set.assert_not_called()


@pytest.mark.parametrize("op", ["spawn", "message"])
def test_implied_ops_cannot_be_denied(cfg, op):
    with pytest.raises(GrantError, match=r"spawn and message are part of every grant.*revoke"):
        commands.build_grant(cfg, None, GrantFlags(deny=(op,)))


def test_unknown_op_lists_operations(cfg):
    with pytest.raises(GrantError, match=r"bogus.*destroy"):
        commands.build_grant(cfg, None, GrantFlags(allow=("bogus",)))
    with pytest.raises(GrantError, match=r"bogus"):
        commands.build_grant(cfg, None, GrantFlags(deny=("bogus",)))


def test_deny_removes_operation(cfg):
    g = commands.build_grant(cfg, None, GrantFlags(deny=("stop",)))
    assert "stop" not in g.operations


def test_unknown_mount_names_known_ones(cfg_with_mount_data):
    with pytest.raises(GrantError, match=r"nope.*data"):
        commands.build_grant(cfg_with_mount_data, None, GrantFlags(mounts=("nope",)))


def test_invalid_egress_entry_is_grant_error(cfg):
    with pytest.raises(GrantError, match="egress"):
        commands.build_grant(cfg, None, GrantFlags(egress=("not a host!",)))


def test_flags_start_from_current_grant_without_profile(cfg):
    cur = Grant(max_running_children=7)
    assert commands.build_grant(cfg, cur, GrantFlags(loose=True)).max_running_children == 7


def test_profile_replaces_current(cfg_with_profile_bugfixer):
    cur = Grant(max_running_children=7)
    g = commands.build_grant(cfg_with_profile_bugfixer, cur, GrantFlags(profile="bugfixer"))
    assert g.max_running_children == 5


def test_clear_mounts(cfg_with_mount_data):
    cur = Grant(grantable=Grantable(mounts=("data",)))
    g = commands.build_grant(cfg_with_mount_data, cur, GrantFlags(clear_mounts=True))
    assert g.grantable.mounts == ()


def test_clear_egress_and_list_flags_replace_whole_list(cfg_with_mount_data):
    cur = Grant(grantable=Grantable(mounts=("data",), egress=("a.org", "b.org")))
    g0 = commands.build_grant(cfg_with_mount_data, cur, GrantFlags(clear_egress=True))
    assert g0.grantable.egress == ()
    g = commands.build_grant(cfg_with_mount_data, cur, GrantFlags(egress=("c.org",)))
    assert g.grantable.egress == ("c.org",) and g.grantable.mounts == ("data",)


def test_mounts_with_clear_mounts_is_an_error(cfg_with_mount_data):
    with pytest.raises(GrantError, match="clear-mounts"):
        commands.build_grant(
            cfg_with_mount_data, None, GrantFlags(mounts=("data",), clear_mounts=True)
        )


def test_egress_with_clear_egress_is_an_error(cfg):
    with pytest.raises(GrantError, match="clear-egress"):
        commands.build_grant(cfg, None, GrantFlags(egress=("a.org",), clear_egress=True))


def test_cap_excess_refuses_and_names_it(cfg_with_cap_limit_1, incus):
    with pytest.raises(GrantError, match="1"):
        commands.grant(cfg_with_cap_limit_1, incus, "myrepo-a", GrantFlags(children=3))
    incus.config_set.assert_not_called()


def test_cap_excess_names_every_excess(make_cfg, tmp_path, incus):
    cfg = _cfg_cap(
        make_cfg,
        tmp_path,
        max_running_children=1,
        mounts=[],
        egress=["ok.org"],
        loose=False,
    )
    flags = GrantFlags(children=3, mounts=("data",), egress=("evil.org",), loose=True)
    with pytest.raises(GrantError) as exc:
        commands.grant(cfg, incus, "myrepo-a", flags)
    msg = str(exc.value)
    for needle in ("max_running_children", "'data'", "evil.org", "loose"):
        assert needle in msg
    incus.config_set.assert_not_called()


def test_malformed_label_shows_problem_and_grant_overwrites(cfg, make_incus, ensure):
    incus = make_incus({labels.GRANT: "{"})
    row = commands.show(cfg, incus, "myrepo-a")
    assert row.problem is not None and row.problem.startswith("invalid grant label")
    assert row.grant is None and row.effective is None
    commands.grant(cfg, incus, "myrepo-a", GrantFlags())
    incus.config_set.assert_called_once()


def test_unknown_key_label_reports_invalid_grant(cfg, make_incus):
    incus = make_incus({labels.GRANT: '{"version":1,"surprise":true}'})
    row = commands.show(cfg, incus, "myrepo-a")
    assert row.problem is not None and "invalid grant" in row.problem


def test_show_valid_grant_has_effective_and_children(cfg, make_incus):
    incus = make_incus({labels.GRANT: Grant(max_running_children=9).to_label()})
    incus.list_containers.return_value = [
        {"name": "myrepo-c1", "status": "Running", "config": {labels.SPAWNED_BY: "myrepo-a"}},
    ]
    row = commands.show(cfg, incus, "myrepo-a")
    assert row.short == "a" and row.problem is None
    assert row.grant == Grant(max_running_children=9) and row.effective is not None
    assert row.running_children == 1


def test_show_without_grant(cfg, incus):
    row = commands.show(cfg, incus, "myrepo-a")
    assert row.grant is None and row.problem is None and row.effective is None


def test_revoke(make_incus):
    incus = make_incus({labels.GRANT: Grant().to_label()})
    assert commands.revoke(incus, "myrepo-a") is True
    incus.config_unset_checked.assert_called_once_with("myrepo-a", labels.GRANT)
    assert commands.revoke(make_incus({}), "myrepo-a") is False


def test_list_grants_counts_running_children(cfg, incus):
    incus.list_containers.return_value = [
        {"name": "myrepo-orch", "status": "Running", "config": {labels.GRANT: Grant().to_label()}},
        {"name": "myrepo-c1", "status": "Running", "config": {labels.SPAWNED_BY: "myrepo-orch"}},
        {"name": "myrepo-c2", "status": "Stopped", "config": {labels.SPAWNED_BY: "myrepo-orch"}},
        {"name": "other-x", "status": "Running", "config": {labels.GRANT: Grant().to_label()}},
    ]
    rows = commands.list_grants(cfg, incus)
    assert [(r.short, r.running_children) for r in rows] == [("orch", 1)]
    incus.list_containers.assert_called_once()


def test_list_grants_reports_malformed_and_sorts(cfg, incus):
    incus.list_containers.return_value = [
        {"name": "myrepo-z", "status": "Running", "config": {labels.GRANT: Grant().to_label()}},
        {"name": "myrepo-b", "status": "Running", "config": {labels.GRANT: "{"}},
    ]
    rows = commands.list_grants(cfg, incus)
    assert [r.short for r in rows] == ["b", "z"]
    assert rows[0].problem is not None and "invalid grant" in rows[0].problem
    assert rows[0].grant is None and rows[1].problem is None


def test_default_profile_skips_children_and_mount_mode(
    cfg_with_profile_bugfixer_default, make_incus, ensure
):
    for lbls in ({labels.SPAWNED_BY: "myrepo-o"}, {"user.jailbee.mode": "mount"}):
        incus = make_incus(lbls)
        assert (
            commands.apply_default_profile(cfg_with_profile_bugfixer_default, incus, "myrepo-a")
            is None
        )
        incus.config_set.assert_not_called()


def test_default_profile_none_without_default(cfg, incus, ensure):
    assert commands.apply_default_profile(cfg, incus, "myrepo-a") is None
    incus.config_set.assert_not_called()


def test_default_profile_skips_over_cap(make_cfg, tmp_path, incus, ensure):
    view = OrchestrationView(
        profiles=PROFILES,
        default_profile="bugfixer",
        repo=RepoOrchestration(max_running_children=1),
    )
    cfg = _cfg(make_cfg, tmp_path, view=view, mounts=True)
    assert commands.apply_default_profile(cfg, incus, "myrepo-a") is None
    incus.config_set.assert_not_called()


def test_default_profile_applies(cfg_with_profile_bugfixer_default, make_incus, ensure):
    incus = make_incus({})
    g = commands.apply_default_profile(cfg_with_profile_bugfixer_default, incus, "myrepo-a")
    assert g is not None and g.max_running_children == 5
    incus.config_set.assert_called_once_with("myrepo-a", labels.GRANT, g.to_label())
    ensure.assert_called_once()


def test_repo_registered_queries_by_prefix(cfg, mocker):
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    mocker.stopall()
    assert commands.repo_registered(cfg) is False
    with Session(get_engine()) as session:
        session.add(
            RegisteredRepo(
                container_prefix="other",
                repo_root="/x",
                registered_at=datetime(2026, 8, 28, tzinfo=UTC),
            )
        )
        session.commit()
    assert commands.repo_registered(cfg) is False
    with Session(get_engine()) as session:
        session.add(
            RegisteredRepo(
                container_prefix="myrepo",
                repo_root="/y",
                registered_at=datetime(2026, 8, 28, tzinfo=UTC),
            )
        )
        session.commit()
    assert commands.repo_registered(cfg) is True
