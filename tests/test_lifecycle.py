"""Tests for lifecycle (new/start/stop/destroy/ls/shell)."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.config import CONTAINER_USERNAME, NewConfig, load_config
from jailbee.lifecycle import (
    NewContainerOptions,
    ResolvedContainer,
    derive_container_name,
    destroy_container,
    list_containers,
    new_container,
    resolve_container_for_interactive,
    resolve_container_for_interactive_detailed,
    resolve_container_name,
    restart_container,
    switch_network,
)
from tests.conftest import with_agent

FIXTURES = Path(__file__).parent / "fixtures"


def test_format_bytes():
    from jailbee.lifecycle import _format_bytes

    assert _format_bytes(0) == "0B"
    assert _format_bytes(512) == "512B"
    assert _format_bytes(4_000_000_000) == "3.7G"
    assert _format_bytes(2 * 1024**3) == "2.0G"
    assert _format_bytes(700 * 1024**2) == "700.0M"


def test_format_duration_short_renders_compactly() -> None:
    from datetime import timedelta

    from jailbee.lifecycle import format_duration_short

    assert format_duration_short(timedelta(hours=4)) == "4h"
    assert format_duration_short(timedelta(hours=3, minutes=59)) == "3h 59m"
    assert format_duration_short(timedelta(minutes=12)) == "12m"
    assert format_duration_short(timedelta(minutes=2, seconds=30)) == "2m"
    assert format_duration_short(timedelta(seconds=45)) == "45s"
    assert format_duration_short(timedelta(seconds=0)) == "0s"
    assert format_duration_short(timedelta(seconds=-30)) == "0s"
    assert format_duration_short(timedelta(hours=24)) == "24h"


def test_ttl_cell_renders_hours_for_a_long_ttl() -> None:
    """A 4h TTL must not read as `240m`."""
    from datetime import UTC, datetime, timedelta

    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    ci = ContainerInfo(
        name="myrepo-feat-x",
        state="Running",
        network="loose",
        ip=None,
        memory_limit=None,
        loose_until=now + timedelta(hours=4),
    )
    ttl = next(f for f in ls_field_specs(now=now) if f.name == "ttl")
    assert ttl.cell(ci) == "4h"


def _container(
    name="myrepo-feat-foo",
    state="Running",
    ip="10.0.0.5",
    profiles=None,
    memory="4GB",
    created_at=None,
    user_config=None,
):
    config = {"limits.memory": memory}
    if user_config:
        config.update(user_config)
    raw = {
        "name": name,
        "status": state,
        "profiles": profiles or ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
        "state": {
            "network": {"eth0": {"addresses": [{"address": ip, "family": "inet"}]}},
            "memory": {"usage": 4_000_000_000},
        },
        "config": config,
    }
    if created_at is not None:
        raw["created_at"] = created_at
    return raw


# ---- list_containers ----


def test_list_containers_returns_only_own_repo(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-foo"),
        _container(
            name="other-feat-bar",
            profiles=["default", "other-base", "other-binds", "other-net-strict"],
        ),
        _container(name="unmanaged", profiles=["default"]),
    ]
    result = list_containers(cfg, incus)
    assert [c.name for c in result] == ["myrepo-feat-foo"]
    assert result[0].repo == "myrepo"


def test_list_containers_skips_null_profiles(make_cfg, tmp_path):
    """A container mid-destroy can be reported by `incus list` with
    ``"profiles": null``. The key exists with a null value, so the
    ``.get("profiles", [])`` default is bypassed — guard against it
    rather than crashing with ``TypeError: 'NoneType' object is not
    iterable``."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": "myrepo-deleting", "status": "Stopped", "profiles": None},
        _container(name="myrepo-feat-foo"),
    ]
    result = list_containers(cfg, incus)
    assert [c.name for c in result] == ["myrepo-feat-foo"]


def test_list_containers_all_repos(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-foo"),
        _container(
            name="other-feat-bar",
            profiles=["default", "other-base", "other-binds", "other-net-strict"],
        ),
        _container(name="unmanaged", profiles=["default"]),
    ]
    result = list_containers(cfg, incus, all_repos=True)
    names = [c.name for c in result]
    assert "myrepo-feat-foo" in names
    assert "other-feat-bar" in names
    assert "unmanaged" not in names
    by_name = {c.name: c.repo for c in result}
    assert by_name["myrepo-feat-foo"] == "myrepo"
    assert by_name["other-feat-bar"] == "other"


def test_list_containers_parses_created_at(make_cfg, tmp_path):
    from datetime import datetime

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        # RFC3339Nano, as Incus emits it (nanosecond precision + trailing Z).
        _container(name="myrepo-feat-foo", created_at="2026-07-13T14:30:00.123456789Z"),
    ]
    (result,) = list_containers(cfg, incus)
    assert result.created_at == datetime.fromisoformat("2026-07-13T14:30:00.123456+00:00")


def test_list_containers_created_at_absent_or_zero_is_none(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-a"),  # no created_at key
        _container(name="myrepo-b", created_at="0001-01-01T00:00:00Z"),  # Go zero time
        _container(name="myrepo-c", created_at="not-a-timestamp"),
    ]
    result = {c.name: c for c in list_containers(cfg, incus)}
    assert result["myrepo-a"].created_at is None
    assert result["myrepo-b"].created_at is None
    assert result["myrepo-c"].created_at is None


def test_list_containers_sorts_newest_first(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-old", created_at="2026-01-01T00:00:00Z"),
        _container(name="myrepo-new", created_at="2026-07-13T00:00:00Z"),
        _container(name="myrepo-mid", created_at="2026-04-01T00:00:00Z"),
        # No creation time -> treated as newest (mid-creation / legacy).
        _container(name="myrepo-unknown"),
    ]
    result = [c.name for c in list_containers(cfg, incus)]
    assert result == ["myrepo-unknown", "myrepo-new", "myrepo-mid", "myrepo-old"]


def test_list_containers_extracts_network_mode(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-a",
            profiles=["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
        ),
        _container(
            name="myrepo-b", profiles=["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"]
        ),
    ]
    result = list_containers(cfg, incus)
    by_name = {c.name: c for c in result}
    assert by_name["myrepo-a"].network == "strict"
    assert by_name["myrepo-b"].network == "loose"


def test_list_containers_extracts_ip_for_running(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-a", ip="10.156.171.42"),
    ]
    result = list_containers(cfg, incus)
    assert result[0].ip == "10.156.171.42"


def test_list_containers_handles_stopped_no_state(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-stopped",
            "status": "Stopped",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {},
        }
    ]
    result = list_containers(cfg, incus)
    assert result[0].state == "Stopped"
    assert result[0].ip is None


def test_list_containers_handles_stopped_with_null_network(make_cfg, tmp_path):
    """Real `incus list --format json` output for a stopped container has
    state.network = null (not {} and not absent). dict.get(k, {}) returns
    None when the key is present-with-None, so each level needs ``or {}``.
    Regression for AttributeError seen after `gie stop` + `gie ls`.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-stopped",
            "status": "Stopped",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"],
            "state": {"network": None, "memory": {"usage": 0}},
            "config": {"limits.memory": "16GiB"},
        }
    ]
    result = list_containers(cfg, incus)
    assert result[0].state == "Stopped"
    assert result[0].ip is None
    assert result[0].network == "loose"


def test_list_containers_populates_memory_usage_for_running(make_cfg, tmp_path):
    """Running container with state.memory.usage set -> memory_usage int."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    # _container() fixture has state.memory.usage = 4_000_000_000 for running
    incus.list_containers.return_value = [_container(name="myrepo-feat-x", state="Running")]
    result = list_containers(cfg, incus)
    assert result[0].memory_usage == 4_000_000_000


def test_list_containers_memory_usage_none_for_stopped_without_memory(make_cfg, tmp_path):
    """Stopped container without state.memory -> memory_usage is None."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-stopped",
            "status": "Stopped",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            "state": None,
            "config": {},
        }
    ]
    result = list_containers(cfg, incus)
    assert result[0].memory_usage is None


def test_memory_columns_are_dashboard_only_and_json_stays_stable():
    """Neither memory column is in the `ls` default table; MEM is the
    dashboard's. MEM is a live sample, so it earns its width in a view that
    refreshes and not in a one-shot listing; MEMORY LIMIT is the `--fields`
    escape hatch for the bare limit."""
    from datetime import UTC, datetime

    from jailbee.lifecycle import ls_field_specs
    from jailbee.table_format import shows_by_default_in_dashboard

    specs = ls_field_specs(now=datetime(2026, 6, 8, tzinfo=UTC), all_repos=False)
    by_name = {f.name: f for f in specs}
    assert by_name["mem"].default_table is False
    assert by_name["memory_limit"].default_table is False
    assert shows_by_default_in_dashboard(by_name["mem"]) is True
    assert shows_by_default_in_dashboard(by_name["memory_limit"]) is False
    # JSON stays backward-compatible: memory_limit is emitted, mem is opt-in.
    assert by_name["memory_limit"].default_json is True
    assert by_name["mem"].default_json is False


def test_ls_mem_cell_formats_used_and_limit():
    """The mem cell shows 'used / limit' for running, bare limit otherwise."""
    from datetime import UTC, datetime

    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    mem = next(
        f
        for f in ls_field_specs(now=datetime(2026, 6, 8, tzinfo=UTC), all_repos=False)
        if f.name == "mem"
    )
    running = ContainerInfo(
        name="r",
        state="Running",
        network=None,
        ip=None,
        memory_limit="8GiB",
        memory_usage=4_000_000_000,
    )
    assert mem.cell(running) == "3.7G / 8GiB"
    stopped = ContainerInfo(name="s", state="Stopped", network=None, ip=None, memory_limit="8GiB")
    assert mem.cell(stopped) == "8GiB"


def test_ls_created_cell_renders_local_time_or_dash():
    """The CREATED cell shows a compact local timestamp, or a dim dash."""
    from datetime import UTC, datetime

    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    created = next(
        f
        for f in ls_field_specs(now=datetime(2026, 6, 8, tzinfo=UTC), all_repos=False)
        if f.name == "created"
    )
    dt = datetime(2026, 7, 13, 14, 30, tzinfo=UTC)
    dated = ContainerInfo(
        name="d", state="Running", network=None, ip=None, memory_limit=None, created_at=dt
    )
    assert created.cell(dated) == dt.astimezone().strftime("%Y-%m-%d %H:%M")
    assert created.json(dated) == dt.isoformat()

    undated = ContainerInfo(name="u", state="Running", network=None, ip=None, memory_limit=None)
    assert created.cell(undated) == "[dim]—[/dim]"
    assert created.json(undated) is None


def test_list_containers_populates_loose_until(make_cfg, tmp_path):
    from datetime import UTC, datetime

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-x",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.loose_until": "2026-05-20T12:05:00+00:00",
            },
        )
    ]

    infos = list_containers(cfg, incus)
    assert infos[0].loose_until == datetime(2026, 5, 20, 12, 5, 0, tzinfo=UTC)


def test_list_containers_loose_until_none_when_unset(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-x", user_config={"user.jailbee.mode": "clone"})
    ]

    infos = list_containers(cfg, incus)
    assert infos[0].loose_until is None


def test_list_containers_loose_until_corrupt_treated_as_none(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-x",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.loose_until": "not-a-timestamp",
            },
        )
    ]

    infos = list_containers(cfg, incus)
    assert infos[0].loose_until is None


def test_list_containers_populates_base_branch_from_label(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-x",
            user_config={"user.jailbee.mode": "clone", "user.jailbee.base_branch": "develop"},
        )
    ]

    infos = list_containers(cfg, incus)
    assert infos[0].base_branch == "develop"
    assert infos[0].git_status is None


def test_list_containers_base_branch_is_none_when_label_absent(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-x", user_config={"user.jailbee.mode": "clone"})
    ]

    infos = list_containers(cfg, incus)
    assert infos[0].base_branch is None


def test_list_containers_populates_pr_author(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-x",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.pr": "123",
                "user.jailbee.pr_author": "1",
            },
        ),
    ]
    info = list_containers(cfg, incus)[0]
    assert info.pr_number == 123
    assert info.pr_author is True


def test_list_containers_populates_pr_review_checkout(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-x",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.pr": "456",
            },
        ),
    ]
    info = list_containers(cfg, incus)[0]
    assert info.pr_number == 456
    assert info.pr_author is False


def test_list_containers_pr_absent(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-x", user_config={"user.jailbee.mode": "clone"}),
    ]
    info = list_containers(cfg, incus)[0]
    assert info.pr_number is None
    assert info.pr_author is False


def test_list_containers_pr_malformed_treated_as_none(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-x",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.pr": "not-a-number",
                "user.jailbee.pr_author": "1",
            },
        ),
    ]
    info = list_containers(cfg, incus)[0]
    assert info.pr_number is None
    assert info.pr_author is True


def test_list_containers_with_git_status_invokes_probe_for_running_clone_only(
    make_cfg, tmp_path, mocker
):
    from jailbee.git_status import GitStatus

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-a",
            state="Running",
            user_config={"user.jailbee.mode": "clone", "user.jailbee.repo_dir": "/home/dev/repo"},
        ),
        _container(
            name="myrepo-b",
            state="Stopped",
            user_config={"user.jailbee.mode": "clone", "user.jailbee.repo_dir": "/home/dev/repo"},
        ),
        _container(
            name="myrepo-c",
            state="Running",
            user_config={"user.jailbee.mode": "mount", "user.jailbee.repo_dir": "/home/dev/repo"},
        ),
    ]

    probe_mock = mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={
            "myrepo-a": GitStatus(wt="+1 -0", ahead_diff="clean", ahead_count="0", conflict="ok"),
        },
    )

    containers = list_containers(cfg, incus, with_git_status=True)
    by_name = {c.name: c for c in containers}

    probe_mock.assert_called_once()
    args, _kwargs = probe_mock.call_args
    targets = args[1]
    assert targets == [("myrepo-a", "/home/dev/repo", None)]

    assert by_name["myrepo-a"].git_status is not None
    assert by_name["myrepo-a"].git_status.wt == "+1 -0"
    assert by_name["myrepo-b"].git_status is None
    assert by_name["myrepo-c"].git_status is None


def test_list_containers_passes_base_branch_to_probe(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-bb",
            state="Running",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.repo_dir": "/home/dev/repo",
                "user.jailbee.base_branch": "dev",
            },
        ),
    ]

    probe_mock = mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={},
    )

    list_containers(cfg, incus, with_git_status=True)

    probe_mock.assert_called_once()
    args, _kwargs = probe_mock.call_args
    targets = args[1]
    assert targets == [("myrepo-feat-bb", "/home/dev/repo", "dev")]
    assert args[2] == cfg.default_branch


def test_list_containers_passes_the_host_head_to_the_probe(make_cfg, tmp_path, mocker):
    """One `git rev-parse HEAD` per listing, not per container."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-a",
            state="Running",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.repo_dir": "/home/dev/repo",
                "user.jailbee.base_branch": "main",
            },
        ),
        _container(
            name="myrepo-feat-b",
            state="Running",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.repo_dir": "/home/dev/repo",
                "user.jailbee.base_branch": "main",
            },
        ),
    ]
    head = mocker.patch("jailbee.lifecycle.get_head_sha", return_value="deadbeef")
    probe = mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={},
    )

    list_containers(cfg, incus, with_git_status=True)

    # Two containers, one host-side rev-parse.
    assert head.call_count == 1
    assert probe.call_args.kwargs["host_head"] == "deadbeef"


# ---- _resolve_local_on_host ----


def test_resolve_local_on_host_fills_in_from_host_objects(make_cfg, tmp_path, mocker):
    """The container did not hold the host's tip, but the host holds the
    container's HEAD — a previous `gie git pull` put it there."""
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import _resolve_local_on_host

    cfg = make_cfg(tmp_path)
    status = GitStatus(
        wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok", head_sha="abc123"
    )
    mocker.patch("jailbee.lifecycle.has_commit", return_value=True)
    mocker.patch(
        "jailbee.lifecycle.diff_shortstat_between",
        return_value=" 2 files changed, 12 insertions(+), 3 deletions(-)",
    )
    mocker.patch("jailbee.lifecycle.count_commits_between", return_value="3")

    out = _resolve_local_on_host(cfg, status)

    assert out.local_diff == "+12 -3"
    assert out.local_count == "3"


def test_resolve_local_on_host_is_a_noop_when_the_container_answered(make_cfg, tmp_path, mocker):
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import _resolve_local_on_host

    cfg = make_cfg(tmp_path)
    status = GitStatus(
        wt="clean",
        ahead_diff="clean",
        ahead_count="0",
        conflict="ok",
        head_sha="abc123",
        local_diff="+1 -1",
        local_count="1",
    )
    has = mocker.patch("jailbee.lifecycle.has_commit")

    assert _resolve_local_on_host(cfg, status) is status
    has.assert_not_called()


def test_resolve_local_on_host_is_a_noop_without_a_head_sha(make_cfg, tmp_path, mocker):
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import _resolve_local_on_host

    cfg = make_cfg(tmp_path)
    status = GitStatus(wt="?", ahead_diff="?", ahead_count="?", conflict="?")
    has = mocker.patch("jailbee.lifecycle.has_commit")

    assert _resolve_local_on_host(cfg, status) is status
    has.assert_not_called()


def test_resolve_local_on_host_stays_unknown_when_neither_side_has_the_objects(
    make_cfg, tmp_path, mocker
):
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import _resolve_local_on_host

    cfg = make_cfg(tmp_path)
    status = GitStatus(
        wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok", head_sha="abc123"
    )
    mocker.patch("jailbee.lifecycle.has_commit", return_value=False)

    out = _resolve_local_on_host(cfg, status)

    assert out.local_diff == "?"
    assert out.local_count == "?"


def test_resolve_local_on_host_stays_unknown_when_the_host_diff_fails(make_cfg, tmp_path, mocker):
    """A present object but a failing diff must not be reported as clean."""
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import _resolve_local_on_host

    cfg = make_cfg(tmp_path)
    status = GitStatus(
        wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok", head_sha="abc123"
    )
    mocker.patch("jailbee.lifecycle.has_commit", return_value=True)
    mocker.patch("jailbee.lifecycle.diff_shortstat_between", return_value=None)
    mocker.patch("jailbee.lifecycle.count_commits_between", return_value="3")

    out = _resolve_local_on_host(cfg, status)

    assert out.local_diff == "?"
    assert out.local_count == "?"


