"""Warm fork pattern policy and reflink seeding."""

from unittest.mock import MagicMock, call

import pytest
from pydantic import ValidationError

from jailbee.config import ForkConfig
from jailbee.fork_warm import match_patterns, validate_pattern
from jailbee.incus import Incus, IncusError
from tests.conftest import make_cfg


@pytest.mark.parametrize(
    "pattern, expected",
    [
        (".gradle", ".gradle"),
        ("*/build", "*/build"),
        ("app/bibleview-js/node_modules", "app/bibleview-js/node_modules"),
        ("**/build", "**/build"),
        ("build/", "build"),
    ],
)
def test_validate_pattern_accepts(pattern, expected):
    assert validate_pattern(pattern) == expected


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/abs",
        "a/../b",
        "..",
        ".git/x",
        "a b",
        "x;rm",
        "$(x)",
        '"q"',
        ".",
        "./",
        "a/./b",
        "a//b",
    ],
)
def test_validate_pattern_rejects(pattern):
    with pytest.raises(ValueError):
        validate_pattern(pattern)


def test_match_star_does_not_cross_slash():
    assert match_patterns(["*/build"], ["app/build/", "a/b/build/", "build/"]) == (
        ["app/build"],
        [],
    )


def test_match_double_star():
    assert match_patterns(["**/build"], ["build/", "a/b/build/"])[0] == ["build", "a/b/build"]


def test_match_reports_unmatched():
    assert match_patterns([".gradle", "nope"], [".gradle/"]) == ([".gradle"], ["nope"])


def test_match_drops_descendant():
    assert match_patterns(["app", "app/build"], ["app/build/", "app/", "app/"])[0] == ["app"]


@pytest.mark.parametrize(
    "override, expected", [(None, [".gradle"]), ([], []), (["build/"], ["build"])]
)
def test_resolve_warm_paths(tmp_path, override, expected):
    from jailbee.fork_warm import resolve_warm_paths

    cfg = make_cfg(tmp_path).model_copy(update={"fork": ForkConfig(warm_paths=[".gradle"])})
    assert resolve_warm_paths(cfg, override) == expected
    with pytest.raises(ValueError):
        resolve_warm_paths(cfg, ["/bad"])


def test_fork_config():
    assert ForkConfig().warm_paths == []
    assert ForkConfig(warm_paths=["build/"]).warm_paths == ["build"]
    with pytest.raises(ValidationError):
        ForkConfig(warm_paths=["/x"])


def test_full_config_defaults(tmp_path):
    from tests.conftest import make_cfg

    assert make_cfg(tmp_path).fork.warm_paths == []