def test_list_containers_default_does_not_invoke_probe(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-a", state="Running", user_config={"user.jailbee.mode": "clone"})
    ]

    probe_mock = mocker.patch("jailbee.lifecycle.probe_many_parallel")
    list_containers(cfg, incus)
    probe_mock.assert_not_called()


def test_list_containers_makes_no_config_get_calls(make_cfg, tmp_path, mocker):
    """The refactor reads user.jailbee.* from the incus list payload, not via
    per-key `incus config get`. Pin that: list_containers must never call
    incus.config_get (its whole purpose is to avoid those subprocess spawns).
    """
    from jailbee.git_status import GitStatus

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-feat-x",
            user_config={
                "user.jailbee.mode": "clone",
                "user.jailbee.base_branch": "main",
                "user.jailbee.repo_dir": "/home/dev/repo",
                "user.jailbee.pr": "123",
                "user.jailbee.pr_author": "1",
            },
        ),
    ]

    mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={
            "myrepo-feat-x": GitStatus(
                wt="+0 -0", ahead_diff="clean", ahead_count="0", conflict="ok"
            ),
        },
    )

    list_containers(cfg, incus, with_git_status=True, with_background=True)

    incus.config_get.assert_not_called()


def test_list_containers_threads_fast_and_timeout(mocker, tmp_path):
    """lifecycle passes the completion knobs straight through to Incus."""
    from jailbee.lifecycle import list_containers
    from tests.conftest import make_cfg

    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    cfg = make_cfg(repo_root)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []

    list_containers(cfg, incus, fast=True, timeout=2)

    incus.list_containers.assert_called_once_with(fast=True, timeout=2)


def test_list_containers_tolerates_null_state_from_fast(mocker, tmp_path):
    """`--fast` returns state: null; the container must still be listed."""
    from jailbee.lifecycle import list_containers
    from tests.conftest import make_cfg

    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    cfg = make_cfg(repo_root)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-feat-foo",
            "status": "Running",
            "profiles": ["myrepo-base"],
            "config": {},
            "state": None,
        }
    ]

    infos = list_containers(cfg, incus, fast=True)

    assert [c.name for c in infos] == ["myrepo-feat-foo"]
    assert infos[0].ip is None
    assert infos[0].memory_usage is None


def test_new_container_sets_default_base_branch_label(make_cfg, tmp_path, mocker):
    """Default flow tags container with cfg.default_branch as base_branch."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    mocker.patch(
        "jailbee.lifecycle.rev_parse_remote",
        return_value="abc1234",
    )
    mocker.patch("jailbee.lifecycle.fetch_remote_ref")

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    calls = [c for c in incus.config_set.call_args_list if c.args[1] == "user.jailbee.base_branch"]
    assert len(calls) == 1
    assert calls[0].args[2] == "main"


def test_new_container_uses_explicit_base_branch_label_when_provided(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        side_effect=lambda root, remote, b: b == "feat/foo",
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=False,
        base_branch_label="develop",
    )
    new_container(cfg, incus, opts)

    calls = [c for c in incus.config_set.call_args_list if c.args[1] == "user.jailbee.base_branch"]
    assert calls[0].args[2] == "develop"


def test_resolve_container_for_interactive_uses_with_git_status(make_cfg, tmp_path, mocker):
    """The interactive picker path fetches git status for richer labels."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()

    list_mock = mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[],
    )

    try:
        resolve_container_for_interactive(cfg, incus, None)
    except ValueError:
        # We expect a ValueError ("no managed containers found"). We
        # care about how list_containers was called, not the resolution.
        pass

    assert list_mock.call_args.kwargs.get("with_git_status") is True


def test_new_container_mount_mode_uses_default_branch_label(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="",
        name=f"{cfg.container_prefix}-mountsmoke",
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=False,
        autostart=False,
        mount=True,
    )
    new_container(cfg, incus, opts)

    calls = [c for c in incus.config_set.call_args_list if c.args[1] == "user.jailbee.base_branch"]
    assert len(calls) == 1
    assert calls[0].args[2] == "main"


def test_switch_network_does_not_touch_loose_labels(make_cfg, tmp_path, mocker):
    """Invariant: switch_network is the low-level primitive; CLI-layer
    label lifecycle must not leak into autostart/internal callers."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [_container(name="myrepo-feat-x")]
    mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch("jailbee.hosts.clear_hosts")

    switch_network(cfg, incus, "myrepo-feat-x", "strict")

    set_calls = list(incus.config_set.call_args_list)
    unset_calls = list(incus.config_unset.call_args_list)
    for call in set_calls + unset_calls:
        assert "user.jailbee.loose_until" not in call.args
        assert "user.jailbee.loose_revert_to" not in call.args


# ---- derive_container_name ----


def test_derive_container_name_replaces_slashes(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    assert derive_container_name(cfg, "feat/dimension-groups") == "myrepo-feat-dimension-groups"


def test_derive_container_name_strips_dot_prefixes(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    assert derive_container_name(cfg, "scratch/.hidden") == "myrepo-scratch-hidden"


def test_derive_container_name_lowercases(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    assert derive_container_name(cfg, "Feat/MyBranch") == "myrepo-feat-mybranch"


def test_derive_container_name_sanitizes_punctuation(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    # `#` is legal in git refs but not in Incus names — sanitize, don't reject.
    assert derive_container_name(cfg, "feat/#14633-work-time") == "myrepo-feat-14633-work-time"


def test_derive_container_name_sanitizes_whitespace(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    assert derive_container_name(cfg, "feat/with spaces") == "myrepo-feat-with-spaces"


def test_derive_container_name_collapses_dash_runs(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    assert derive_container_name(cfg, "feat//double--slash") == "myrepo-feat-double-slash"


def test_derive_container_name_validates_full_prefixed_name(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    with pytest.raises(ValueError, match="invalid container name"):
        derive_container_name(cfg, "...")


# ---- new_container ----


def _cfg_for_new(tmp_path, *, clone_from="local", autofetch=False):
    """Build a Config for new_container tests.

    Defaults to local clone mode so legacy tests (which assert the
    classic `git clone --branch <default>` flow) keep working without
    setting up host-side fetch/rev-parse mocks. Origin-mode tests opt
    in with ``clone_from="origin"``.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(
        update={
            "shared_dir": tmp_path / "shared",
            "new": NewConfig(clone_from=clone_from, autofetch=autofetch),
        }
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    object.__setattr__(cfg, "repo_root", repo)
    object.__setattr__(cfg, "default_branch", "dev")
    object.__setattr__(cfg, "container_prefix", repo.name)
    return cfg


def test_new_container_calls_init_assign_set_start(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    incus.init.assert_called_once_with("gisgro-base", "repo-feat-x")
    incus.profile_assign.assert_called_once_with(
        "repo-feat-x",
        ["default", "repo-base", "repo-binds", "repo-net-strict"],
    )
    config_set_keys = {call.args[1] for call in incus.config_set.call_args_list}
    assert "limits.memory" in config_set_keys
    assert "limits.cpu" in config_set_keys
    incus.start.assert_called_once_with("repo-feat-x")


def test_new_container_adds_dev_to_host_device_groups(tmp_path, mocker):
    """A host_devices entry's group is provisioned: dev is added to it inside
    the container so it can open the (static_node-reset) device node."""
    from jailbee.config import HostDevice

    cfg = _cfg_for_new(tmp_path)
    cfg = cfg.model_copy(update={"host_devices": [HostDevice(path="/dev/kvm", group="kvm")]})
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    usermod_calls = [
        call
        for call in incus.exec.call_args_list
        if any("usermod -aG kvm dev" in str(arg) for arg in call.args)
    ]
    assert usermod_calls, "expected a usermod -aG kvm dev exec for host_devices group"


def test_new_container_injects_github_token_even_with_no_autostart(tmp_path, mocker):
    """--no-autostart skips the user's autostart commands but must NOT skip
    GH_TOKEN injection — `gh` has to work in every container (#bug)."""
    from jailbee.config import GithubConfig

    cfg = _cfg_for_new(tmp_path)
    cfg = cfg.model_copy(
        update={
            "github": GithubConfig(
                enabled=True,
                api_tokens={cfg.container_prefix: "github_pat_xxx"},
            )
        }
    )
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    inject = mocker.patch("jailbee.autostart.inject_github_token")
    run_autostart = mocker.patch("jailbee.autostart.run_autostart")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    # Token injected despite autostart being disabled...
    inject.assert_called_once()
    assert inject.call_args.args[2] == "repo-feat-x"
    # ...but the user's autostart steps were skipped.
    run_autostart.assert_not_called()


def test_new_container_persists_user_gie_branch(tmp_path):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    config_set_calls = [c.args for c in incus.config_set.call_args_list]
    assert ("repo-feat-x", "user.jailbee.branch", "feat/x") in config_set_calls
    # The branch is also surfaced as an env var so the in-container bash
    # prompt can render it. Same source as user.jailbee.branch.
    assert ("repo-feat-x", "environment.JAILBEE_BRANCH", "feat/x") in config_set_calls


def test_container_repo_dir_returns_label_when_present(tmp_path):
    """When user.jailbee.repo_dir is set, that exact path is returned."""
    from jailbee.lifecycle import container_repo_dir

    cfg = _cfg_for_new(tmp_path)
    object.__setattr__(cfg, "container_prefix", "gisgro")
    incus = MagicMock()
    incus.config_get.return_value = "/home/dev/gisgro"

    assert container_repo_dir(cfg, incus, "gisgro-feat-x") == "/home/dev/gisgro"
    incus.config_get.assert_called_once_with("gisgro-feat-x", "user.jailbee.repo_dir")


def test_container_repo_dir_falls_back_to_repo_root_name(tmp_path):
    """When the label is absent (pre-feature container), use repo_root.name."""
    from jailbee.lifecycle import container_repo_dir

    cfg = _cfg_for_new(tmp_path)
    object.__setattr__(cfg, "container_prefix", "gisgro")
    incus = MagicMock()
    incus.config_get.return_value = None

    # repo_root.name == "repo" per _cfg_for_new
    assert container_repo_dir(cfg, incus, "gisgro-feat-x") == "/home/dev/repo"


def test_container_repo_dir_default_prefix_matches_repo_root_name(tmp_path):
    """Default-prefix containers see the same path whether label is set or not."""
    from jailbee.lifecycle import container_repo_dir

    # repo_root.name == container_prefix == "repo"
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.config_get.return_value = "/home/dev/repo"

    assert container_repo_dir(cfg, incus, "repo-feat") == "/home/dev/repo"


def test_new_container_persists_repo_dir_label(tmp_path):
    """new_container writes user.jailbee.repo_dir = /home/dev/<container_prefix>.

    Uses a config where repo_root.name differs from container_prefix to
    verify the label tracks the prefix (the new behaviour) rather than
    the host directory name.
    """
    cfg = _cfg_for_new(tmp_path)
    object.__setattr__(cfg, "container_prefix", "gisgro")
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    config_set_calls = [c.args for c in incus.config_set.call_args_list]
    name = "gisgro-feat-x"
    assert (name, "user.jailbee.repo_dir", "/home/dev/gisgro") in config_set_calls


def test_new_container_clones_existing_branch_directly(tmp_path, mocker):
    """When branch exists in source, clone it directly (existing behaviour)."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=True,
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    # Check that "git clone --shared --branch feat/x" was called (no checkout -b)
    git_clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and len(c.args[1]) > 0
        and c.args[1][0] == "git"
        and "clone" in c.args[1]
    ]
    assert len(git_clone_calls) == 1
    cmd = git_clone_calls[0].args[1]
    assert "--branch" in cmd
    assert cmd[cmd.index("--branch") + 1] == "feat/x"

    checkout_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "checkout" in c.args[1]
    ]
    assert checkout_calls == []


def test_new_container_falls_back_to_default_branch_when_missing(tmp_path, mocker):
    """When branch doesn't exist in source, clone default_branch
    and create the new branch locally with `git checkout -b`."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    # Verify a `git clone --branch dev` call (default_branch from fixture)
    git_clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and c.args[1][0] == "git"
        and "clone" in c.args[1]
    ]
    assert len(git_clone_calls) == 1
    clone_cmd = git_clone_calls[0].args[1]
    assert clone_cmd[clone_cmd.index("--branch") + 1] == "dev"

    # And a follow-up `git checkout -b feat/new`
    checkout_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "checkout" in c.args[1]
    ]
    assert len(checkout_calls) == 1
    checkout_cmd = checkout_calls[0].args[1]
    assert "-b" in checkout_cmd
    assert "feat/new" in checkout_cmd


@pytest.mark.parametrize(
    ("base", "branch_in_source", "expected_substr"),
    [
        # base=None, branch exists → "cloning existing branch"
        (None, True, "cloning existing branch 'feat/x'"),
        # base=None, branch missing → "new branch ... off 'dev' (default)"
        (None, False, "new branch 'feat/x' off 'dev'"),
        # base given, branch missing → "new branch ... off '<base>'"
        ("feat/y", False, "new branch 'feat/x' off 'feat/y'"),
        # base given, branch exists → reused, and the base is named
        ("feat/y", True, "cloning existing branch 'feat/x' (base 'feat/y')"),
    ],
)
def test_new_container_announces_branch_decision(
    tmp_path, mocker, capsys, base, branch_in_source, expected_substr
):
    """`gie new` prints which of the clone paths it took so the user can see
    whether the branch was created or reused, and against which base."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""

    def host_check(_repo_root, _remote, name):
        # A given base must exist in source; whether <branch> does is what
        # decides between forking off the base and reusing the branch.
        if name == base:
            return True
        return branch_in_source

    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        side_effect=host_check,
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base=base,
    )
    new_container(cfg, incus, opts)

    # Rich wraps the captured output at 80 cols, so collapse whitespace
    # before checking the substring is present.
    out = " ".join(capsys.readouterr().out.split())
    assert expected_substr in out


def test_new_container_creates_branch_off_base(tmp_path, mocker):
    """`gie new X Y` clones Y then `git checkout -b X` (the new base-branch path)."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""

    # Pre-flight host check: branch X is NOT in source; base Y IS in source.
    def host_check(repo_root, remote, branch):
        del repo_root, remote
        return branch == "feat/y"

    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        side_effect=host_check,
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        side_effect=lambda repo_root, branch: host_check(repo_root, "origin", branch),
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base="feat/y",
    )
    new_container(cfg, incus, opts)

    # Exactly one `git clone --shared --branch feat/y`
    git_clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and c.args[1][0] == "git"
        and "clone" in c.args[1]
    ]
    assert len(git_clone_calls) == 1
    clone_cmd = git_clone_calls[0].args[1]
    assert clone_cmd[clone_cmd.index("--branch") + 1] == "feat/y"

    # Followed by a `git checkout -b feat/x`
    checkout_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "checkout" in c.args[1]
    ]
    assert len(checkout_calls) == 1
    checkout_cmd = checkout_calls[0].args[1]
    assert "-b" in checkout_cmd
    assert "feat/x" in checkout_cmd


def test_new_container_errors_when_base_missing_in_source(tmp_path, mocker):
    """`gie new X Y` errors out (before incus.init) when Y is not in source."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    # Both branch and base report missing → branch-already check passes,
    # then base-missing check raises.
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base="feat/y",
    )

    with pytest.raises(ValueError, match="Base branch 'feat/y' not found"):
        new_container(cfg, incus, opts)

    incus.init.assert_not_called()


def test_new_container_existing_branch_with_base_clones_it_and_labels_the_base(tmp_path, mocker):
    """`gie new X Y` with X already in source: clone X, base branch is Y.

    `base` means "this container's base branch", not "fork from" — the fork is
    only implied when X does not exist yet. For an existing branch (reviewing
    or continuing someone's work) the base is what `gie ls` AHEAD/MERGE and
    `gie git pull` measure against, and there is nothing to fork.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    # Both X and Y exist in source.
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base="feat/y",
    )
    new_container(cfg, incus, opts)

    clone_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "clone" in c.args[1]
    ]
    assert len(clone_cmds) == 1
    assert clone_cmds[0][clone_cmds[0].index("--branch") + 1] == "feat/x"

    # No `checkout -b`: the branch exists, so the clone already lands on it.
    checkout_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "checkout" in c.args[1]
    ]
    assert checkout_cmds == []

    incus.config_set.assert_any_call(
        f"{cfg.container_prefix}-feat-x", "user.jailbee.base_branch", "feat/y"
    )


def test_new_container_existing_branch_with_base_seeds_the_base_anchor(tmp_path, mocker):
    """`refs/jailbee/base/<base>` is seeded from the base, not from the branch itself.

    Without this, `gie ls` AHEAD would compare the branch against its own tip
    (always 0) instead of against the base the user named.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    rev_parse_origin = mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="basesha")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base="feat/y",
    )
    new_container(cfg, incus, opts)

    assert rev_parse_origin.call_args.args[2] == "feat/y"
    update_ref_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "update-ref" in c.args[1]
    ]
    assert any("refs/jailbee/base/feat/y" in cmd for cmd in update_ref_cmds)


def test_new_container_base_requires_clone(tmp_path):
    """`base` with `clone=False` (`--no-clone`) is a contradiction — error early."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        base="feat/y",
    )

    with pytest.raises(ValueError, match=r"base.*requires.*clone|--no-clone"):
        new_container(cfg, incus, opts)

    incus.init.assert_not_called()


def test_new_container_rewrites_origin_and_sets_tracking_for_existing_branch(tmp_path, mocker):
    """After clone, rewrite origin URL to upstream + set tracking.

    Branch-exists path: `clone --branch` auto-sets tracking, but we still
    write it explicitly (idempotent) so the `merge` ref follows the host's
    rather than whatever the clone inferred.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )
    mocker.patch(
        "jailbee.git.get_remote_url",
        return_value="git@github.com:Acme/repo.git",
    )
    mocker.patch(
        "jailbee.git.get_branch_tracking",
        return_value=("origin", "refs/heads/feat/x"),
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    git_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and c.args[1][0] == "git"
    ]
    set_url_cmds = [cmd for cmd in git_cmds if "remote" in cmd and "set-url" in cmd]
    assert len(set_url_cmds) == 1
    assert set_url_cmds[0][-2:] == ["origin", "git@github.com:Acme/repo.git"]

    branch_cfg_cmds = [
        cmd for cmd in git_cmds if "config" in cmd and any("branch.feat/x." in c for c in cmd)
    ]
    assert len(branch_cfg_cmds) == 2
    keys = {cmd[-2]: cmd[-1] for cmd in branch_cfg_cmds}
    assert keys["branch.feat/x.remote"] == "origin"
    assert keys["branch.feat/x.merge"] == "refs/heads/feat/x"


def test_new_container_tracking_remote_is_always_origin_in_the_container(tmp_path, mocker):
    """The container's `branch.<b>.remote` is jailbee's own invariant, not the host's.

    The in-container clone is `git clone --shared /mnt/host-source`, so its only
    remote is called `origin` no matter what the host calls its upstream. Copying
    the host's `branch.<b>.remote` verbatim configured the container to track a
    remote that does not exist there, and `git push` inside the container died
    with "'upstream' does not appear to be a git repository".

    Only `merge` is host-derived — that is a ref name on the upstream, which the
    two repos genuinely share.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    mocker.patch("jailbee.git.get_remote_url", return_value="git@github.com:Acme/repo.git")
    mocker.patch(
        "jailbee.git.get_branch_tracking",
        return_value=("upstream", "refs/heads/feat/x"),
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    git_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and c.args[1][0] == "git"
    ]
    branch_cfg_cmds = [
        cmd for cmd in git_cmds if "config" in cmd and any("branch.feat/x." in c for c in cmd)
    ]
    keys = {cmd[-2]: cmd[-1] for cmd in branch_cfg_cmds}
    assert keys["branch.feat/x.remote"] == "origin"
    assert keys["branch.feat/x.merge"] == "refs/heads/feat/x"