@pytest.fixture
def warm_rig(tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = [{"name": "repo-source", "status": "Running"}]
    incus.config_show.return_value = (
        "devices:\n  root:\n    type: disk\n    path: /\n    pool: default\n"
    )
    incus.list_storage_pools.return_value = [{"name": "default", "driver": "btrfs"}]
    incus.config_get.return_value = "/home/dev/repo"
    incus.current_project.return_value = "default"
    incus.snapshot_list.return_value = []
    incus.exec.return_value = "app/build/\0.gradle/\0"
    mocker.patch("jailbee.sync._container_status_dirty", return_value=False)
    mocker.patch("jailbee.sync._container_head_oid", return_value="a" * 40)
    return cfg, incus


def seed(rig, patterns=("*/build", ".gradle")):
    from jailbee.fork_warm import seed_warm_paths

    cfg, incus = rig
    return seed_warm_paths(
        cfg, incus, child="repo-child", source="repo-source", commit="a" * 40, patterns=patterns
    )


def test_empty_patterns_does_nothing(warm_rig):
    assert seed(warm_rig, []).seeded == []
    assert warm_rig[1].mock_calls == []


@pytest.mark.parametrize("reason", ["pool", "dirty", "moved", "stopped", "project"])
def test_preconditions_skip_without_snapshot(warm_rig, mocker, reason):
    _, incus = warm_rig
    if reason == "pool":
        incus.list_storage_pools.return_value[0]["driver"] = "dir"
    elif reason == "dirty":
        mocker.patch("jailbee.sync._container_status_dirty", return_value=True)
    elif reason == "moved":
        mocker.patch("jailbee.sync._container_head_oid", return_value="b" * 40)
    elif reason == "project":
        incus.current_project.return_value = "other"
    else:
        incus.list_containers.return_value = []
    result = seed(warm_rig)
    assert result.skipped
    incus.snapshot_create.assert_not_called()
    incus.config_device_add.assert_not_called()


def test_happy_path_order(warm_rig):
    cfg, incus = warm_rig
    assert seed(warm_rig).seeded == ["app/build", ".gradle"]
    uid, gid = cfg.container_user.uid, cfg.container_user.gid
    expected = [
        call.snapshot_create("repo-source", "jailbee-warm-repo-child"),
        call.config_device_add(
            "repo-child",
            "jailbee-warm",
            "disk",
            {
                "source": (
                    "/var/lib/incus/storage-pools/default/containers-snapshots/"
                    "repo-source/jailbee-warm-repo-child/rootfs/home/dev/repo"
                ),
                "path": "/run/jailbee-warm",
                "readonly": "true",
            },
        ),
        call.exec("repo-child", ["mkdir", "-p", "--", "/home/dev/repo/app"], uid=uid, gid=gid),
        call.exec(
            "repo-child",
            [
                "cp",
                "-a",
                "--reflink=always",
                "--",
                "/run/jailbee-warm/app/build",
                "/home/dev/repo/app/build",
            ],
            uid=uid,
            gid=gid,
        ),
        call.exec("repo-child", ["mkdir", "-p", "--", "/home/dev/repo"], uid=uid, gid=gid),
        call.exec(
            "repo-child",
            [
                "cp",
                "-a",
                "--reflink=always",
                "--",
                "/run/jailbee-warm/.gradle",
                "/home/dev/repo/.gradle",
            ],
            uid=uid,
            gid=gid,
        ),
        call.config_device_remove("repo-child", "jailbee-warm", missing_ok=True),
        call.snapshot_delete("repo-source", "jailbee-warm-repo-child"),
    ]
    # Other read-only safety probes may appear between writes.
    writes = [c for c in incus.mock_calls if c in expected]
    assert writes == expected


def test_cp_failure_cleans_partial_and_continues(warm_rig):
    cfg, incus = warm_rig

    def execute(name, argv, **kwargs):
        if argv[0] == "cp" and argv[-1].endswith("app/build"):
            raise IncusError("no reflink")
        return "app/build/\0.gradle/\0" if argv[0] == "git" else ""

    incus.exec.side_effect = execute
    assert seed(warm_rig).seeded == [".gradle"]
    incus.exec.assert_any_call(
        "repo-child",
        ["rm", "-rf", "--", "/home/dev/repo/app/build"],
        uid=cfg.container_user.uid,
        gid=cfg.container_user.gid,
    )
    incus.config_device_remove.assert_called_once_with(
        "repo-child", "jailbee-warm", missing_ok=True
    )
    incus.snapshot_delete.assert_called_once()


def test_device_add_failure_still_deletes_snapshot(warm_rig):
    incus = warm_rig[1]
    incus.config_device_add.side_effect = IncusError("bad snapshot path")
    assert seed(warm_rig).skipped
    incus.snapshot_delete.assert_called_once_with("repo-source", "jailbee-warm-repo-child")
    assert not any(c.args[1][0] == "cp" for c in incus.exec.call_args_list)


def test_stale_snapshots_do_not_remove_another_active_fork(warm_rig):
    incus = warm_rig[1]
    incus.snapshot_list.return_value = [
        {"name": "jailbee-warm-repo-child"},
        {"name": "jailbee-warm-other"},
        {"name": "user-snap"},
    ]
    seed(warm_rig)
    assert incus.snapshot_delete.call_args_list == [
        call("repo-source", "jailbee-warm-repo-child"),
        call("repo-source", "jailbee-warm-repo-child"),
    ]


def test_unmatched_pattern_warns(warm_rig, mocker):
    warn = mocker.patch("jailbee.fork_warm.warn")
    assert seed(warm_rig, [".gradle", "tracked-file"]).seeded == [".gradle"]
    assert "tracked-file" in str(warn.call_args_list)


def test_never_raises(warm_rig):
    warm_rig[1].config_show.side_effect = RuntimeError("unexpected")
    assert seed(warm_rig).skipped


@pytest.mark.parametrize("entry", ["../outside/", "/absolute/", ".git/", "app/../../outside/"])
def test_unsafe_ignored_listing_skipped(warm_rig, entry):
    incus = warm_rig[1]
    incus.exec.return_value = entry + "\0.gradle/\0"
    assert seed(warm_rig, ["**"]).seeded == [".gradle"]


def test_snapshot_moved_after_preflight_skips_copy(warm_rig, mocker):
    incus = warm_rig[1]
    mocker.patch("jailbee.sync._container_head_oid", side_effect=["a" * 40, "b" * 40])
    assert seed(warm_rig).skipped
    assert not any(c.args[1][0] == "cp" for c in incus.exec.call_args_list)
    incus.snapshot_delete.assert_called_once()


def test_cleanup_failures_are_independent(warm_rig, mocker):
    warn = mocker.patch("jailbee.fork_warm.warn")
    incus = warm_rig[1]
    incus.config_device_remove.side_effect = IncusError("remove failed")
    assert seed(warm_rig).seeded == ["app/build", ".gradle"]
    incus.snapshot_delete.assert_called_once()
    assert warn.called