def test_new_container_seeds_base_ref_from_host_origin_sha(tmp_path, mocker):
    """Regression for the PR-review AHEAD bug: the container clone carries only
    the host's local refs/heads/*, so a PR's base branch (baseRefName) is
    usually absent as origin/<base> inside the container. The gie base ref must
    be seeded to the HOST's origin/<base> SHA so `gie ls` AHEAD compares against
    the real base instead of falling through to the default branch.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    rev = mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="a4ebc3ed1234")

    opts = NewContainerOptions(
        container_branch="alice/fix/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base_branch_label="release/0.98.0",
    )
    new_container(cfg, incus, opts)

    seed_calls = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "update-ref" in c.args[1]
    ]
    assert len(seed_calls) == 1
    cmd = seed_calls[0]
    i = cmd.index("update-ref")
    assert cmd[i + 1] == "refs/jailbee/base/release/0.98.0"
    assert cmd[i + 2] == "a4ebc3ed1234"
    rev.assert_any_call(cfg.repo_root, "origin", "release/0.98.0")


def test_new_container_skips_base_seed_when_host_lacks_base_ref(tmp_path, mocker):
    """When the base branch resolves on neither origin nor a local head, no
    base ref is seeded — the probe then honestly reports "?" rather than being
    handed a bogus ref."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value=None)
    mocker.patch("jailbee.lifecycle.rev_parse", return_value=None)

    opts = NewContainerOptions(
        container_branch="alice/fix/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base_branch_label="release/0.98.0",
    )
    new_container(cfg, incus, opts)

    seed_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "update-ref" in c.args[1]
    ]
    assert seed_calls == []


def test_new_container_rewrites_origin_and_sets_tracking_for_new_branch(tmp_path, mocker):
    """When branch was created locally (`checkout -b`), tracking
    isn't set by git — `gie` writes branch.<br>.remote/merge explicitly so
    `git push` works without `-u` on first use.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )
    mocker.patch(
        "jailbee.git.get_remote_url",
        return_value="git@github.com:Acme/repo.git",
    )
    # Branch is brand-new locally → host has no tracking config for it
    mocker.patch("jailbee.git.get_branch_tracking", return_value=None)

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    git_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and c.args[1][0] == "git"
    ]
    branch_cfg_cmds = [
        cmd for cmd in git_cmds if "config" in cmd and any("branch.feat/new." in c for c in cmd)
    ]
    keys = {cmd[-2]: cmd[-1] for cmd in branch_cfg_cmds}
    assert keys == {
        "branch.feat/new.remote": "origin",
        "branch.feat/new.merge": "refs/heads/feat/new",
    }


def test_new_container_skips_origin_rewrite_when_host_has_no_origin(tmp_path, mocker):
    """If host repo has no `origin` remote, leave the mount-path origin in
    place — but still set branch tracking so `git push` to the (mount-path)
    origin produces a useful error later instead of "no upstream configured"."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )
    mocker.patch("jailbee.git.get_remote_url", return_value=None)
    mocker.patch("jailbee.git.get_branch_tracking", return_value=None)

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    git_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and c.args[1][0] == "git"
    ]
    set_url_cmds = [cmd for cmd in git_cmds if "remote" in cmd and "set-url" in cmd]
    assert set_url_cmds == []

    branch_cfg_cmds = [
        cmd for cmd in git_cmds if "config" in cmd and any("branch.feat/x." in c for c in cmd)
    ]
    assert len(branch_cfg_cmds) == 2


# ---- new_container: clone_from="origin" / autofetch ----


def test_new_container_origin_mode_fetches_and_checkouts_commit(tmp_path, mocker):
    """Default-branch fallback in origin-mode: host-fetch + clone + checkout -B."""
    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=True)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch(
        "jailbee.lifecycle.rev_parse_remote",
        return_value="abc123def456",
    )

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    fetch.assert_called_once_with(cfg.repo_root, "origin", "dev")

    git_clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and c.args[1][0] == "git"
        and "clone" in c.args[1]
    ]
    assert len(git_clone_calls) == 1
    clone_cmd = git_clone_calls[0].args[1]
    assert "--branch" not in clone_cmd

    checkout_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "checkout" in c.args[1]
    ]
    assert len(checkout_calls) == 1
    checkout_cmd = checkout_calls[0].args[1]
    assert checkout_cmd[-3:] == ["-B", "feat/new", "abc123def456"]


def test_new_container_origin_mode_skips_fetch_when_autofetch_false(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=False)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch(
        "jailbee.lifecycle.rev_parse_remote",
        return_value="deadbeef",
    )

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    fetch.assert_not_called()


def test_new_container_origin_mode_errors_when_fetch_fails(tmp_path, mocker):
    from jailbee.git import GitFetchError

    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=True)
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    mocker.patch(
        "jailbee.lifecycle.fetch_remote_ref",
        side_effect=GitFetchError("fetch failed", stderr="fatal: connection refused"),
    )

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )

    with pytest.raises(ValueError, match="autofetch of 'origin/dev' failed"):
        new_container(cfg, incus, opts)

    incus.init.assert_not_called()


def test_new_container_retries_autofetch_when_accepted(tmp_path, mocker):
    """A confirmed retry re-runs only the host fetch, not container creation."""
    from jailbee.git import GitFetchError

    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=True)
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    fetch = mocker.patch(
        "jailbee.lifecycle.fetch_remote_ref",
        side_effect=[GitFetchError("fetch failed", stderr="fatal: connection refused"), None],
    )
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value=None)
    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")
    reported = mocker.patch("jailbee.retry.error")

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )

    # `rev_parse_origin` is stubbed to None, so creation still aborts — but on
    # the *next* guard, which is what proves the retried fetch succeeded.
    with pytest.raises(ValueError, match="not found"):
        new_container(cfg, incus, opts)

    assert fetch.call_count == 2
    incus.init.assert_not_called()
    # Reporting variant: reports exactly once (not on every retry attempt),
    # and reports the GitFetchError's stderr rather than its generic str().
    reported.assert_called_once_with("Fetching origin/dev failed: fatal: connection refused")


def test_new_container_autofetch_retry_is_not_offered_off_tty(tmp_path, mocker):
    from jailbee.git import GitFetchError

    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=True)
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    fetch = mocker.patch(
        "jailbee.lifecycle.fetch_remote_ref",
        side_effect=GitFetchError("fetch failed", stderr="fatal: connection refused"),
    )
    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=False)
    prompt = mocker.patch("builtins.input")

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )

    with pytest.raises(ValueError, match="autofetch of 'origin/dev' failed"):
        new_container(cfg, incus, opts)

    fetch.assert_called_once()
    prompt.assert_not_called()


def test_new_container_origin_mode_errors_when_origin_ref_missing(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=False)
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    mocker.patch(
        "jailbee.lifecycle.rev_parse_remote",
        return_value=None,
    )

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )

    with pytest.raises(ValueError, match="'refs/remotes/origin/dev' not found"):
        new_container(cfg, incus, opts)

    incus.init.assert_not_called()


def test_new_container_origin_mode_skipped_for_base_existing_locally(tmp_path, mocker):
    """When --base exists locally, use local-mode clone even with clone_from='origin'.

    Origin-mode would only kick in for a default-branch starting point (the
    historical clone_from='origin' case) or when the chosen base is missing
    from refs/heads. Here feat/y exists locally → local-mode wins.
    """
    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=True)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""

    def host_check(_repo_root, _remote, name):
        return name == "feat/y"

    def local_check(_repo_root, name):
        return name == "feat/y"

    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        side_effect=host_check,
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        side_effect=local_check,
    )
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    # Seed the base ref deterministically (base-seed now also calls
    # rev_parse_origin, so its invocation no longer proves origin-mode).
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="basesha")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base="feat/y",
    )
    new_container(cfg, incus, opts)

    fetch.assert_not_called()

    git_clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and c.args[1][0] == "git"
        and "clone" in c.args[1]
    ]
    assert len(git_clone_calls) == 1
    clone_cmd = git_clone_calls[0].args[1]
    # Local mode: `clone --branch feat/y` and no origin-mode `checkout -B`.
    assert clone_cmd[clone_cmd.index("--branch") + 1] == "feat/y"
    checkout_force_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "-B" in c.args[1]
    ]
    assert checkout_force_calls == []


def test_new_container_origin_mode_skipped_when_branch_not_default(tmp_path, mocker):
    """Existing non-default branch keeps local-mode clone even with clone_from='origin'."""
    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=True)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=True,
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    fetch.assert_not_called()
    git_clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and c.args[1][0] == "git"
        and "clone" in c.args[1]
    ]
    assert len(git_clone_calls) == 1
    clone_cmd = git_clone_calls[0].args[1]
    assert clone_cmd[clone_cmd.index("--branch") + 1] == "feat/x"


def test_new_container_origin_mode_announces_origin_in_branch_note(tmp_path, mocker, capsys):
    cfg = _cfg_for_new(tmp_path, clone_from="origin", autofetch=False)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=False,
    )
    mocker.patch(
        "jailbee.lifecycle.rev_parse_remote",
        return_value="abc123def456",
    )

    opts = NewContainerOptions(
        container_branch="feat/new",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    out = " ".join(capsys.readouterr().out.split())
    assert "new branch 'feat/new' off 'origin/dev'" in out


def test_new_container_origin_mode_when_base_is_origin_only(tmp_path, mocker):
    """`gie new X Y` where Y exists only as `refs/remotes/origin/Y` must use
    origin-mode clone (no `--branch`).

    Background: `git clone --shared --branch Y /local/repo` from a
    bind-mounted source only sees `refs/heads/*`. If the user has
    fetched origin/Y but never created a local branch, that clone fails
    with "Remote branch Y not found in upstream origin" even though
    `branch_exists_in_source` (which accepts origin refs) said yes.
    """
    cfg = _cfg_for_new(tmp_path, clone_from="local", autofetch=False)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""

    # Pre-flight: feat/y exists in source (via origin); feat/x does not.
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        side_effect=lambda _r, _remote, n: n == "feat/y",
    )
    # But refs/heads/feat/y is absent — only origin has it.
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=False,
    )
    mocker.patch(
        "jailbee.lifecycle.rev_parse_remote",
        return_value="deadbeefcafebabe",
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base="feat/y",
    )
    new_container(cfg, incus, opts)

    git_clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and c.args[1][0] == "git"
        and "clone" in c.args[1]
    ]
    assert len(git_clone_calls) == 1
    clone_cmd = git_clone_calls[0].args[1]
    assert "--branch" not in clone_cmd, f"origin-mode must omit --branch; got {clone_cmd!r}"

    checkout_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "checkout" in c.args[1]
    ]
    assert len(checkout_calls) == 1
    cmd = checkout_calls[0].args[1]
    assert "-B" in cmd
    assert "feat/x" in cmd
    assert "deadbeefcafebabe" in cmd


def test_new_container_announces_origin_base_when_base_only_in_origin(tmp_path, mocker, capsys):
    """Branch-note for origin-only base reads 'off 'origin/<base>''.

    Mirrors the existing announcement for the default-branch origin-mode
    case, surfaced now for the --base path so the user sees that the
    new container will start from the origin tip, not a local copy.
    """
    cfg = _cfg_for_new(tmp_path, clone_from="local", autofetch=False)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        side_effect=lambda _r, _remote, n: n == "feat/y",
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=False,
    )
    mocker.patch(
        "jailbee.lifecycle.rev_parse_remote",
        return_value="abc123",
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base="feat/y",
    )
    new_container(cfg, incus, opts)

    out = " ".join(capsys.readouterr().out.split())
    assert "new branch 'feat/x' off 'origin/feat/y'" in out


def test_new_container_origin_only_base_autofetches(tmp_path, mocker):
    """When base is origin-only and autofetch=True, fetch origin/<base> first.

    The autofetch happens regardless of `clone_from` because the only
    way to use an origin-only branch is via origin-mode.
    """
    cfg = _cfg_for_new(tmp_path, clone_from="local", autofetch=True)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""

    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        side_effect=lambda _r, _remote, n: n == "feat/y",
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=False,
    )
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch(
        "jailbee.lifecycle.rev_parse_remote",
        return_value="abc123",
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        base="feat/y",
    )
    new_container(cfg, incus, opts)

    fetch.assert_called_once_with(cfg.repo_root, "origin", "feat/y")


def test_new_container_attaches_host_source_device_per_container(tmp_path, mocker):
    """Repo-source RO bind lives on each container, not in the shared
    `<prefix>-binds` profile.

    Decouples the bind's `source` from "whichever repo applied profiles
    last," so two clones of the same upstream sharing a container_prefix
    don't trample each other's bind.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    matching = [c for c in incus.config_device_add.call_args_list if c.args[1] == "host-source"]
    assert len(matching) == 1
    call = matching[0]
    assert call.args[0] == "repo-feat-x"
    assert call.args[2] == "disk"
    props = call.args[3]
    assert props["source"] == str(cfg.repo_root)
    assert props["path"] == "/mnt/host-source"
    assert props["readonly"] == "true"


def test_new_container_attaches_host_source_in_mount_mode_too(tmp_path):
    """Mount-mode containers also get host-source for consistency, even
    though clone mode is the only path that actually uses it.

    Preserves the previous profile-level behavior where the bind was
    unconditionally present on every container.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="",
        name="repo-mountfoo",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mount=True,
    )
    new_container(cfg, incus, opts)

    matching = [c for c in incus.config_device_add.call_args_list if c.args[1] == "host-source"]
    assert len(matching) == 1
    props = matching[0].args[3]
    assert props["source"] == str(cfg.repo_root)
    assert props["path"] == "/mnt/host-source"
    assert props["readonly"] == "true"


def test_clone_uses_mnt_host_source_path(tmp_path, mocker):
    """The in-container `git clone` reads from /mnt/host-source."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=True,
    )
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    git_clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and c.args[1][0] == "git"
        and "clone" in c.args[1]
    ]
    assert len(git_clone_calls) == 1
    cmd = git_clone_calls[0].args[1]
    assert "/mnt/host-source" in cmd


def test_new_container_skips_clone_when_disabled(tmp_path):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    exec_calls = [c for c in incus.exec.call_args_list if c.args and "git" in c.args[1]]
    assert exec_calls == []


def test_new_container_attaches_runtime_devices_after_start(tmp_path, mocker):
    """GUI sockets must be attached after `incus start` (so logind
    has provisioned /run/user/<uid>) but before autostart runs.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    events: list[str] = []
    incus.start.side_effect = lambda _name: events.append("start")
    attach = mocker.patch(
        "jailbee.runtime_mounts.attach_runtime_devices",
        side_effect=lambda *a, **kw: events.append("attach") or True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    attach.assert_called_once()
    assert events == ["start", "attach"]


def test_new_container_runs_both_on_create_and_on_start(tmp_path, mocker):
    """`gie new` transitions the container into running state, so on_start
    steps must execute on this first launch — not only on subsequent
    `gie start`. Verifies both triggers fire, in order: ON_CREATE first,
    then ON_START.
    """
    from jailbee.autostart import AutostartTrigger

    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    triggers: list[AutostartTrigger] = []

    def _record(_cfg, _incus, _name, trigger, **_kw):
        triggers.append(trigger)

    mocker.patch(
        "jailbee.autostart.run_autostart",
        side_effect=_record,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=True,
    )
    new_container(cfg, incus, opts)

    assert triggers == [AutostartTrigger.ON_CREATE, AutostartTrigger.ON_START]


def test_new_container_skips_autostart_when_disabled(tmp_path, mocker):
    """`autostart=False` must skip both triggers, not just one."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    run_autostart = mocker.patch("jailbee.autostart.run_autostart")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    run_autostart.assert_not_called()


def test_new_container_applies_mirror_registries_from_cfg(tmp_path, mocker):
    """When the mirror is up, `gie new` must push the repo's extra_registries
    so rpardini starts caching them. cfg loaded from full_config.yaml carries
    the ECR host."""
    cfg = _cfg_for_new(tmp_path)
    mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch("jailbee.docker_daemon.apply_docker_proxy")
    apply_registries = mocker.patch("jailbee.registry.apply_mirror_registries")
    incus = MagicMock()
    incus.exists.return_value = False
    ca_path = tmp_path / "ca.crt"
    ca_path.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mirror_endpoint=("10.234.216.1", 3128),
        mirror_ca_path=ca_path,
    )
    new_container(cfg, incus, opts)

    apply_registries.assert_called_once_with(
        incus, ["803520778560.dkr.ecr.eu-north-1.amazonaws.com"]
    )


def test_new_container_skips_mirror_registries_when_no_endpoint(tmp_path, mocker):
    """Mirror disabled (mirror_endpoint=None) → no proxy to talk to. Skip."""
    cfg = _cfg_for_new(tmp_path)
    mocker.patch("jailbee.hosts.apply_hosts")
    apply_registries = mocker.patch("jailbee.registry.apply_mirror_registries")
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mirror_endpoint=None,
        mirror_ca_path=None,
    )
    new_container(cfg, incus, opts)

    apply_registries.assert_not_called()


def test_new_container_forwards_mirror_endpoint_to_run_autostart(tmp_path, mocker):
    """`new_container` must forward `opts.mirror_endpoint` to `run_autostart`
    so transient `strict → loose → strict` switches inside autostart steps
    keep the mirror pinned in /etc/hosts."""
    cfg = _cfg_for_new(tmp_path)
    captured_kwargs: list[dict] = []

    def _record(*_a, **kw):
        captured_kwargs.append(kw)

    mocker.patch(
        "jailbee.autostart.run_autostart",
        side_effect=_record,
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch("jailbee.docker_daemon.apply_docker_proxy")
    incus = MagicMock()
    incus.exists.return_value = False
    ca_path = tmp_path / "ca.crt"
    ca_path.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=True,
        mirror_endpoint=("10.234.216.1", 3128),
        mirror_ca_path=ca_path,
    )
    new_container(cfg, incus, opts)

    assert len(captured_kwargs) == 2  # on_create + on_start
    for kw in captured_kwargs:
        assert kw.get("mirror_endpoint") == ("10.234.216.1", 3128)


def test_new_container_sets_pr_label(tmp_path):
    """`new_container` persists `opts.pr` as the `user.jailbee.pr` label so
    `gie ls` can render the container's PR association."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=False,
        autostart=False,
        pr=1234,
    )
    new_container(cfg, incus, opts)

    calls = [c for c in incus.config_set.call_args_list if c.args[1] == "user.jailbee.pr"]
    assert len(calls) == 1
    assert calls[0].args[2] == "1234"


def test_new_container_no_pr_label_when_pr_none(tmp_path):
    """No `user.jailbee.pr` label is written for a non-PR container."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    calls = [c for c in incus.config_set.call_args_list if c.args[1] == "user.jailbee.pr"]
    assert calls == []


def test_new_container_persists_pr_label_before_autostart_failure(tmp_path, mocker):
    """Regression: the PR label must be persisted *before* autostart runs.

    A failing autostart step leaves the container running for debugging; it
    must keep its PR association so `gie ls` still shows it as a review
    container. Before the fix the label was set only after `new_container`
    returned, so an autostart failure dropped it silently.
    """
    from jailbee.autostart import AutostartStepError

    cfg = _cfg_for_new(tmp_path)
    mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch(
        "jailbee.autostart.run_autostart",
        side_effect=AutostartStepError(
            container="c", step_name="build", reason="boom", exit_code=1
        ),
    )
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=False,
        autostart=True,
        pr=1234,
    )
    with pytest.raises(AutostartStepError):
        new_container(cfg, incus, opts)

    calls = [c for c in incus.config_set.call_args_list if c.args[1] == "user.jailbee.pr"]
    assert len(calls) == 1
    assert calls[0].args[2] == "1234"


def test_new_container_pr_label_failure_is_non_fatal(tmp_path):
    """Setting `user.jailbee.pr` is best-effort: an Incus failure warns but does
    not abort an otherwise-successful creation (the label is display-only
    metadata, unlike the branch/mode labels)."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    def _fail_on_pr(name, key, value):
        if key == "user.jailbee.pr":
            raise RuntimeError("simulated incus failure")

    incus.config_set.side_effect = _fail_on_pr

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=False,
        autostart=False,
        pr=1234,
    )
    result = new_container(cfg, incus, opts)
    assert result  # container name returned; creation not aborted


def test_new_container_raises_if_exists(tmp_path):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = True

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
    )
    with pytest.raises(ValueError, match="already exists"):
        new_container(cfg, incus, opts)


def test_new_container_calls_apply_hosts_for_strict(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    apply = mocker.patch("jailbee.hosts.apply_hosts")
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    apply.assert_called_once_with(cfg, incus, "repo-feat-x", mirror_endpoint=None)


def test_new_container_forwards_mirror_endpoint_to_apply_hosts(tmp_path, mocker):
    """Strict containers need the mirror IP pinned because incusbr0's
    dnsmasq can't see the mirror on jailbee-loose."""
    cfg = _cfg_for_new(tmp_path)
    apply = mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch("jailbee.docker_daemon.apply_docker_proxy")
    incus = MagicMock()
    incus.exists.return_value = False
    ca_path = tmp_path / "ca.crt"
    ca_path.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mirror_endpoint=("10.234.216.1", 3128),
        mirror_ca_path=ca_path,
    )
    new_container(cfg, incus, opts)

    apply.assert_called_once_with(cfg, incus, "repo-feat-x", mirror_endpoint=("10.234.216.1", 3128))


def test_new_container_applies_hosts_after_start(tmp_path, mocker):
    """apply_hosts uses `incus exec`, which requires a running container."""
    cfg = _cfg_for_new(tmp_path)
    call_order: list[str] = []
    mocker.patch(
        "jailbee.hosts.apply_hosts",
        side_effect=lambda *_a, **_kw: call_order.append("apply_hosts"),
    )
    incus = MagicMock()
    incus.exists.return_value = False
    incus.start.side_effect = lambda *_a, **_kw: call_order.append("start")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    assert call_order.index("start") < call_order.index("apply_hosts")


def test_new_container_skips_apply_hosts_for_loose(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    apply = mocker.patch("jailbee.hosts.apply_hosts")
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="loose",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    apply.assert_not_called()


def test_new_container_persists_user_gie_mode_clone(tmp_path):
    """Every new container is tagged with user.jailbee.mode = clone."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    config_set_calls = [c.args for c in incus.config_set.call_args_list]
    assert ("repo-feat-x", "user.jailbee.mode", "clone") in config_set_calls


def test_new_container_mount_attaches_host_repo_rw_device(tmp_path):
    """Mount mode attaches host-repo-rw disk device targeting cfg.repo_root RW."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="",
        name="repo-mountfoo",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mount=True,
    )
    new_container(cfg, incus, opts)

    device_add_calls = incus.config_device_add.call_args_list
    matching = [c for c in device_add_calls if c.args[1] == "host-repo-rw"]
    assert len(matching) == 1
    call = matching[0]
    assert call.args[0] == "repo-mountfoo"
    assert call.args[2] == "disk"
    props = call.args[3]
    assert props["source"] == str(cfg.repo_root)
    assert props["path"] == f"/home/dev/{cfg.repo_root.name}"
    assert "readonly" not in props


def test_new_container_mount_sets_user_gie_mode_and_skips_branch(tmp_path):
    """Mount mode persists user.jailbee.mode=mount and does NOT set user.jailbee.branch."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="",
        name="repo-mountfoo",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mount=True,
    )
    new_container(cfg, incus, opts)

    config_set_calls = [c.args for c in incus.config_set.call_args_list]
    assert ("repo-mountfoo", "user.jailbee.mode", "mount") in config_set_calls
    assert not any(c[1] == "user.jailbee.branch" for c in config_set_calls)
    # The prompt env var is paired with user.jailbee.branch — mount mode
    # has no fixed branch (host's checkout floats), so don't set it.
    assert not any(c[1] == "environment.JAILBEE_BRANCH" for c in config_set_calls)


def test_new_container_mount_does_not_clone(tmp_path):
    """Mount mode must not invoke any `git clone` inside the container."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="",
        name="repo-mountfoo",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mount=True,
    )
    new_container(cfg, incus, opts)

    clone_calls = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "clone" in c.args[1]
    ]
    assert clone_calls == []


def test_new_container_mount_rejects_base(tmp_path):
    """`--base` is meaningless in mount mode; raise."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="",
        name="repo-mountfoo",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mount=True,
        base="main",
    )
    with pytest.raises(ValueError, match="base"):
        new_container(cfg, incus, opts)


def test_new_container_attaches_under_repo_host_mount_after_clone(tmp_path, mocker):
    """host_mounts targeting paths under /home/<user>/<repo>/ are attached
    as per-container devices AFTER the clone — never via the binds profile,
    which would pre-create the clone target and break `git clone`.
    """
    from jailbee.config import HostMount

    cfg = _cfg_for_new(tmp_path)
    src = tmp_path / "src-local"
    src.mkdir()
    cfg = cfg.model_copy(
        update={
            "host_mounts": [HostMount(host=src, container="/home/dev/repo/local", readonly=True)]
        }
    )
    object.__setattr__(cfg, "repo_root", tmp_path / "repo")
    object.__setattr__(cfg, "default_branch", "dev")
    object.__setattr__(cfg, "container_prefix", "repo")

    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    # The under-repo mount must be attached as a per-container device.
    matching = [c for c in incus.config_device_add.call_args_list if c.args[1] == "host-src-local"]
    assert len(matching) == 1
    call = matching[0]
    assert call.args[0] == "repo-feat-x"
    assert call.args[2] == "disk"
    props = call.args[3]
    assert props["source"] == str(src)
    assert props["path"] == "/home/dev/repo/local"
    assert props["readonly"] == "true"

    # And it must happen AFTER the `git clone` exec — otherwise the parent
    # /home/dev/repo/ gets pre-created and clone fails.
    method_calls = list(incus.method_calls)
    clone_idx = next(
        i
        for i, mc in enumerate(method_calls)
        if mc[0] == "exec"
        and len(mc.args) > 1
        and isinstance(mc.args[1], list)
        and "clone" in mc.args[1]
    )
    device_idx = next(
        i
        for i, mc in enumerate(method_calls)
        if mc[0] == "config_device_add" and mc.args[1] == "host-src-local"
    )
    assert device_idx > clone_idx


def test_new_container_does_not_attach_non_under_repo_host_mount(tmp_path, mocker):
    """host_mounts outside /home/<user>/<repo>/ are NOT re-attached per-
    container — they live in the binds profile.
    """
    from jailbee.config import HostMount

    cfg = _cfg_for_new(tmp_path)
    src = tmp_path / "src-elsewhere"
    src.mkdir()
    cfg = cfg.model_copy(
        update={
            "host_mounts": [HostMount(host=src, container="/home/dev/.elsewhere", readonly=True)]
        }
    )
    object.__setattr__(cfg, "repo_root", tmp_path / "repo")
    object.__setattr__(cfg, "default_branch", "dev")
    object.__setattr__(cfg, "container_prefix", "repo")

    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    # No device_add for this host mount.
    assert not any(
        c.args[1] == "host-src-elsewhere" for c in incus.config_device_add.call_args_list
    )


def test_new_container_mount_mode_attaches_under_repo_host_mount(tmp_path):
    """Mount mode also gets per-container under-repo bind mounts — they
    shadow whatever's at /home/<user>/<repo>/<sub> in the host repo bind.
    """
    from jailbee.config import HostMount

    cfg = _cfg_for_new(tmp_path)
    src = tmp_path / "src-local"
    src.mkdir()
    cfg = cfg.model_copy(
        update={
            "host_mounts": [HostMount(host=src, container="/home/dev/repo/local", readonly=True)]
        }
    )
    object.__setattr__(cfg, "repo_root", tmp_path / "repo")
    object.__setattr__(cfg, "default_branch", "dev")
    object.__setattr__(cfg, "container_prefix", "repo")

    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="",
        name="repo-mountfoo",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mount=True,
    )
    new_container(cfg, incus, opts)

    matching = [c for c in incus.config_device_add.call_args_list if c.args[1] == "host-src-local"]
    assert len(matching) == 1
    props = matching[0].args[3]
    assert props["source"] == str(src)
    assert props["path"] == "/home/dev/repo/local"
    assert props["readonly"] == "true"


def test_new_container_attaches_under_repo_shared_cache_after_clone(tmp_path, mocker):
    """An auto-added shared_cache whose container_path lives under the
    repo (e.g. jetbrains-idea) is attached as a per-container disk device
    AFTER `git clone`. Mirrors _attach_under_repo_host_mounts.
    """
    cfg = _cfg_for_new(tmp_path)
    # full_config.yaml already has jetbrains.enabled=true; share_idea
    # defaults to True via the JetbrainsConfig field.
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    matching = [
        c for c in incus.config_device_add.call_args_list if c.args[1] == "shared-jetbrains-idea"
    ]
    assert len(matching) == 1
    call = matching[0]
    assert call.args[0] == "repo-feat-x"
    assert call.args[2] == "disk"
    props = call.args[3]
    assert props["source"] == f"{cfg.shared_dir}/jetbrains-idea"
    assert props["path"] == "/home/dev/repo/.idea"
    # Shared caches are RW; no readonly key.
    assert "readonly" not in props

    # Attached AFTER `git clone`.
    method_calls = list(incus.method_calls)
    clone_idx = next(
        i
        for i, mc in enumerate(method_calls)
        if mc[0] == "exec"
        and len(mc.args) > 1
        and isinstance(mc.args[1], list)
        and "clone" in mc.args[1]
    )
    device_idx = next(
        i
        for i, mc in enumerate(method_calls)
        if mc[0] == "config_device_add" and mc.args[1] == "shared-jetbrains-idea"
    )
    assert device_idx > clone_idx


def test_new_container_does_not_attach_under_repo_shared_cache_in_mount_mode(tmp_path):
    """In --mount mode, the host's .idea wins. Skip the under-repo
    jetbrains-idea cache; the per-container host-repo-rw bind already
    covers /home/<user>/<container_prefix>/.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""

    opts = NewContainerOptions(
        container_branch="",
        name="repo-mountfoo",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mount=True,
    )
    new_container(cfg, incus, opts)

    assert not any(
        c.args[1] == "shared-jetbrains-idea" for c in incus.config_device_add.call_args_list
    )


def test_new_container_creates_missing_under_repo_shared_cache_source_dir(tmp_path, mocker):
    """If `<shared_dir>/jetbrains-idea` doesn't exist on the host
    (e.g. user upgraded gie without re-running init/apply), the
    lifecycle attach must mkdir it defensively before calling
    config_device_add. Otherwise Incus rejects the device with
    'Missing source path'.
    """
    cfg = _cfg_for_new(tmp_path)
    # Sanity: shared_dir is tmp-only, so jetbrains-idea does NOT exist.
    assert not (cfg.shared_dir / "jetbrains-idea").exists()

    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    # The source dir must exist after the attach helper has run.
    assert (cfg.shared_dir / "jetbrains-idea").is_dir()
    # And the device must still have been attached.
    assert any(c.args[1] == "shared-jetbrains-idea" for c in incus.config_device_add.call_args_list)


def test_new_container_does_not_attach_under_repo_shared_cache_when_share_idea_off(
    tmp_path, mocker
):
    """share_idea=False suppresses the cache at the config layer; no
    attach call must happen."""
    cfg = _cfg_for_new(tmp_path)
    cfg = cfg.model_copy(
        update={"jetbrains": cfg.jetbrains.model_copy(update={"share_idea": False})}
    )
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    assert not any(
        c.args[1] == "shared-jetbrains-idea" for c in incus.config_device_add.call_args_list
    )


# ---- new_container: clone_commit (PR heads) ----


def _clone_commit_opts(**overrides):
    defaults = dict(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
        clone_commit="beefcafe1234",
        base_branch_label="main",
    )
    defaults.update(overrides)
    return NewContainerOptions(**defaults)


def test_new_container_clone_commit_checks_out_that_commit(tmp_path, mocker):
    """`clone_commit` pins the clone to a raw SHA — no `--branch` resolution."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="basesha")

    new_container(cfg, incus, _clone_commit_opts())

    fetch.assert_not_called()

    clone_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "clone" in c.args[1]
    ]
    assert len(clone_cmds) == 1
    assert "--branch" not in clone_cmds[0]

    checkout_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "checkout" in c.args[1]
    ]
    assert len(checkout_cmds) == 1
    assert checkout_cmds[0][-3:] == ["-B", "feat/foo", "beefcafe1234"]


def test_new_container_clone_commit_ignores_a_same_named_host_branch(tmp_path, mocker):
    """Regression: a PR head must win over the host's local branch of that name.

    The host commonly has the PR's branch checked out — possibly stale, or
    carrying unpushed commits. The container must be built from the PR head
    that was fetched, never from whatever the host branch happens to point at.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="basesha")

    new_container(cfg, incus, _clone_commit_opts())

    checkout_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "checkout" in c.args[1]
    ]
    assert checkout_cmds[0][-3:] == ["-B", "feat/foo", "beefcafe1234"]
    clone_cmds = [
        c.args[1]
        for c in incus.exec.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], list) and "clone" in c.args[1]
    ]
    assert "--branch" not in clone_cmds[0]


def test_new_container_clone_commit_requires_clone(tmp_path):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    with pytest.raises(ValueError, match="clone"):
        new_container(cfg, incus, _clone_commit_opts(clone=False))

    incus.init.assert_not_called()


def test_new_container_clone_commit_rejects_base(tmp_path):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False

    with pytest.raises(ValueError, match="mutually exclusive"):
        new_container(cfg, incus, _clone_commit_opts(base="dev"))

    incus.init.assert_not_called()


def test_list_containers_reads_mode_from_user_gie_mode(make_cfg, tmp_path):
    """ContainerInfo.mode reflects user.jailbee.mode; absence → 'clone'."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-a",
            profiles=["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
            user_config={"user.jailbee.mode": "mount"},
        ),
        _container(
            name="myrepo-b",
            profiles=["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
        ),
    ]

    result = list_containers(cfg, incus)
    by_name = {c.name: c for c in result}
    assert by_name["myrepo-a"].mode == "mount"
    assert by_name["myrepo-b"].mode == "clone"


# ---- restart_container ----


def test_restart_container_detaches_then_restarts_then_attaches(tmp_path, mocker):
    """Detach must happen *before* `incus restart` so the four
    socket devices don't race with logind on the next boot. attach must
    happen *after* restart returns so logind has provisioned
    /run/user/<uid>.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-x", "status": "Running"}]

    events: list[str] = []
    incus.restart.side_effect = lambda _n: events.append("restart")
    detach = mocker.patch(
        "jailbee.runtime_mounts.detach_runtime_devices",
        side_effect=lambda *a, **kw: events.append("detach"),
    )
    attach = mocker.patch(
        "jailbee.runtime_mounts.attach_runtime_devices",
        side_effect=lambda *a, **kw: events.append("attach") or True,
    )

    restart_container(cfg, incus, "feat-x")

    detach.assert_called_once()
    attach.assert_called_once()
    assert events == ["detach", "restart", "attach"]


def test_restart_container_starts_when_already_stopped(tmp_path, mocker):
    """If the container is already stopped, `incus restart` would error out
    ("The instance is already stopped"). Fall back to `incus start` so the
    user-facing `gie restart` works as "ensure running + run autostart".
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "feat-x", "status": "Stopped"}]

    events: list[str] = []
    incus.restart.side_effect = lambda _n: events.append("restart")
    incus.start.side_effect = lambda _n: events.append("start")
    mocker.patch(
        "jailbee.runtime_mounts.detach_runtime_devices",
        side_effect=lambda *a, **kw: events.append("detach"),
    )
    mocker.patch(
        "jailbee.runtime_mounts.attach_runtime_devices",
        side_effect=lambda *a, **kw: events.append("attach") or True,
    )

    restart_container(cfg, incus, "feat-x")

    incus.restart.assert_not_called()
    incus.start.assert_called_once_with("feat-x")
    assert events == ["detach", "start", "attach"]


# ---- destroy_container ----


def _cfg_for_destroy(tmp_path):
    cfg = load_config(FIXTURES / "full_config.yaml")
    return cfg.model_copy(update={"shared_dir": tmp_path / "shared"})


def test_destroy_container_stops_then_deletes_when_running(tmp_path, mocker):
    cfg = _cfg_for_destroy(tmp_path)
    mocker.patch("jailbee.chrome_pool.release")
    incus = MagicMock()
    incus.exists.return_value = True
    incus.list_containers.return_value = [
        {
            "name": "x",
            "status": "Running",
            "profiles": ["default", "gisgro-base", "gisgro-binds", "gisgro-net-strict"],
        }
    ]
    destroy_container(cfg, incus, "x", force=True)
    incus.stop.assert_called_once_with("x", force=True)
    incus.delete.assert_called_once_with("x", force=True)


def test_destroy_container_just_deletes_when_stopped(tmp_path, mocker):
    cfg = _cfg_for_destroy(tmp_path)
    mocker.patch("jailbee.chrome_pool.release")
    incus = MagicMock()
    incus.exists.return_value = True
    incus.list_containers.return_value = [
        {
            "name": "x",
            "status": "Stopped",
            "profiles": ["default", "gisgro-base", "gisgro-binds", "gisgro-net-strict"],
        }
    ]
    destroy_container(cfg, incus, "x", force=True)
    incus.stop.assert_not_called()
    incus.delete.assert_called_once_with("x", force=True)


def test_destroy_raises_if_not_exists(tmp_path, mocker):
    cfg = _cfg_for_destroy(tmp_path)
    mocker.patch("jailbee.chrome_pool.release")
    incus = MagicMock()
    incus.exists.return_value = False
    with pytest.raises(ValueError, match="does not exist"):
        destroy_container(cfg, incus, "missing", force=True)


def test_destroy_container_calls_chrome_pool_release(tmp_path, mocker):
    """Destroy must release the container's Chrome profile slot
    before deleting the container, so the slot is freed for reuse.
    """
    cfg = _cfg_for_destroy(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = True
    incus.list_containers.return_value = [
        {
            "name": "x",
            "status": "Stopped",
            "profiles": ["default", "gisgro-base", "gisgro-binds", "gisgro-net-strict"],
        }
    ]
    release = mocker.patch("jailbee.chrome_pool.release")

    destroy_container(cfg, incus, "x", force=True)

    release.assert_called_once_with(cfg, incus, "x")
    incus.delete.assert_called_once_with("x", force=True)


def test_destroy_container_cleans_gie_refs(tmp_path, mocker):
    """Destroy must clean refs/jailbee/<short>/* on the host so the
    refspecs created by `gie git fetch` don't accumulate after containers are
    destroyed.
    """
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    incus.exists.return_value = True
    incus.list_containers.return_value = [{"name": full, "status": "Stopped", "profiles": []}]
    mocker.patch("jailbee.chrome_pool.release")
    mock_list = mocker.patch(
        "jailbee.git.list_refs",
        return_value=[
            "refs/jailbee/feat-foo/feat/foo",
            "refs/jailbee/feat-foo/feat/bar",
        ],
    )
    mock_delete = mocker.patch("jailbee.git.delete_ref")

    destroy_container(cfg, incus, full, force=True)

    mock_list.assert_called_once_with(cfg.repo_root, "refs/jailbee/feat-foo/")
    assert mock_delete.call_count == 2
    delete_refs = [c.args[1] for c in mock_delete.call_args_list]
    assert "refs/jailbee/feat-foo/feat/foo" in delete_refs
    assert "refs/jailbee/feat-foo/feat/bar" in delete_refs
    incus.delete.assert_called_once()


def test_destroy_container_succeeds_when_ref_cleanup_fails(tmp_path, mocker):
    """Ref cleanup is best-effort — a git failure must not block destroy."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    incus.exists.return_value = True
    incus.list_containers.return_value = [{"name": full, "status": "Stopped", "profiles": []}]
    mocker.patch("jailbee.chrome_pool.release")
    mocker.patch("jailbee.git.list_refs", side_effect=RuntimeError("git broken"))

    destroy_container(cfg, incus, full, force=True)
    incus.delete.assert_called_once()


def test_destroy_container_prunes_background_op(make_cfg, tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.exists.return_value = True
    incus.list_containers.return_value = []
    mocker.patch("jailbee.chrome_pool.release")

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import destroy_container

    name = f"{cfg.container_prefix}-feat-foo"
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="feat/foo",
            pid=1,
            log_path="/l",
            now=datetime.now(UTC),
        )

    destroy_container(cfg, incus, name, force=True)

    with Session(get_engine()) as s:
        assert background.list_jobs(s, cfg.container_prefix) == {}


def test_destroy_prunes_submodule_refs(tmp_path, mocker):
    """destroy_container must call prune_host_submodule_refs inside the
    best-effort try block so refs/jailbee-sub/<short>/* are cleaned up from
    each host submodule repo after container deletion."""
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = True
    incus.list_containers.return_value = [
        {"name": f"{cfg.container_prefix}-feat-x", "status": "Stopped", "profiles": []}
    ]
    mocker.patch("jailbee.chrome_pool.release")
    mocker.patch("jailbee.git.list_refs", return_value=[])
    prune = mocker.patch("jailbee.submodules.prune_host_submodule_refs")

    from jailbee.lifecycle import destroy_container

    destroy_container(cfg, incus, f"{cfg.container_prefix}-feat-x", force=True)

    prune.assert_called_once()


def test_destroy_container_fires_phase_callbacks(make_cfg, tmp_path, mocker):
    from jailbee.incus import Incus
    from jailbee.lifecycle import destroy_container

    cfg = make_cfg(tmp_path / "repo")
    incus = mocker.MagicMock(spec=Incus)
    incus.exists.return_value = True
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    mocker.patch("jailbee.chrome_pool.release")
    mocker.patch("jailbee.git.list_refs", return_value=[])

    phases: list[str] = []
    destroy_container(cfg, incus, "c", force=True, on_phase=phases.append)

    assert phases == ["stopping", "deleting"]
    incus.stop.assert_called_once()
    incus.delete.assert_called_once()


def test_destroy_container_stopped_skips_stopping_phase(make_cfg, tmp_path, mocker):
    from jailbee.incus import Incus
    from jailbee.lifecycle import destroy_container

    cfg = make_cfg(tmp_path / "repo")
    incus = mocker.MagicMock(spec=Incus)
    incus.exists.return_value = True
    incus.list_containers.return_value = [{"name": "c", "status": "Stopped"}]
    mocker.patch("jailbee.chrome_pool.release")
    mocker.patch("jailbee.git.list_refs", return_value=[])

    phases: list[str] = []
    destroy_container(cfg, incus, "c", force=True, on_phase=phases.append)

    assert phases == ["deleting"]  # not running -> no stop, no 'stopping'
    incus.stop.assert_not_called()


# ---- switch_network ----


def test_switch_network_replaces_only_net_profile(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-feat-foo",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
        }
    ]
    switch_network(cfg, incus, "myrepo-feat-foo", "loose")
    incus.profile_assign.assert_called_once_with(
        "myrepo-feat-foo",
        ["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"],
    )


def test_switch_network_rejects_unknown_mode(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
        }
    ]
    with pytest.raises(ValueError, match="Unknown network"):
        switch_network(cfg, incus, "myrepo-x", "wat")


def test_switch_network_raises_if_no_net_profile_attached(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds"],
        }
    ]
    with pytest.raises(ValueError, match="no network profile"):
        switch_network(cfg, incus, "myrepo-x", "loose")


def test_switch_network_calls_apply_hosts_when_switching_to_strict(
    make_cfg,
    tmp_path,
    mocker,
):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    apply = mocker.patch("jailbee.hosts.apply_hosts")
    clear = mocker.patch("jailbee.hosts.clear_hosts")
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"],
        }
    ]
    switch_network(cfg, incus, "myrepo-x", "strict")

    apply.assert_called_once_with(cfg, incus, "myrepo-x", mirror_endpoint=None)
    clear.assert_not_called()


def test_switch_network_forwards_mirror_endpoint_when_switching_to_strict(
    make_cfg,
    tmp_path,
    mocker,
):
    """When switching to strict, mirror_endpoint must be forwarded so
    /etc/hosts keeps the jailbee-registry-mirror.incus row."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    apply = mocker.patch("jailbee.hosts.apply_hosts")
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-loose"],
        }
    ]
    switch_network(cfg, incus, "myrepo-x", "strict", mirror_endpoint=("10.42.0.7", 3128))

    apply.assert_called_once_with(cfg, incus, "myrepo-x", mirror_endpoint=("10.42.0.7", 3128))


def test_switch_network_calls_clear_hosts_when_switching_to_loose(
    make_cfg,
    tmp_path,
    mocker,
):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    apply = mocker.patch("jailbee.hosts.apply_hosts")
    clear = mocker.patch("jailbee.hosts.clear_hosts")
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": "myrepo-x",
            "status": "Running",
            "profiles": ["default", "myrepo-base", "myrepo-binds", "myrepo-net-strict"],
        }
    ]
    switch_network(cfg, incus, "myrepo-x", "loose")

    clear.assert_called_once_with(cfg, incus, "myrepo-x")
    apply.assert_not_called()


# ---- resolve_container_name ----


def test_resolve_container_name_full_match_wins(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()

    def exists(n):
        return n == "myrepo-feat-x"

    incus.exists.side_effect = exists
    assert resolve_container_name(cfg, incus, "myrepo-feat-x") == "myrepo-feat-x"


def test_resolve_container_name_prefix_fallback(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()

    def exists(n):
        return n == "myrepo-feat-x"

    incus.exists.side_effect = exists
    assert resolve_container_name(cfg, incus, "feat-x") == "myrepo-feat-x"


def test_resolve_container_name_full_match_other_repo(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()

    def exists(n):
        return n == "other-feat-x"

    incus.exists.side_effect = exists
    assert resolve_container_name(cfg, incus, "other-feat-x") == "other-feat-x"


def test_resolve_container_name_not_found_lists_attempts(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.exists.return_value = False
    with pytest.raises(ValueError, match="myrepo-feat-x"):
        resolve_container_name(cfg, incus, "feat-x")


# ---- new_container HTTPS_PROXY wiring ----


def _write_ca(tmp_path):
    """Write a fake CA PEM and return its path."""
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nfake-cert\n-----END CERTIFICATE-----\n")
    return ca


def test_new_container_applies_docker_proxy_for_strict(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    apply = mocker.patch("jailbee.docker_daemon.apply_docker_proxy")
    incus = MagicMock()
    incus.exists.return_value = False
    ca_path = _write_ca(tmp_path)

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mirror_endpoint=("10.234.216.1", 3128),
        mirror_ca_path=ca_path,
    )
    new_container(cfg, incus, opts)

    apply.assert_called_once()
    # Positional: (incus, name); kwargs: ca_cert_pem, port
    assert apply.call_args.args[0] is incus
    assert apply.call_args.args[1] == "repo-feat-x"
    assert apply.call_args.kwargs["ca_cert_pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert apply.call_args.kwargs["port"] == 3128


def test_new_container_applies_docker_proxy_for_loose(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    apply = mocker.patch("jailbee.docker_daemon.apply_docker_proxy")
    incus = MagicMock()
    incus.exists.return_value = False
    ca_path = _write_ca(tmp_path)

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="loose",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mirror_endpoint=("10.234.216.1", 3128),
        mirror_ca_path=ca_path,
    )
    new_container(cfg, incus, opts)

    apply.assert_called_once()


def test_new_container_skips_docker_proxy_when_mirror_endpoint_is_none(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    apply = mocker.patch("jailbee.docker_daemon.apply_docker_proxy")
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mirror_endpoint=None,
        mirror_ca_path=None,
    )
    new_container(cfg, incus, opts)

    apply.assert_not_called()


def test_new_container_skips_docker_proxy_when_ca_path_is_none(tmp_path, mocker):
    """Mirror endpoint computed but CA file unreadable → skip rather than crash."""
    cfg = _cfg_for_new(tmp_path)
    apply = mocker.patch("jailbee.docker_daemon.apply_docker_proxy")
    incus = MagicMock()
    incus.exists.return_value = False

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mirror_endpoint=("10.234.216.1", 3128),
        mirror_ca_path=None,
    )
    new_container(cfg, incus, opts)

    apply.assert_not_called()


def test_new_container_applies_docker_proxy_after_start(tmp_path, mocker):
    """CA cert + dockerd restart need the container running."""
    cfg = _cfg_for_new(tmp_path)
    call_order: list[str] = []
    mocker.patch(
        "jailbee.docker_daemon.apply_docker_proxy",
        side_effect=lambda *_a, **_kw: call_order.append("proxy"),
    )
    incus = MagicMock()
    incus.exists.return_value = False
    incus.start.side_effect = lambda *_a, **_kw: call_order.append("start")
    ca_path = _write_ca(tmp_path)

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mirror_endpoint=("10.234.216.1", 3128),
        mirror_ca_path=ca_path,
    )
    new_container(cfg, incus, opts)

    assert call_order.index("start") < call_order.index("proxy")


# ---- resolve_container_for_interactive ----


def test_resolver_returns_resolved_name_when_given(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.exists.side_effect = lambda n: n == "myrepo-feat-foo"

    picker = MagicMock()
    is_interactive = MagicMock(return_value=True)

    result = resolve_container_for_interactive(
        cfg,
        incus,
        "feat-foo",
        picker=picker,
        is_interactive=is_interactive,
    )

    assert result == "myrepo-feat-foo"
    picker.assert_not_called()
    is_interactive.assert_not_called()


def test_resolver_errors_when_no_containers(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.list_containers.return_value = []  # zero
    picker = MagicMock()

    with pytest.raises(ValueError, match="no managed containers"):
        resolve_container_for_interactive(
            cfg,
            incus,
            None,
            picker=picker,
            is_interactive=lambda: True,
        )

    picker.assert_not_called()


def test_resolver_auto_picks_single_container(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.list_containers.return_value = [_container(name="myrepo-feat-only")]
    picker = MagicMock()
    is_interactive = MagicMock(return_value=True)

    result = resolve_container_for_interactive(
        cfg,
        incus,
        None,
        picker=picker,
        is_interactive=is_interactive,
    )

    assert result == "myrepo-feat-only"
    picker.assert_not_called()
    is_interactive.assert_not_called()


def test_resolver_invokes_picker_when_multiple_and_interactive(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-a"),
        _container(name="myrepo-feat-b"),
    ]
    picker = MagicMock(return_value="myrepo-feat-b")
    is_interactive = MagicMock(return_value=True)

    result = resolve_container_for_interactive(
        cfg,
        incus,
        None,
        picker=picker,
        is_interactive=is_interactive,
    )

    assert result == "myrepo-feat-b"
    is_interactive.assert_called_once_with()
    picker.assert_called_once()
    (passed_containers,) = picker.call_args.args
    assert [c.name for c in passed_containers] == ["myrepo-feat-a", "myrepo-feat-b"]


def test_resolver_errors_when_multiple_and_non_interactive(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-a"),
        _container(name="myrepo-feat-b"),
    ]
    picker = MagicMock()
    is_interactive = MagicMock(return_value=False)

    with pytest.raises(ValueError, match=r"multiple containers.*specify <name>"):
        resolve_container_for_interactive(
            cfg,
            incus,
            None,
            picker=picker,
            is_interactive=is_interactive,
        )

    picker.assert_not_called()


def test_resolver_raises_when_picker_cancelled(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-a"),
        _container(name="myrepo-feat-b"),
    ]
    picker = MagicMock(return_value=None)  # Ctrl+C

    with pytest.raises(ValueError, match="cancelled"):
        resolve_container_for_interactive(
            cfg,
            incus,
            None,
            picker=picker,
            is_interactive=lambda: True,
        )


def test_resolver_with_background_resolves_op_when_no_container(make_cfg, tmp_path, monkeypatch):
    """A named in-flight op resolves even though the container doesn't exist yet."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.exists.return_value = False  # container not created yet

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=f"{cfg.container_prefix}-feat-bg",
            container_prefix=cfg.container_prefix,
            branch="feat/bg",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )

    result = resolve_container_for_interactive(cfg, incus, "feat-bg", with_background=True)
    assert result == f"{cfg.container_prefix}-feat-bg"


def test_resolver_without_background_still_raises_for_missing(make_cfg, tmp_path, monkeypatch):
    """With the flag off, a missing container raises as before (no DB lookup)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.exists.return_value = False

    with pytest.raises(ValueError, match="no such container"):
        resolve_container_for_interactive(cfg, incus, "feat-bg")


def test_resolver_with_background_raises_when_neither_exists(make_cfg, tmp_path, monkeypatch):
    """Flag on but no container and no op → original ValueError re-raised."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.exists.return_value = False

    with pytest.raises(ValueError, match="no such container"):
        resolve_container_for_interactive(cfg, incus, "nonexistent", with_background=True)


def test_stdin_is_interactive_respects_env_and_tty(monkeypatch):
    from jailbee.lifecycle import _stdin_is_interactive

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.delenv("JAILBEE_NONINTERACTIVE", raising=False)
    assert _stdin_is_interactive() is True

    monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    assert _stdin_is_interactive() is False

    monkeypatch.delenv("JAILBEE_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _stdin_is_interactive() is False


# ---- resolve_container_for_interactive_detailed (auto_selected reporting) ----


def test_detailed_resolver_explicit_name_is_not_auto_selected(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.exists.side_effect = lambda n: n == "myrepo-feat-foo"

    result = resolve_container_for_interactive_detailed(
        cfg,
        incus,
        "feat-foo",
        picker=MagicMock(),
        is_interactive=MagicMock(return_value=True),
    )

    assert result == ResolvedContainer(name="myrepo-feat-foo", auto_selected=False)


def test_detailed_resolver_single_container_is_auto_selected(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.list_containers.return_value = [_container(name="myrepo-feat-only")]
    picker = MagicMock()

    result = resolve_container_for_interactive_detailed(
        cfg,
        incus,
        None,
        picker=picker,
        is_interactive=MagicMock(return_value=True),
    )

    assert result == ResolvedContainer(name="myrepo-feat-only", auto_selected=True)
    picker.assert_not_called()


def test_detailed_resolver_picker_choice_is_not_auto_selected(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(name="myrepo-feat-a"),
        _container(name="myrepo-feat-b"),
    ]

    result = resolve_container_for_interactive_detailed(
        cfg,
        incus,
        None,
        picker=MagicMock(return_value="myrepo-feat-b"),
        is_interactive=MagicMock(return_value=True),
    )

    assert result == ResolvedContainer(name="myrepo-feat-b", auto_selected=False)


def test_plain_resolver_still_returns_a_bare_name(make_cfg, tmp_path):
    """The 18 existing call sites must keep getting a str, not a dataclass."""
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    incus = MagicMock()
    incus.list_containers.return_value = [_container(name="myrepo-feat-only")]

    result = resolve_container_for_interactive(
        cfg,
        incus,
        None,
        picker=MagicMock(),
        is_interactive=MagicMock(return_value=True),
    )

    assert result == "myrepo-feat-only"


# ---- short_name ----


def test_short_name_strips_own_prefix(make_cfg, tmp_path):
    from jailbee.lifecycle import short_name

    cfg = make_cfg(tmp_path)
    full = f"{cfg.container_prefix}-feat-foo"
    assert short_name(cfg, full) == "feat-foo"


def test_short_name_returns_name_without_prefix_unchanged(make_cfg, tmp_path):
    from jailbee.lifecycle import short_name

    cfg = make_cfg(tmp_path)
    assert short_name(cfg, "other-repo-feat-foo") == "other-repo-feat-foo"


def test_short_name_only_strips_leading_match(make_cfg, tmp_path):
    from jailbee.lifecycle import short_name

    cfg = make_cfg(tmp_path)
    embedded = f"foo-{cfg.container_prefix}-bar"
    assert short_name(cfg, embedded) == embedded


# ---- agent install/update integration in new_container ----


def _new_opts(**overrides):
    base = dict(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=False,
    )
    base.update(overrides)
    return NewContainerOptions(**base)


def _patch_new_container_deps(mocker):
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=False)
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="abc1234")
    mocker.patch("jailbee.lifecycle.fetch_remote_ref")


def test_new_container_calls_ensure_agents(make_cfg, tmp_path, mocker):
    """`new_container` delegates agent install/update to `agents.ensure_agents`,
    passing the in-container repo dir and the run's mirror endpoint.

    The install-vs-update dispatch itself (per-agent enable/auto_update/
    network logic) is unit-tested in tests/test_agents.py; this only pins
    the integration boundary — that `new_container` calls `ensure_agents`
    with the right container/repo_dir/mirror_endpoint, at all.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    cfg = with_agent(cfg, "claude", enabled=True)
    incus = MagicMock()
    incus.exists.return_value = False
    _patch_new_container_deps(mocker)
    ensure_agents = mocker.patch("jailbee.agents.ensure_agents")

    opts = _new_opts(mirror_endpoint=("10.0.0.1", 5000))
    new_container(cfg, incus, opts)

    ensure_agents.assert_called_once()
    call = ensure_agents.call_args
    assert call.args[0] is cfg
    assert call.args[1] is incus
    assert call.args[3] == f"/home/{CONTAINER_USERNAME}/{cfg.container_prefix}"
    assert call.kwargs["mirror_endpoint"] == ("10.0.0.1", 5000)


def test_new_container_ensure_agents_runs_before_autostart(make_cfg, tmp_path, mocker):
    """Ordering pin: agents are installed/updated before autostart execs
    them (see the call site's comment for why mounts/network order it)."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False
    _patch_new_container_deps(mocker)
    calls: list[str] = []
    mocker.patch(
        "jailbee.agents.ensure_agents", side_effect=lambda *a, **kw: calls.append("agents")
    )
    mocker.patch(
        "jailbee.autostart.run_autostart",
        side_effect=lambda *a, **kw: calls.append("autostart"),
    )

    new_container(cfg, incus, _new_opts(autostart=True))

    assert calls[0] == "agents"
    assert "autostart" in calls[1:]


def test_new_container_syncs_gie_skills(make_cfg, tmp_path, mocker):
    """claude.enabled → gie skills are synced into the shared dir during new."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    cfg = with_agent(cfg, "claude", enabled=True)
    incus = MagicMock()
    incus.exists.return_value = False
    _patch_new_container_deps(mocker)
    sync = mocker.patch("jailbee.claude_skills.sync_jailbee_skills")

    new_container(cfg, incus, _new_opts())

    sync.assert_called_once_with(cfg)


def test_new_container_invokes_on_phase_in_order(make_cfg, tmp_path, mocker):
    """on_phase fires at creating -> cloning -> autostart boundaries."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=False)
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="abc1234")
    mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    # Autostart runs against a MagicMock incus; stub it so the test stays a
    # pure callback-ordering check.
    mocker.patch("jailbee.autostart.run_autostart")

    phases: list[str] = []
    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=True,
    )
    new_container(cfg, incus, opts, on_phase=phases.append)

    assert phases == ["creating", "cloning", "autostart"]


def test_new_container_without_on_phase_is_unchanged(make_cfg, tmp_path, mocker):
    """The synchronous path (on_phase=None) must not error."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=False)
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="abc1234")
    mocker.patch("jailbee.lifecycle.fetch_remote_ref")

    opts = NewContainerOptions(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=False,
    )
    # Must not raise.
    new_container(cfg, incus, opts)


def test_list_containers_attaches_job_phase_to_existing(make_cfg, tmp_path, monkeypatch):
    """A running container with a matching BackgroundJob row gets job_phase set."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": f"{cfg.container_prefix}-feat-foo",
            "status": "Running",
            "profiles": ["default", f"{cfg.container_prefix}-base"],
            "config": {},
            "state": None,
        }
    ]
    incus.config_get.return_value = None

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=f"{cfg.container_prefix}-feat-foo",
            container_prefix=cfg.container_prefix,
            branch="feat/foo",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )
        background.set_phase(
            s, f"{cfg.container_prefix}-feat-foo", background.PHASE_CLONING, now=datetime.now(UTC)
        )

    out = list_containers(cfg, incus, with_background=True)
    assert len(out) == 1
    assert out[0].job_phase == "cloning"
    assert out[0].job_pid == 4242


def test_list_containers_synthesises_row_for_container_less_op(make_cfg, tmp_path, monkeypatch):
    """A BackgroundJob with no live container shows as a synthetic row."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = []

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=f"{cfg.container_prefix}-feat-pre",
            container_prefix=cfg.container_prefix,
            branch="feat/pre",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )

    out = list_containers(cfg, incus, with_background=True)
    assert len(out) == 1
    assert out[0].name == f"{cfg.container_prefix}-feat-pre"
    assert out[0].state == "—"
    assert out[0].job_phase == "starting"


def test_list_containers_attaches_job_error_to_existing(make_cfg, tmp_path, monkeypatch):
    """A failed job whose container exists carries error_msg onto ContainerInfo."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": f"{cfg.container_prefix}-feat-foo",
            "status": "Running",
            "profiles": ["default", f"{cfg.container_prefix}-base"],
            "config": {},
            "state": None,
        }
    ]
    incus.config_get.return_value = None

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    name = f"{cfg.container_prefix}-feat-foo"
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="feat/foo",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )
        background.fail_job(s, name, "autostart step 'deps' failed", now=datetime.now(UTC))

    out = list_containers(cfg, incus, with_background=True)
    assert len(out) == 1
    assert out[0].job_phase == "failed"
    assert out[0].job_error == "autostart step 'deps' failed"


def test_list_containers_synthetic_row_carries_job_error(make_cfg, tmp_path, monkeypatch):
    """A failed job with no container still surfaces its error message."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = []

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    name = f"{cfg.container_prefix}-ghost"
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix=cfg.container_prefix,
            branch="feat/ghost",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )
        background.fail_job(s, name, "base image missing", now=datetime.now(UTC))

    out = list_containers(cfg, incus, with_background=True)
    assert len(out) == 1
    assert out[0].state == "—"
    assert out[0].job_phase == "failed"
    assert out[0].job_error == "base image missing"


def test_list_containers_without_with_background_skips_db(make_cfg, tmp_path):
    """Default callers don't touch the DB (job_phase stays None)."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": f"{cfg.container_prefix}-feat-foo",
            "status": "Running",
            "profiles": ["default", f"{cfg.container_prefix}-base"],
            "config": {},
            "state": None,
        }
    ]
    incus.config_get.return_value = None

    out = list_containers(cfg, incus)
    assert out[0].job_phase is None


# ---- share_local (.local) auto-mount ----


def _share_local_device_add_calls(incus):
    return [c for c in incus.config_device_add.call_args_list if c.args[1] == "share-local"]


def test_new_container_attaches_share_local_rw_and_excludes(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    (cfg.repo_root / ".local").mkdir()
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )
    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )

    new_container(cfg, incus, opts)

    calls = _share_local_device_add_calls(incus)
    assert len(calls) == 1
    _name, device, dev_type, props = calls[0].args
    assert device == "share-local"
    assert dev_type == "disk"
    assert props["source"] == str(cfg.repo_root / ".local")
    assert props["path"] == f"/home/dev/{cfg.repo_root.name}/.local"
    assert "readonly" not in props  # RW

    exclude_calls = [
        c
        for c in incus.exec.call_args_list
        if any(".git/info/exclude" in str(a) for a in c.args[1])
    ]
    assert exclude_calls, "expected a .git/info/exclude write"


def test_new_container_skips_share_local_when_dir_absent(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)  # no .local dir created
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )
    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )

    new_container(cfg, incus, opts)

    assert _share_local_device_add_calls(incus) == []


def test_new_container_skips_share_local_when_disabled(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    (cfg.repo_root / ".local").mkdir()
    cfg = cfg.model_copy(update={"share_local": False})
    # model_copy drops computed attrs; restore the ones new_container needs.
    object.__setattr__(cfg, "repo_root", tmp_path / "repo")
    object.__setattr__(cfg, "default_branch", "dev")
    object.__setattr__(cfg, "container_prefix", "repo")
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_locally",
        return_value=True,
    )
    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )

    new_container(cfg, incus, opts)

    assert _share_local_device_add_calls(incus) == []


def test_new_container_mount_mode_skips_share_local(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    (cfg.repo_root / ".local").mkdir()
    incus = MagicMock()
    incus.exists.return_value = False
    opts = NewContainerOptions(
        container_branch="",
        name=f"{cfg.container_prefix}-mountsmoke",
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=False,
        autostart=False,
        mount=True,
    )

    new_container(cfg, incus, opts)

    assert _share_local_device_add_calls(incus) == []


def test_lookup_background_job_matches_short_name(make_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import lookup_background_job

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=f"{cfg.container_prefix}-feat-foo",
            container_prefix=cfg.container_prefix,
            branch="feat/foo",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )

    # short name, full name, and the exact stored name all resolve
    assert (
        lookup_background_job(cfg, "feat-foo").container_name == f"{cfg.container_prefix}-feat-foo"
    )
    assert (
        lookup_background_job(cfg, f"{cfg.container_prefix}-feat-foo").container_name
        == f"{cfg.container_prefix}-feat-foo"
    )
    # a miss returns None
    assert lookup_background_job(cfg, "nope") is None


def test_wait_for_background_ready_returns_when_row_cleared(make_cfg, tmp_path, monkeypatch):
    """Wait returns once the job row is deleted; phase changes fire on_phase.

    Models a ``--no-autostart`` create: the worker goes cloning -> done without
    ever reaching the autostart phase, so the only ready signal is the row
    being deleted.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    full = f"{cfg.container_prefix}-feat-w"

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import wait_for_background_ready

    engine = get_engine()
    with Session(engine) as s:
        background.start_job(
            s,
            container_name=full,
            container_prefix=cfg.container_prefix,
            branch="feat/w",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )
        background.set_phase(s, full, background.PHASE_CLONING, now=datetime.now(UTC))

    monkeypatch.setattr(background, "worker_alive", lambda _pid: True)

    # The one fake sleep deletes the row, simulating the worker finishing.
    def fake_sleep(_interval):
        with Session(engine) as s:
            background.delete_job(s, full)

    seen: list[str] = []
    wait_for_background_ready(cfg, full, sleep=fake_sleep, on_phase=seen.append)
    assert seen == [background.PHASE_CLONING]


def test_wait_for_background_ready_returns_early_at_autostart(make_cfg, tmp_path, monkeypatch):
    """A create op that reaches autostart is attachable: wait returns before
    the op finishes, because the container is already started by then."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    full = f"{cfg.container_prefix}-feat-a"

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import BackgroundJob
    from jailbee.lifecycle import wait_for_background_ready

    engine = get_engine()
    with Session(engine) as s:
        background.start_job(
            s,
            container_name=full,
            container_prefix=cfg.container_prefix,
            branch="feat/a",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )
        background.set_phase(s, full, background.PHASE_CLONING, now=datetime.now(UTC))

    monkeypatch.setattr(background, "worker_alive", lambda _pid: True)

    # Only one transition (cloning -> autostart) is queued. The worker never
    # deletes the row here, so if the wait did NOT return at autostart the next
    # sleep would StopIteration and the test would fail loudly.
    transitions = iter([background.PHASE_AUTOSTART])

    def fake_sleep(_interval):
        with Session(engine) as s:
            background.set_phase(s, full, next(transitions), now=datetime.now(UTC))

    seen: list[str] = []
    wait_for_background_ready(cfg, full, sleep=fake_sleep, on_phase=seen.append)

    assert seen == [background.PHASE_CLONING, background.PHASE_AUTOSTART]
    # Returned without waiting for deletion: the job row is still present.
    with Session(engine) as s:
        assert s.get(BackgroundJob, full) is not None


def test_wait_for_background_ready_raises_on_failed(make_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    full = f"{cfg.container_prefix}-feat-f"

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import wait_for_background_ready

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=full,
            container_prefix=cfg.container_prefix,
            branch="feat/f",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )
        background.fail_job(s, full, "boom", now=datetime.now(UTC))

    sleep = MagicMock()
    with pytest.raises(ValueError, match="boom"):
        wait_for_background_ready(cfg, full, sleep=sleep)
    sleep.assert_not_called()


def test_wait_for_background_ready_raises_when_worker_dead(make_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()
    full = f"{cfg.container_prefix}-feat-d"

    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import wait_for_background_ready

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=full,
            container_prefix=cfg.container_prefix,
            branch="feat/d",
            pid=4242,
            log_path="/l",
            now=datetime.now(UTC),
        )

    monkeypatch.setattr(background, "worker_alive", lambda _pid: False)

    sleep = MagicMock()
    with pytest.raises(ValueError, match="worker"):
        wait_for_background_ready(cfg, full, sleep=sleep)
    sleep.assert_not_called()


def test_wait_for_background_ready_returns_instantly_when_no_row(make_cfg, tmp_path, monkeypatch):
    """A name with no job row is already ready; sleep is never called."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_cfg(tmp_path / "myrepo")
    cfg.repo_root.mkdir()

    from jailbee.lifecycle import wait_for_background_ready

    called = MagicMock()
    wait_for_background_ready(cfg, f"{cfg.container_prefix}-none", sleep=called)
    called.assert_not_called()


def test_new_container_inits_submodules_after_clone(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    init_mock = mocker.patch("jailbee.submodules.init_submodules_in_container")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    assert init_mock.call_count == 1
    assert init_mock.call_args.kwargs["repo_dir"].startswith("/home/")


def test_new_container_skips_submodules_when_disabled(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    cfg = cfg.model_copy(update={"new": cfg.new.model_copy(update={"submodules": False})})
    incus = MagicMock()
    incus.exists.return_value = False
    incus.exec.return_value = ""
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)
    init_mock = mocker.patch("jailbee.submodules.init_submodules_in_container")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    init_mock.assert_not_called()


def test_list_containers_populates_op_kind(make_cfg, tmp_path, monkeypatch, mocker):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY
    from jailbee.incus import Incus
    from jailbee.lifecycle import list_containers

    cfg = make_cfg(tmp_path / "repo")
    object.__setattr__(cfg, "container_prefix", "myrepo")
    incus = mocker.MagicMock(spec=Incus)
    incus.list_containers.return_value = [
        {
            "name": "myrepo-dying",
            "status": "Running",
            "profiles": ["default", "myrepo-net-strict"],
            "state": {"network": {}},
            "config": {},
        }
    ]
    incus.config_get.return_value = None

    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-dying",
            container_prefix="myrepo",
            branch=None,
            pid=123,
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=JOB_DESTROY,
        )

    rows = list_containers(cfg, incus, with_background=True)
    dying = next(c for c in rows if c.name == "myrepo-dying")
    assert dying.job_kind == JOB_DESTROY
    assert dying.job_phase == background.PHASE_STARTING


def test_wait_for_background_ready_rejects_destroy_op(make_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    import os
    from datetime import UTC, datetime

    import pytest
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY
    from jailbee.lifecycle import wait_for_background_ready

    cfg = make_cfg(tmp_path / "repo")
    object.__setattr__(cfg, "container_prefix", "myrepo")
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-dying",
            container_prefix="myrepo",
            branch=None,
            pid=os.getpid(),  # alive, so the guard (not the dead-pid path) fires
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=JOB_DESTROY,
        )

    with pytest.raises(ValueError, match="being destroyed"):
        wait_for_background_ready(cfg, "myrepo-dying", sleep=lambda _s: None)


def test_wait_for_background_ready_dead_destroy_worker_reports_gone(
    make_cfg, tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from datetime import UTC, datetime

    import pytest
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY
    from jailbee.lifecycle import wait_for_background_ready

    cfg = make_cfg(tmp_path / "repo")
    object.__setattr__(cfg, "container_prefix", "myrepo")
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-dying",
            container_prefix="myrepo",
            branch=None,
            pid=2**31 - 1,  # effectively-guaranteed-dead pid on Linux
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=JOB_DESTROY,
        )

    # A dead destroy worker reports "gone", not "being destroyed".
    with pytest.raises(ValueError, match="is gone"):
        wait_for_background_ready(cfg, "myrepo-dying", sleep=lambda _s: None)


def test_wait_for_background_ready_failed_destroy_says_destroy(make_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from datetime import UTC, datetime

    import pytest
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_DESTROY
    from jailbee.lifecycle import wait_for_background_ready

    cfg = make_cfg(tmp_path / "repo")
    object.__setattr__(cfg, "container_prefix", "myrepo")
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name="myrepo-dying",
            container_prefix="myrepo",
            branch=None,
            pid=2**31 - 1,  # guaranteed-dead pid so destroy guard does not fire
            log_path="/l",
            now=datetime.now(UTC),
            op_kind=JOB_DESTROY,
        )
        background.fail_job(s, "myrepo-dying", "delete blew up", now=datetime.now(UTC))

    with pytest.raises(ValueError, match="destroy of 'dying' failed"):
        wait_for_background_ready(cfg, "myrepo-dying", sleep=lambda _s: None)


# ---- _clone_repo_in_container: gie-base ref seeding ----


def test_clone_seeds_gie_base_ref(mocker, make_cfg, tmp_path):
    from jailbee.lifecycle import _clone_repo_in_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle._wire_origin_and_tracking")
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="deadbeefcafe")
    # Submodules off so the clone path doesn't reach into submodule helpers.
    object.__setattr__(cfg.new, "submodules", False)

    _clone_repo_in_container(
        cfg,
        incus,
        "p-feat-x",
        "feat/x",
        source_branch="main",
        create_new_branch=True,
        repo_target="/home/dev/repo",
        base_branch="main",
    )

    # One exec must seed refs/jailbee/base/main from the HOST's origin/main SHA
    # (the container clone does not carry refs/remotes/origin/main itself).
    seed_calls = [c for c in incus.exec.call_args_list if "update-ref" in c.args[1]]
    assert len(seed_calls) == 1
    assert seed_calls[0].args[1] == [
        "git",
        "-C",
        "/home/dev/repo",
        "update-ref",
        "refs/jailbee/base/main",
        "deadbeefcafe",
    ]


def test_clone_seed_swallows_missing_origin_base(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.lifecycle import _clone_repo_in_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle._wire_origin_and_tracking")
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="deadbeefcafe")
    object.__setattr__(cfg.new, "submodules", False)

    # update-ref raises (object unreachable) — must not propagate.
    def exec_side_effect(name, argv, **kwargs):
        if "update-ref" in argv:
            raise IncusError("fatal: refs/remotes/origin/main not a valid ref")
        return ""

    incus.exec.side_effect = exec_side_effect

    # Should complete without raising.
    _clone_repo_in_container(
        cfg,
        incus,
        "p-feat-x",
        "feat/x",
        source_branch="main",
        create_new_branch=True,
        repo_target="/home/dev/repo",
        base_branch="main",
    )


# ---- submodule sub-rows (Task 3) ----


def test_submodule_sub_rows_formats_changed_submodules():
    from jailbee.git_status import GitStatus, SubmoduleChange
    from jailbee.lifecycle import ContainerInfo, submodule_sub_rows

    c = ContainerInfo(
        name="p-feat-x",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
    )
    c.git_status = GitStatus(
        wt="clean",
        ahead_diff="+42 -7",
        ahead_count="2",
        conflict="ok",
        submodules=(
            SubmoduleChange("deps/libfoo", 42, 7, 2, 0, 0, "modified"),
            SubmoduleChange("vendor/bar", 0, 0, 5, 3, 0, "new"),
        ),
    )
    rows = submodule_sub_rows(c)
    assert rows[0]["name"] == "  └ deps/libfoo"
    assert rows[0]["ahead_diff"] == "+42 -7"
    assert rows[0]["ahead_count"] == "2"
    assert rows[0]["wt"] == "clean"
    assert rows[1]["name"] == "  └ vendor/bar"
    assert rows[1]["wt"] == "+3 -0"


def test_submodule_sub_rows_empty_when_no_status():
    from jailbee.lifecycle import ContainerInfo, submodule_sub_rows

    c = ContainerInfo(
        name="p-feat-x",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
    )
    assert submodule_sub_rows(c) == []


def test_ls_field_specs_json_includes_submodules_when_enabled():
    from datetime import UTC, datetime

    from jailbee.git_status import GitStatus, SubmoduleChange
    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    c = ContainerInfo(
        name="p-feat-x",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
    )
    c.git_status = GitStatus(
        wt="clean",
        ahead_diff="+42 -7",
        ahead_count="2",
        conflict="ok",
        submodules=(SubmoduleChange("deps/libfoo", 42, 7, 2, 0, 0, "modified"),),
    )
    specs = {
        f.name: f
        for f in ls_field_specs(now=datetime(2026, 6, 8, tzinfo=UTC), show_submodules=True)
    }
    payload = specs["git_status"].json(c)
    assert payload["submodules"] == [
        {
            "path": "deps/libfoo",
            "ahead_ins": 42,
            "ahead_del": 7,
            "ahead_commits": 2,
            "wt_ins": 0,
            "wt_del": 0,
            "status": "modified",
        }
    ]

    specs_off = {
        f.name: f
        for f in ls_field_specs(now=datetime(2026, 6, 8, tzinfo=UTC), show_submodules=False)
    }
    assert "submodules" not in specs_off["git_status"].json(c)


_NOW = datetime(2026, 6, 8, tzinfo=UTC)


def _ci_with_status(**status_kwargs):
    from jailbee.git_status import GitStatus
    from jailbee.lifecycle import ContainerInfo

    base = {"wt": "clean", "ahead_diff": "clean", "ahead_count": "0", "conflict": "ok"}
    return ContainerInfo(
        name="myrepo-feat-x",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        git_status=GitStatus(**{**base, **status_kwargs}),
    )


def test_local_columns_render_values():
    from jailbee.lifecycle import ls_field_specs

    ci = _ci_with_status(local_diff="+12 -3", local_count="3")
    specs = {f.name: f for f in ls_field_specs(now=_NOW)}

    assert specs["local_diff"].cell(ci) == "+12 -3"
    assert specs["local_count"].cell(ci) == "3"
    assert specs["local_diff"].header == "LOCAL ±"
    assert specs["local_count"].header == "L↑"
    assert specs["local_count"].justify == "right"


def test_local_columns_render_clean_unknown_and_zero():
    from jailbee.lifecycle import ls_field_specs

    specs = {f.name: f for f in ls_field_specs(now=_NOW)}

    clean = _ci_with_status(local_diff="clean", local_count="0")
    assert specs["local_diff"].cell(clean) == "[dim]clean[/dim]"
    assert specs["local_count"].cell(clean) == "[dim]0[/dim]"

    unknown = _ci_with_status(local_diff="?", local_count="?")
    assert specs["local_diff"].cell(unknown) == "[yellow]?[/yellow]"
    assert specs["local_count"].cell(unknown) == "[yellow]?[/yellow]"


def test_local_columns_render_dash_without_git_status():
    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    ci = ContainerInfo(
        name="myrepo-feat-x",
        state="Stopped",
        network="strict",
        ip=None,
        memory_limit=None,
    )
    specs = {f.name: f for f in ls_field_specs(now=_NOW)}

    assert specs["local_diff"].cell(ci) == "[dim]—[/dim]"
    assert specs["local_count"].cell(ci) == "[dim]—[/dim]"


def test_local_columns_are_opt_in():
    """The default table is already wide — these arrive via --fields or config."""
    from jailbee.lifecycle import ls_field_specs

    specs = {f.name: f for f in ls_field_specs(now=_NOW)}

    assert specs["local_diff"].default_table is False
    assert specs["local_count"].default_table is False
    assert specs["local_diff"].default_json is False
    assert specs["local_count"].default_json is False


def test_git_status_json_carries_the_new_keys():
    from jailbee.lifecycle import ls_field_specs

    ci = _ci_with_status(
        head_sha="abc123", remote_contained=True, local_diff="+12 -3", local_count="3"
    )
    spec = next(f for f in ls_field_specs(now=_NOW) if f.name == "git_status")

    payload = spec.json(ci)

    assert payload["head_sha"] == "abc123"
    assert payload["remote_contained"] is True
    assert payload["local_diff"] == "+12 -3"
    assert payload["local_count"] == "3"


def _pr_field():
    from datetime import UTC, datetime

    from jailbee.lifecycle import ls_field_specs

    now = datetime(2026, 7, 20, tzinfo=UTC)
    return next(f for f in ls_field_specs(now=now) if f.name == "pr")


def test_pr_field_cell_author():
    from jailbee.lifecycle import ContainerInfo

    field = _pr_field()
    c = ContainerInfo(
        name="r-x",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        pr_number=123,
        pr_author=True,
    )
    assert field.cell(c) == "#123"
    assert field.json(c) == {"number": 123, "role": "author"}


def test_pr_field_cell_review():
    from jailbee.lifecycle import ContainerInfo

    field = _pr_field()
    c = ContainerInfo(
        name="r-x",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        pr_number=456,
        pr_author=False,
    )
    assert field.cell(c) == "#456↓"
    assert field.json(c) == {"number": 456, "role": "review"}


def test_pr_field_cell_empty_when_no_pr():
    from jailbee.lifecycle import ContainerInfo

    field = _pr_field()
    c = ContainerInfo(name="r-x", state="Running", network=None, ip=None, memory_limit=None)
    assert field.cell(c) == ""
    assert field.json(c) is None


def test_pr_field_show_if():
    from jailbee.lifecycle import ContainerInfo

    field = _pr_field()
    no_pr = ContainerInfo(name="r-a", state="Running", network=None, ip=None, memory_limit=None)
    with_pr = ContainerInfo(
        name="r-b",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        pr_number=1,
    )
    assert field.show_if is not None
    assert field.show_if([no_pr]) is False
    assert field.show_if([no_pr, with_pr]) is True


def test_mode_field_shows_only_when_a_mount_container_exists():
    """MODE is a constant column on a clone-only host, and constants are
    width without information. It comes back the moment the two modes
    coexist and telling a row's kind apart starts to matter."""
    from datetime import UTC, datetime

    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    specs = ls_field_specs(now=datetime(2026, 6, 8, tzinfo=UTC), all_repos=False)
    field = next(f for f in specs if f.name == "mode")
    clone = ContainerInfo(
        name="r-a", state="Running", network=None, ip=None, memory_limit=None, mode="clone"
    )
    mount = ContainerInfo(
        name="r-b", state="Running", network=None, ip=None, memory_limit=None, mode="mount"
    )

    assert field.show_if is not None
    assert field.show_if([clone, clone]) is False
    assert field.show_if([clone, mount]) is True


# ---- CLI-level submodule sub-row tests (Task 3) ----


def _setup_repo_with_gitmodules(tmp_path, name="myrepo"):
    """Create a minimal fake repo directory with .gie/config.yaml and .gitmodules."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".gie").mkdir()
    (repo / ".gie" / "config.yaml").write_text("{}\n")
    (repo / ".gitmodules").write_text('[submodule "deps/libfoo"]\n\tpath = deps/libfoo\n')
    return repo


def test_ls_shows_submodule_rows_when_gitmodules_present(tmp_path, mocker):
    """``gie ls`` renders indented sub-rows for submodule changes when .gitmodules exists."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.git_status import GitStatus, SubmoduleChange
    from jailbee.lifecycle import ContainerInfo

    repo = _setup_repo_with_gitmodules(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".gie" / "config.yaml",
    )

    containers = [
        ContainerInfo(
            name="myrepo-feat-sub",
            state="Running",
            network="strict",
            ip="10.0.0.1",
            memory_limit="4GB",
            repo="myrepo",
            mode="clone",
            base_branch="main",
            git_status=GitStatus(
                wt="clean",
                ahead_diff="+42 -7",
                ahead_count="2",
                conflict="ok",
                submodules=(SubmoduleChange("deps/libfoo", 42, 7, 2, 0, 0, "modified"),),
            ),
        ),
    ]
    mocker.patch("jailbee.lifecycle.list_containers", return_value=containers)
    mocker.patch("jailbee.incus.Incus")

    result = CliRunner().invoke(app, ["ls"], env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.stdout
    assert "└ deps/libfoo" in result.stdout


def test_ls_no_submodules_flag_suppresses_rows(tmp_path, mocker):
    """``gie ls --no-submodules`` hides the submodule sub-rows even in a submodule repo."""
    from typer.testing import CliRunner

    from jailbee.cli import app
    from jailbee.git_status import GitStatus, SubmoduleChange
    from jailbee.lifecycle import ContainerInfo

    repo = _setup_repo_with_gitmodules(tmp_path, "myrepo")
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo / ".gie" / "config.yaml",
    )

    containers = [
        ContainerInfo(
            name="myrepo-feat-sub",
            state="Running",
            network="strict",
            ip="10.0.0.1",
            memory_limit="4GB",
            repo="myrepo",
            mode="clone",
            base_branch="main",
            git_status=GitStatus(
                wt="clean",
                ahead_diff="+42 -7",
                ahead_count="2",
                conflict="ok",
                submodules=(SubmoduleChange("deps/libfoo", 42, 7, 2, 0, 0, "modified"),),
            ),
        ),
    ]
    mocker.patch("jailbee.lifecycle.list_containers", return_value=containers)
    mocker.patch("jailbee.incus.Incus")

    result = CliRunner().invoke(app, ["ls", "--no-submodules"], env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.stdout
    assert "libfoo" not in result.stdout


def test_ls_job_cell_names_the_phase_a_dead_worker_died_in(mocker):
    """A dead worker keeps its phase in the label, not a bare 'failed'."""
    from datetime import UTC, datetime

    from jailbee import background
    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    mocker.patch.object(background, "worker_alive", return_value=False)
    c = ContainerInfo(
        name="myrepo-feat-foo",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        repo="myrepo",
        job_phase=background.PHASE_CLONING,
        job_pid=999,
        job_kind="create",
    )
    spec = next(
        f for f in ls_field_specs(now=datetime.now(UTC), all_repos=False) if f.name == "job"
    )
    assert "cloning (worker gone)" in spec.cell(c)
    assert spec.json(c) == "cloning (worker gone)"


def test_ls_job_cell_agrees_with_job_ls_phase_cell_for_a_live_destroy_job(mocker):
    """`gie ls`'s JOB cell and `gie job ls`'s PHASE cell must never disagree
    about the same state — a live `destroy`-kind job in `starting` reads
    'destroying' in both."""
    from datetime import UTC, datetime, timedelta

    from jailbee import background, jobs
    from jailbee.db.models import JOB_DESTROY, BackgroundJob
    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    mocker.patch.object(background, "worker_alive", return_value=True)
    now = datetime.now(UTC)
    c = ContainerInfo(
        name="myrepo-feat-foo",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        repo="myrepo",
        job_phase=background.PHASE_STARTING,
        job_pid=999,
        job_kind=JOB_DESTROY,
    )
    job = BackgroundJob(
        container_name="myrepo-feat-foo",
        container_prefix="myrepo",
        branch="feat/foo",
        phase=background.PHASE_STARTING,
        pid=999,
        log_path="/logs/myrepo-feat-foo.log",
        error_msg=None,
        op_kind=JOB_DESTROY,
        started_at=now - timedelta(minutes=1),
        updated_at=now,
    )

    ls_spec = next(f for f in ls_field_specs(now=now, all_repos=False) if f.name == "job")
    job_spec = next(f for f in jobs.job_field_specs(now=now, all_repos=False) if f.name == "phase")

    assert "destroying" in ls_spec.cell(c)
    assert "destroying" in job_spec.cell(job)
    assert "[yellow]" in ls_spec.cell(c)
    assert "[yellow]" in job_spec.cell(job)


def test_ls_job_cell_agrees_with_job_ls_phase_cell_for_a_dead_destroy_job(mocker):
    """A dead destroy job reads 'starting (worker gone)' in both places, not
    'destroying' — the friendlier name must not hide a vanished worker."""
    from datetime import UTC, datetime, timedelta

    from jailbee import background, jobs
    from jailbee.db.models import JOB_DESTROY, BackgroundJob
    from jailbee.lifecycle import ContainerInfo, ls_field_specs

    mocker.patch.object(background, "worker_alive", return_value=False)
    now = datetime.now(UTC)
    c = ContainerInfo(
        name="myrepo-feat-foo",
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        repo="myrepo",
        job_phase=background.PHASE_STARTING,
        job_pid=999,
        job_kind=JOB_DESTROY,
    )
    job = BackgroundJob(
        container_name="myrepo-feat-foo",
        container_prefix="myrepo",
        branch="feat/foo",
        phase=background.PHASE_STARTING,
        pid=999,
        log_path="/logs/myrepo-feat-foo.log",
        error_msg=None,
        op_kind=JOB_DESTROY,
        started_at=now - timedelta(minutes=1),
        updated_at=now,
    )

    ls_spec = next(f for f in ls_field_specs(now=now, all_repos=False) if f.name == "job")
    job_spec = next(f for f in jobs.job_field_specs(now=now, all_repos=False) if f.name == "phase")

    assert "starting (worker gone)" in ls_spec.cell(c)
    assert "starting (worker gone)" in job_spec.cell(job)
    assert "[red]" in ls_spec.cell(c)
    assert "[red]" in job_spec.cell(job)


def _new_env(make_cfg, tmp_path, mocker, *, local_branch: bool = True):
    """The `new_container` fixture set used by the branch-config tests.

    `local_branch=True` makes `feat/foo` resolve as a local branch, so the
    clone ref is `refs/heads/feat/foo` and no origin fetch happens.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo, default_branch="main")
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch(
        "jailbee.lifecycle.branch_exists_in_source",
        return_value=local_branch,
    )
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=local_branch)
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value="abc1234")
    mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    return cfg, incus


def _branch_new_opts(**overrides):
    """A clone-mode NewContainerOptions with autostart on."""
    base = dict(
        container_branch="feat/foo",
        name=None,
        network="strict",
        memory="4GB",
        cpu=4,
        from_base="gie-golden",
        clone=True,
        autostart=True,
    )
    base.update(overrides)
    return NewContainerOptions(**base)


def _branch_result(cfg, deviation, source="refs/heads/feat/foo"):
    from jailbee.branch_config import BranchAutostart

    return BranchAutostart(cfg=cfg, deviation=deviation, source=source)


def test_new_container_uses_branch_autostart(make_cfg, tmp_path, mocker):
    """The grafted config, not the host one, reaches run_autostart."""
    from jailbee.branch_config import AutostartDeviation
    from jailbee.config import Autostart, AutostartStep

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    grafted = cfg.model_copy(
        update={"autostart": Autostart(on_create=[AutostartStep(name="seed", run="./s")])}
    )
    mocker.patch(
        "jailbee.branch_config.load_branch_autostart",
        return_value=_branch_result(grafted, AutostartDeviation(added=("on_create[seed]",))),
    )
    run_autostart = mocker.patch("jailbee.autostart.run_autostart")

    new_container(cfg, incus, _branch_new_opts())

    assert run_autostart.call_args_list[0].args[0] is grafted


def test_ordinary_deviation_warns_but_does_not_prompt(make_cfg, tmp_path, mocker):
    from jailbee.branch_config import AutostartDeviation, StepChange

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    mocker.patch(
        "jailbee.branch_config.load_branch_autostart",
        return_value=_branch_result(
            cfg,
            AutostartDeviation(changed=(StepChange("on_create[build]", ("run",)),)),
            source="refs/heads/feat/foo",
        ),
    )
    mocker.patch("jailbee.autostart.run_autostart")
    warned = mocker.patch("jailbee.lifecycle.warn_plain")
    confirm = mocker.Mock(return_value=True)

    new_container(cfg, incus, _branch_new_opts(), confirm_fn=confirm)

    confirm.assert_not_called()
    deviation_warnings = [
        c for c in warned.call_args_list if "autostart config comes from" in c.args[0]
    ]
    assert len(deviation_warnings) == 1
    assert "refs/heads/feat/foo" in deviation_warnings[0].args[0]


def _wire_branch_autostart(cfg, mocker, *, branch, baseline, deviation=None):
    """Wire both comparisons the branch-config path makes.

    `branch` is grafted onto the config `load_branch_autostart` returns;
    `baseline` is what the privilege gate reads from
    `refs/remotes/origin/<default_branch>`. Returns the grafted config.
    """
    from jailbee.branch_config import AutostartDeviation

    grafted = cfg.model_copy(update={"autostart": branch})
    mocker.patch(
        "jailbee.branch_config.load_branch_autostart",
        return_value=_branch_result(grafted, deviation or AutostartDeviation()),
    )
    mocker.patch("jailbee.git.show_file_at_ref", return_value="autostart: {}")
    mocker.patch(
        "jailbee.config.load_config_from_text",
        return_value=cfg.model_copy(update={"autostart": baseline}),
    )
    return grafted


def _loose(name="seed", **kw):
    from jailbee.config import Autostart, AutostartStep

    return Autostart(on_create=[AutostartStep(name=name, run="./s", network="loose", **kw)])


def test_network_widening_from_your_own_repo_warns_without_prompting(make_cfg, tmp_path, mocker):
    """The regression: a plain `gie new` must not stop to ask about `loose`.

    Once the container runs the branch's code, `strict` is not a boundary
    against it — and a background `gie new` cannot answer a question at all.
    """
    from jailbee.config import Autostart

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(cfg, mocker, branch=_loose(), baseline=Autostart())
    mocker.patch("jailbee.autostart.run_autostart")
    warned = mocker.patch("jailbee.lifecycle.warn_plain")
    confirm = mocker.Mock(return_value=False)

    new_container(cfg, incus, _branch_new_opts(), confirm_fn=confirm)

    confirm.assert_not_called()
    incus.init.assert_called_once()  # creation proceeded
    # Not asking is not the same as not saying: the widening is still reported.
    assert any("widens privileges" in c.args[0] for c in warned.call_args_list)


def test_widening_already_granted_by_the_baseline_is_not_reported(make_cfg, tmp_path, mocker):
    """A checkout lagging `origin/<default_branch>` grants nothing new.

    The host config here has no loose step at all — the branch inherits the
    baseline's, which is exactly the shape that used to fail a background
    `gie new`.
    """
    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(cfg, mocker, branch=_loose(), baseline=_loose())
    mocker.patch("jailbee.autostart.run_autostart")
    warned = mocker.patch("jailbee.lifecycle.warn_plain")
    confirm = mocker.Mock(return_value=False)

    new_container(cfg, incus, _branch_new_opts(), confirm_fn=confirm)

    confirm.assert_not_called()
    incus.init.assert_called_once()
    assert not any("widens privileges" in c.args[0] for c in warned.call_args_list)


def test_network_widening_from_an_untrusted_head_prompts_and_proceeds_on_yes(
    make_cfg, tmp_path, mocker
):
    """A fork's head is code nobody with push access vouched for — there, asked."""
    from jailbee.config import Autostart

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(cfg, mocker, branch=_loose(), baseline=Autostart())
    mocker.patch("jailbee.autostart.run_autostart")
    confirm = mocker.Mock(return_value=True)

    new_container(cfg, incus, _branch_new_opts(untrusted_head=True), confirm_fn=confirm)

    confirm.assert_called_once()
    incus.init.assert_called_once()  # creation proceeded


def test_a_pr_number_alone_does_not_make_a_head_untrusted(make_cfg, tmp_path, mocker):
    """An internal PR's head is a branch in this repo's own origin.

    Byte-identical to what `gie new <branch>` clones, pushed by someone who can
    already run code in these containers — so it must behave identically, or the
    prompt is about how the command was spelled rather than about risk.
    """
    from jailbee.config import Autostart

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(cfg, mocker, branch=_loose(), baseline=Autostart())
    mocker.patch("jailbee.autostart.run_autostart")
    confirm = mocker.Mock(return_value=False)

    new_container(cfg, incus, _branch_new_opts(pr=7), confirm_fn=confirm)

    confirm.assert_not_called()
    incus.init.assert_called_once()


def test_declined_escalation_aborts_before_creating_anything(make_cfg, tmp_path, mocker):
    from jailbee.config import Autostart

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(cfg, mocker, branch=_loose(), baseline=Autostart())
    confirm = mocker.Mock(return_value=False)

    with pytest.raises(ValueError, match=r"[Aa]borted"):
        new_container(cfg, incus, _branch_new_opts(untrusted_head=True), confirm_fn=confirm)

    confirm.assert_called_once()
    incus.init.assert_not_called()
    incus.profile_assign.assert_not_called()
    incus.start.assert_not_called()


def test_assume_yes_skips_the_escalation_prompt(make_cfg, tmp_path, mocker):
    from jailbee.config import Autostart

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(cfg, mocker, branch=_loose(), baseline=Autostart())
    mocker.patch("jailbee.autostart.run_autostart")
    confirm = mocker.Mock(return_value=False)

    new_container(
        cfg, incus, _branch_new_opts(untrusted_head=True, assume_yes=True), confirm_fn=confirm
    )

    confirm.assert_not_called()
    incus.init.assert_called_once()


def test_a_preflight_approval_for_this_ref_is_not_asked_again(make_cfg, tmp_path, mocker):
    """The background worker inherits the answer the foreground already got."""
    from jailbee.config import Autostart

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(cfg, mocker, branch=_loose(), baseline=Autostart())
    mocker.patch("jailbee.autostart.run_autostart")
    confirm = mocker.Mock(return_value=False)  # a worker's declining stub

    new_container(
        cfg,
        incus,
        _branch_new_opts(untrusted_head=True, approved_autostart_ref="refs/heads/feat/foo"),
        confirm_fn=confirm,
    )

    confirm.assert_not_called()
    incus.init.assert_called_once()


def test_a_preflight_approval_for_another_ref_aborts_naming_the_move(make_cfg, tmp_path, mocker):
    """The answer covers one commit. If the branch moved in between, the worker
    must not provision a config nobody was shown — and must say why."""
    from jailbee.config import Autostart

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(cfg, mocker, branch=_loose(), baseline=Autostart())
    confirm = mocker.Mock(return_value=False)

    with pytest.raises(ValueError, match=r"moved between the confirmation"):
        new_container(
            cfg,
            incus,
            _branch_new_opts(untrusted_head=True, approved_autostart_ref="0123456789abcdef"),
            confirm_fn=confirm,
        )

    confirm.assert_not_called()
    incus.init.assert_not_called()


def test_autofetch_done_suppresses_the_worker_side_fetch(make_cfg, tmp_path, mocker):
    """The foreground pre-flight already fetched; fetching again could resolve a
    newer commit than the one it assessed."""
    cfg, incus = _new_env(make_cfg, tmp_path, mocker, local_branch=False)
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch("jailbee.branch_config.load_branch_autostart", return_value=None)
    mocker.patch("jailbee.autostart.run_autostart")

    new_container(cfg, incus, _branch_new_opts(autofetch_done=True))

    fetch.assert_not_called()


def test_the_foreground_path_still_fetches(make_cfg, tmp_path, mocker):
    """Guard for the flag's default: a plain `gie new` must keep autofetching."""
    cfg, incus = _new_env(make_cfg, tmp_path, mocker, local_branch=False)
    fetch = mocker.patch("jailbee.lifecycle.fetch_remote_ref")
    mocker.patch("jailbee.branch_config.load_branch_autostart", return_value=None)
    mocker.patch("jailbee.autostart.run_autostart")

    new_container(cfg, incus, _branch_new_opts())

    fetch.assert_called_once()


def test_no_deviation_prints_no_warning(make_cfg, tmp_path, mocker):
    from jailbee.branch_config import AutostartDeviation

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    mocker.patch(
        "jailbee.branch_config.load_branch_autostart",
        return_value=_branch_result(cfg, AutostartDeviation()),
    )
    mocker.patch("jailbee.autostart.run_autostart")
    warned = mocker.patch("jailbee.lifecycle.warn_plain")
    confirm = mocker.Mock()

    new_container(cfg, incus, _branch_new_opts(), confirm_fn=confirm)

    confirm.assert_not_called()
    assert not any("autostart config comes from" in c.args[0] for c in warned.call_args_list)


def test_mount_mode_skips_the_branch_config_check(make_cfg, tmp_path, mocker):
    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    load = mocker.patch("jailbee.branch_config.load_branch_autostart")
    mocker.patch("jailbee.autostart.run_autostart")

    new_container(
        cfg,
        incus,
        _branch_new_opts(container_branch="", name="myrepo-x", mount=True, clone=False),
    )

    load.assert_not_called()


def test_no_clone_skips_the_branch_config_check(make_cfg, tmp_path, mocker):
    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    load = mocker.patch("jailbee.branch_config.load_branch_autostart")
    mocker.patch("jailbee.autostart.run_autostart")

    new_container(cfg, incus, _branch_new_opts(clone=False))

    load.assert_not_called()


def test_falls_back_to_host_autostart_when_branch_config_unusable(make_cfg, tmp_path, mocker):
    """load_branch_autostart returned None (already warned) — host config is used, and no
    second warning is emitted (load_branch_autostart already warned if it needed to)."""
    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    mocker.patch("jailbee.branch_config.load_branch_autostart", return_value=None)
    run_autostart = mocker.patch("jailbee.autostart.run_autostart")
    warned = mocker.patch("jailbee.lifecycle.warn_plain")

    new_container(cfg, incus, _branch_new_opts())

    assert run_autostart.call_args_list[0].args[0] is cfg
    assert not any("autostart config comes from" in c.args[0] for c in warned.call_args_list)


def test_branch_config_read_from_clone_commit_when_set(make_cfg, tmp_path, mocker):
    """PR mode: the ref is the exact commit, not a branch name."""
    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    load = mocker.patch("jailbee.branch_config.load_branch_autostart", return_value=None)
    mocker.patch("jailbee.autostart.run_autostart")
    sha = "deadbeef" * 5

    new_container(cfg, incus, _branch_new_opts(clone_commit=sha))

    assert load.call_args.args[1] == sha


def test_branch_config_read_from_local_ref_when_no_commit(make_cfg, tmp_path, mocker):
    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    load = mocker.patch("jailbee.branch_config.load_branch_autostart", return_value=None)
    mocker.patch("jailbee.autostart.run_autostart")

    new_container(cfg, incus, _branch_new_opts(container_branch="feat/foo"))

    assert load.call_args.args[1] == "refs/heads/feat/foo"
    # Local-branch clones are labelled with the full ref — no sha to show.
    assert load.call_args.kwargs["source_label"] == "refs/heads/feat/foo"


_ORIGIN_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_branch_config_read_from_origin_resolved_sha_for_an_origin_only_branch(
    make_cfg, tmp_path, mocker
):
    """Origin mode: the branch exists only as `refs/remotes/origin/feat/foo`.

    Pins the ref (the sha `rev_parse_origin` resolved, read post-fetch so it is
    never stale) and the `<sha12> (<branch>)` label shape the docs promise.
    """
    cfg, incus = _new_env(make_cfg, tmp_path, mocker, local_branch=False)
    mocker.patch("jailbee.lifecycle.branch_exists_in_source", return_value=True)
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value=_ORIGIN_SHA)
    load = mocker.patch("jailbee.branch_config.load_branch_autostart", return_value=None)
    mocker.patch("jailbee.autostart.run_autostart")

    new_container(cfg, incus, _branch_new_opts())

    assert load.call_args.args[1] == _ORIGIN_SHA
    assert load.call_args.kwargs["source_label"] == "0123456789ab (feat/foo)"


def test_branch_config_read_from_the_base_commit_when_forking_off_origin_default(
    make_cfg, tmp_path, mocker
):
    """The other origin-mode shape — `clone_from: origin` forking a new branch.

    Nothing named `feat/foo` exists yet, so the clone (and therefore the
    autostart read) is of `origin/main`'s commit; the label names that branch.
    """
    cfg, incus = _new_env(make_cfg, tmp_path, mocker, local_branch=False)
    mocker.patch("jailbee.lifecycle.rev_parse_remote", return_value=_ORIGIN_SHA)
    load = mocker.patch("jailbee.branch_config.load_branch_autostart", return_value=None)
    mocker.patch("jailbee.autostart.run_autostart")

    new_container(cfg, incus, _branch_new_opts())

    assert load.call_args.args[1] == _ORIGIN_SHA
    assert load.call_args.kwargs["source_label"] == "0123456789ab (main)"


def test_no_autostart_skips_the_branch_config_check_entirely(make_cfg, tmp_path, mocker):
    """With `--no-autostart` the branch's autostart never runs, so reading it —
    and above all prompting about it — would be about nothing. It would also
    fail `gie new --pr N --no-autostart --background`, whose worker declines by
    construction.
    """
    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    load = mocker.patch("jailbee.branch_config.load_branch_autostart")
    run_autostart = mocker.patch("jailbee.autostart.run_autostart")
    confirm = mocker.Mock(return_value=False)

    new_container(cfg, incus, _branch_new_opts(autostart=False), confirm_fn=confirm)

    load.assert_not_called()
    confirm.assert_not_called()
    run_autostart.assert_not_called()
    incus.init.assert_called_once()  # creation proceeded


def test_inject_github_token_gets_the_host_config_not_the_grafted_one(make_cfg, tmp_path, mocker):
    """gie's own GH_TOKEN step must not inherit the branch's `autostart.env`.

    `_apply_step` merges `cfg.autostart.env` into every step's environment, and
    this step pipes the host's PAT through `sudo tee` after the branch's tree is
    already cloned in. The branch config reaches `run_autostart` only.
    """
    from jailbee.branch_config import AutostartDeviation
    from jailbee.config import Autostart, AutostartStep

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    grafted = cfg.model_copy(
        update={
            "autostart": Autostart(
                on_create=[AutostartStep(name="seed", run="./s")],
                env={"PATH": "/tmp/evil:/usr/bin"},
            )
        }
    )
    mocker.patch(
        "jailbee.branch_config.load_branch_autostart",
        return_value=_branch_result(grafted, AutostartDeviation(added=("on_create[seed]",))),
    )
    inject = mocker.patch("jailbee.autostart.inject_github_token")
    run_autostart = mocker.patch("jailbee.autostart.run_autostart")

    new_container(cfg, incus, _branch_new_opts())

    assert inject.call_args.args[0] is cfg
    assert run_autostart.call_args_list[0].args[0] is grafted


def test_mount_escalation_prompts_without_a_pr_too(make_cfg, tmp_path, mocker):
    """Attaching a host mount is asked about regardless of provenance.

    Unlike `loose`, it creates an asset the container did not otherwise hold —
    so it is gated even for a branch from the operator's own repo.
    """
    from jailbee.config import Autostart, AutostartStep

    cfg, incus = _new_env(make_cfg, tmp_path, mocker)
    _wire_branch_autostart(
        cfg,
        mocker,
        branch=Autostart(on_create=[AutostartStep(name="seed", run="./s", mounts=["aws"])]),
        baseline=Autostart(),
    )
    confirm = mocker.Mock(return_value=False)

    with pytest.raises(ValueError, match=r"[Aa]borted"):
        new_container(cfg, incus, _branch_new_opts(), confirm_fn=confirm)

    confirm.assert_called_once()
    incus.init.assert_not_called()


def test_new_container_attaches_config_ports(tmp_path, mocker):
    """A `host_ports` entry becomes a proxy device on the new container."""
    from jailbee.config import HostPort

    cfg = _cfg_for_new(tmp_path)
    cfg = cfg.model_copy(update={"host_ports": [HostPort(name="adb", port=5037)]})
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    proxy_adds = [c for c in incus.config_device_add.call_args_list if c.args[2] == "proxy"]
    assert len(proxy_adds) == 1
    assert proxy_adds[0].args[0] == "repo-feat-x"
    assert proxy_adds[0].args[1] == "port-cfg-adb"
    assert proxy_adds[0].args[3] == {
        "listen": "tcp:127.0.0.1:5037",
        "connect": "tcp:127.0.0.1:5037",
        "bind": "instance",
    }


def test_new_container_adds_no_proxy_device_without_host_ports(tmp_path, mocker):
    cfg = _cfg_for_new(tmp_path)
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )
    new_container(cfg, incus, opts)

    assert [c for c in incus.config_device_add.call_args_list if c.args[2] == "proxy"] == []


def test_new_container_warns_and_continues_when_port_attach_fails(tmp_path, mocker):
    """A non-"already exists" failure while attaching a config-declared
    forward must not traceback: `new_container` has already created and
    started the container by this point, so it warns and continues rather
    than letting the `PortError` escape (previously uncaught here and in
    every caller up to `entry.main`).
    """
    from jailbee.config import HostPort
    from jailbee.incus import IncusError

    cfg = _cfg_for_new(tmp_path)
    cfg = cfg.model_copy(update={"host_ports": [HostPort(name="adb", port=5037)]})
    incus = MagicMock()
    incus.exists.return_value = False
    mocker.patch("jailbee.lifecycle.branch_exists_locally", return_value=True)

    def _add_device(_name: str, _device: str, dtype: str, _props: dict) -> None:
        if dtype == "proxy":
            raise IncusError("Error: the daemon is on fire")

    incus.config_device_add.side_effect = _add_device
    warn_plain = mocker.patch("jailbee.lifecycle.warn_plain")

    opts = NewContainerOptions(
        container_branch="feat/x",
        name=None,
        network="strict",
        memory="8GiB",
        cpu=4,
        from_base="gisgro-base",
        clone=True,
        autostart=False,
    )

    created = new_container(cfg, incus, opts)  # must not raise

    assert created == "repo-feat-x"
    warn_plain.assert_called_once()
    msg = warn_plain.call_args.args[0]
    assert "jailbee apply" in msg
