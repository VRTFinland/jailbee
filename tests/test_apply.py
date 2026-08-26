"""Tests for `gie apply` orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from jailbee.incus import Incus


@pytest.fixture(autouse=True)
def _no_mirror_lookup(mocker: MockerFixture) -> Any:
    """Default: mirror disabled. Override per-test by re-patching.

    Returns the `_mirror_endpoint_or_warn` patch object so a test that needs
    the real implementation to run can undo just this one patch via
    ``mocker.stop(...)`` — not ``mocker.stopall()``, which would also tear
    down every other autouse patch made through this test's `mocker`
    instance (e.g. conftest's runtime-mounts and kitty-autodetect fixtures).
    """
    endpoint_patch = mocker.patch(
        "jailbee.apply._mirror_endpoint_or_warn",
        return_value=None,
    )
    mocker.patch(
        "jailbee.apply._read_mirror_ca_or_warn",
        return_value=None,
    )
    return endpoint_patch


@pytest.fixture(autouse=True)
def _no_egress_refresh(mocker: MockerFixture) -> Any:
    """Default: refresh_pool is a no-op success. Override per-test by re-patching.

    Returns the mock so tests that need to alter the RefreshResult can
    update its `return_value`, e.g. to simulate dns_error or partial.
    """
    from jailbee.egress_pool import RefreshResult

    mocker.patch("jailbee.apply.get_engine")
    mocker.patch("jailbee.apply.register_repo")
    return mocker.patch(
        "jailbee.apply.refresh_pool",
        return_value=RefreshResult(container_prefix="X", status="ok"),
    )


@pytest.fixture(autouse=True)
def _no_container_acl_apply(mocker: MockerFixture) -> Any:
    """Default: per-container ACL re-materialisation is a no-op.

    `run_apply`'s per-container loop now calls
    `egress_scope.apply_container_acl`, which reads the container's
    `user.jailbee.egress_extra` label via `incus.config_get` — a call most
    `run_apply` tests here don't configure. Behaviour of
    `apply_container_acl` itself is covered by `tests/test_egress_scope.py`;
    the sweep it feeds is covered separately below (`_sweep_orphan_extra_acls`
    tests), which call it directly rather than through `run_apply`.
    """
    return mocker.patch("jailbee.egress_scope.apply_container_acl")


def test_profile_differs_returns_false_for_equivalent_yaml() -> None:
    from jailbee.apply import _profile_differs

    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.profile_show.return_value = (
        "name: foo\nconfig:\n  k: v\ndevices:\n  d1: {type: disk, source: /a, path: /b}\n"
    )
    new_yaml = (
        "name: foo\n"
        "devices:\n"
        "  d1:\n    type: disk\n    source: /a\n    path: /b\n"
        "config:\n  k: v\n"
    )
    assert _profile_differs(incus, "foo", new_yaml) is False


def test_profile_differs_returns_true_for_added_device() -> None:
    from jailbee.apply import _profile_differs

    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.profile_show.return_value = "name: foo\nconfig: {}\ndevices: {}\n"
    new_yaml = "name: foo\nconfig: {}\ndevices:\n  new: {type: disk, source: /a, path: /b}\n"
    assert _profile_differs(incus, "foo", new_yaml) is True


def test_acl_differs_returns_false_for_equivalent_yaml() -> None:
    from jailbee.apply import _acl_differs

    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_acl_show.return_value = (
        "name: a\negress:\n  - action: allow\n    destination: 1.2.3.4/32\n"
    )
    new_yaml = "name: a\negress: [{action: allow, destination: 1.2.3.4/32}]\n"
    assert _acl_differs(incus, "a", new_yaml) is False


def test_acl_differs_returns_true_for_changed_destination() -> None:
    from jailbee.apply import _acl_differs

    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_acl_show.return_value = (
        "name: a\negress:\n  - action: allow\n    destination: 1.2.3.4/32\n"
    )
    new_yaml = "name: a\negress:\n  - action: allow\n    destination: 5.6.7.8/32\n"
    assert _acl_differs(incus, "a", new_yaml) is True


def test_run_apply_noop_when_nothing_changed(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """All profiles + ACL identical, no running containers → empty result."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.profiles import (
        base_profile_yaml,
        binds_profile_yaml,
        net_profile_yaml,
        profile_names,
    )

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()  # mirror disabled by default
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    names = profile_names(cfg)
    profile_yamls = {
        names.base: base_profile_yaml(cfg),
        names.binds: binds_profile_yaml(cfg),
        names.net_strict: net_profile_yaml(cfg, "strict"),
        names.net_loose: net_profile_yaml(cfg, "loose"),
    }
    incus.profile_show.side_effect = lambda n: profile_yamls[n]

    mocker.patch("jailbee.apply._acl_differs", return_value=False)

    result = run_apply(
        cfg,
        incus,
        gcfg,
        assume_yes=False,
        no_restart=False,
        confirm_fn=lambda _msg: False,
    )

    assert result.profiles_changed == []
    assert sorted(result.profiles_unchanged) == sorted(profile_yamls)
    assert result.acl_changed is False
    assert result.restarted == []
    assert result.restart_failures == []
    assert incus.profile_set_yaml.call_count == 0
    assert incus.network_acl_set_yaml.call_count == 0


def test_run_apply_syncs_gie_skills_when_claude_enabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """run_apply refreshes the bundled gie skills into the shared dir."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from tests.conftest import with_agent

    cfg = make_cfg(tmp_path)
    cfg = with_agent(cfg, "claude", enabled=True)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    sync = mocker.patch("jailbee.claude_skills.sync_jailbee_skills")

    run_apply(cfg, incus, gcfg, confirm_fn=lambda _msg: False)

    sync.assert_called_once_with(cfg)


def test_run_apply_pushes_changed_profile(make_cfg, tmp_path: Path, mocker: MockerFixture) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.profiles import profile_names

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    names = profile_names(cfg)
    mocker.patch(
        "jailbee.apply._profile_differs",
        side_effect=lambda _i, n, _y: n == names.binds,
    )
    mocker.patch("jailbee.apply._acl_differs", return_value=False)

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert names.binds in result.profiles_changed
    assert incus.profile_set_yaml.call_count == 1
    pushed_name = incus.profile_set_yaml.call_args[0][0]
    assert pushed_name == names.binds


def test_run_apply_creates_user_shared_cache_dirs(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """run_apply creates user-defined `shared_caches` host_subpaths so a
    user enabling a custom bind after init doesn't hit a missing disk
    source path at profile-assign time."""
    from jailbee.apply import run_apply
    from jailbee.config import SharedCache
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(
        tmp_path,
        shared_dir=tmp_path / "shared",
        shared_caches=[
            SharedCache(
                name="pebble-oauth",
                host_subpath="pebble-oauth",
                container_path="~/.config/pebble/oauth",
            ),
        ],
    )
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)

    run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert (tmp_path / "shared" / "pebble-oauth").is_dir()


def test_run_apply_reports_acl_changed_when_pool_grew(
    make_cfg,
    tmp_path: Path,
    _no_egress_refresh: Any,
    mocker: MockerFixture,
) -> None:
    from jailbee.apply import run_apply
    from jailbee.egress_pool import RefreshResult
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    _no_egress_refresh.return_value = RefreshResult(
        container_prefix=cfg.container_prefix,
        status="ok",
        added=[("github.com", "1.1.1.1")],
    )
    mocker.patch("jailbee.apply._profile_differs", return_value=False)

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert result.acl_changed is True
    _no_egress_refresh.assert_called_once()


def test_run_apply_does_not_push_acl_directly(
    make_cfg,
    tmp_path: Path,
    _no_egress_refresh: Any,
    mocker: MockerFixture,
) -> None:
    """After the refresh_pool refactor, apply.run_apply itself never calls
    network_acl_set_yaml; the ACL write happens inside refresh_pool (mocked here).
    """
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert incus.network_acl_set_yaml.call_count == 0
    assert result.acl_changed is False  # default RefreshResult has no added/removed


def test_run_apply_repins_hosts_on_running_strict_only(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
            ContainerInfo(
                name="b",
                state="Running",
                network="loose",
                ip="10.0.0.2",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
            ContainerInfo(
                name="c",
                state="Stopped",
                network="strict",
                ip=None,
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    apply_hosts = mocker.patch("jailbee.hosts.apply_hosts")

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert result.hosts_repinned == ["a"]
    assert apply_hosts.call_count == 1
    assert apply_hosts.call_args[0][2] == "a"


def test_run_apply_passes_mirror_endpoint_to_apply_hosts_for_strict(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Strict containers need <mirror-ip> jailbee-registry-mirror.incus in
    /etc/hosts because incusbr0's dnsmasq can't see the mirror on
    jailbee-loose."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._mirror_endpoint_or_warn",
        return_value=("10.0.0.99", 3128),
    )
    mocker.patch(
        "jailbee.apply._read_mirror_ca_or_warn",
        return_value="CA",
    )
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    apply_hosts = mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch("jailbee.docker_daemon.apply_docker_proxy")

    run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert apply_hosts.call_count == 1
    assert apply_hosts.call_args.kwargs["mirror_endpoint"] == ("10.0.0.99", 3128)


def test_run_apply_passes_none_mirror_endpoint_to_apply_hosts_when_mirror_disabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """When the mirror is disabled, no row should be pinned."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    # autouse fixture leaves mirror disabled (returns None).
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    apply_hosts = mocker.patch("jailbee.hosts.apply_hosts")

    run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert apply_hosts.call_count == 1
    assert apply_hosts.call_args.kwargs.get("mirror_endpoint") is None


def test_run_apply_reapplies_docker_proxy_when_mirror_enabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    # Override the autouse `_no_mirror_lookup` fixture: mirror IS enabled.
    mocker.patch(
        "jailbee.apply._mirror_endpoint_or_warn",
        return_value=("10.0.0.99", 3128),
    )
    mocker.patch(
        "jailbee.apply._read_mirror_ca_or_warn",
        return_value="CA",
    )
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
            ContainerInfo(
                name="b",
                state="Running",
                network="loose",
                ip="10.0.0.2",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    apply_proxy = mocker.patch("jailbee.docker_daemon.apply_docker_proxy")

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    # Every running container gets the proxy: strict needs it to reach
    # upstreams under its ACL, loose gets it for caching.
    assert result.docker_proxy_reapplied == ["a", "b"]
    assert apply_proxy.call_count == 2


def test_run_apply_skips_docker_proxy_when_mirror_disabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()  # autouse fixture forces mirror disabled
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    apply_proxy = mocker.patch("jailbee.docker_daemon.apply_docker_proxy")

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert result.docker_proxy_reapplied == []
    assert apply_proxy.call_count == 0


def test_run_apply_no_prompt_when_no_profile_changed(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """ACL changed but profiles identical → no restart prompt."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=True)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")

    confirm = mocker.MagicMock(return_value=True)
    result = run_apply(cfg, incus, gcfg, confirm_fn=confirm)

    assert confirm.call_count == 0
    assert result.restarted == []


def test_run_apply_prompts_when_profile_changed_and_running(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.profiles import profile_names

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    names = profile_names(cfg)
    mocker.patch(
        "jailbee.apply._profile_differs",
        side_effect=lambda _i, n, _y: n == names.binds,
    )
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch("jailbee.apply._restart_one")

    confirm = mocker.MagicMock(return_value=True)
    run_apply(cfg, incus, gcfg, confirm_fn=confirm)

    assert confirm.call_count == 1


def test_run_apply_user_declines_restart(make_cfg, tmp_path: Path, mocker: MockerFixture) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.profiles import profile_names

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    names = profile_names(cfg)
    mocker.patch(
        "jailbee.apply._profile_differs",
        side_effect=lambda _i, n, _y: n == names.binds,
    )
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    restart_one = mocker.patch("jailbee.apply._restart_one")

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert result.restarted == []
    assert restart_one.call_count == 0


def test_run_apply_assume_yes_skips_prompt(make_cfg, tmp_path: Path, mocker: MockerFixture) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.profiles import profile_names

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    names = profile_names(cfg)
    mocker.patch(
        "jailbee.apply._profile_differs",
        side_effect=lambda _i, n, _y: n == names.binds,
    )
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    restart_one = mocker.patch("jailbee.apply._restart_one")

    confirm = mocker.MagicMock()
    run_apply(cfg, incus, gcfg, assume_yes=True, confirm_fn=confirm)

    assert confirm.call_count == 0
    assert restart_one.call_count == 1


def test_run_apply_no_restart_flag_skips_prompt_and_restart(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.profiles import profile_names

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    names = profile_names(cfg)
    mocker.patch(
        "jailbee.apply._profile_differs",
        side_effect=lambda _i, n, _y: n == names.binds,
    )
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    restart_one = mocker.patch("jailbee.apply._restart_one")

    confirm = mocker.MagicMock()
    result = run_apply(
        cfg,
        incus,
        gcfg,
        no_restart=True,
        confirm_fn=confirm,
    )

    assert confirm.call_count == 0
    assert restart_one.call_count == 0
    assert result.restarted == []


def test_run_apply_partial_restart_failure(make_cfg, tmp_path: Path, mocker: MockerFixture) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import IncusError
    from jailbee.lifecycle import ContainerInfo
    from jailbee.profiles import profile_names

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    names = profile_names(cfg)
    mocker.patch(
        "jailbee.apply._profile_differs",
        side_effect=lambda _i, n, _y: n == names.binds,
    )
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
            ContainerInfo(
                name="b",
                state="Running",
                network="strict",
                ip="10.0.0.2",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
            ContainerInfo(
                name="c",
                state="Running",
                network="strict",
                ip="10.0.0.3",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")

    def fake_restart(_cfg, _incus, name, **_kwargs):
        if name == "b":
            raise IncusError("boom")

    mocker.patch(
        "jailbee.apply._restart_one",
        side_effect=fake_restart,
    )

    result = run_apply(cfg, incus, gcfg, assume_yes=True)

    assert result.restarted == ["a", "c"]
    assert result.restart_failures == [("b", "boom")]
    assert result.fully_successful is False


def test_run_apply_all_restarts_succeed_is_fully_successful(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.profiles import profile_names

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    names = profile_names(cfg)
    mocker.patch(
        "jailbee.apply._profile_differs",
        side_effect=lambda _i, n, _y: n == names.binds,
    )
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name="a",
                state="Running",
                network="strict",
                ip="10.0.0.1",
                memory_limit="16GiB",
                repo=tmp_path.name,
            ),
        ],
    )
    mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch("jailbee.apply._restart_one")

    result = run_apply(cfg, incus, gcfg, assume_yes=True)

    assert result.restarted == ["a"]
    assert result.restart_failures == []
    assert result.fully_successful is True


def test_run_apply_dns_failure_aborts_before_incus_calls(
    make_cfg, tmp_path: Path, _no_egress_refresh: Any, mocker: MockerFixture
) -> None:
    from jailbee.apply import run_apply
    from jailbee.egress import NetworkResolveError
    from jailbee.egress_pool import RefreshResult
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []

    _no_egress_refresh.return_value = RefreshResult(
        container_prefix=cfg.container_prefix,
        status="dns_error",
        error="github.com: getaddrinfo: -3",
    )

    with pytest.raises(NetworkResolveError):
        run_apply(cfg, incus, gcfg)

    assert incus.profile_set_yaml.call_count == 0
    assert incus.network_acl_set_yaml.call_count == 0


def test_run_apply_continues_when_the_mirror_endpoint_cannot_be_resolved(
    make_cfg,
    tmp_path: Path,
    mocker: MockerFixture,
    _no_mirror_lookup: Any,
    _no_egress_refresh: Any,
) -> None:
    """Replaces the old abort test: Task 5 deliberately removed the abort
    on a mirror-endpoint failure, so `apply` must now degrade to a warning
    (via `_mirror_endpoint_or_warn`) and continue to its normal work rather
    than raise. This exercises the real `_mirror_endpoint_or_warn`, not a
    mock standing in for it, so it fails if that catch is ever removed."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    # Let the real `_mirror_endpoint_or_warn` run instead of the autouse
    # fixture's blanket mock of it (targeted `.stop()`, not
    # `mocker.stopall()` — see `_no_mirror_lookup`'s docstring).
    mocker.stop(_no_mirror_lookup)
    mocker.patch("jailbee.docker_daemon.mirror_wanted", return_value=True)
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        side_effect=ValueError(
            "jailbee-registry-mirror container not found. Run 'jailbee registry up' first."
        ),
    )
    # Unrelated to the mirror: unlike the old abort test, this one now
    # reaches the profile/ACL diff step. `incus.profile_show` on a bare
    # `MagicMock(spec=Incus)` returns a MagicMock, and feeding that into
    # `_profile_differs`'s `yaml.safe_load` sends PyYAML's reader into an
    # unbounded loop reading "chunks" off a mock. Short-circuit the diff
    # itself, same as most other `run_apply` tests in this file that don't
    # care about profile/ACL content.
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch("jailbee.apply._list_containers", return_value=[])

    run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    # No exception escaped `run_apply` above; confirm it actually reached
    # its normal work past the mirror lookup rather than short-circuiting.
    _no_egress_refresh.assert_called_once()


def test_run_apply_pushes_extra_registries_to_mirror_when_enabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """`gie apply` must extend the mirror's REGISTRIES with the repo's
    extras so ECR (etc.) pulls hit the cache from then on."""
    from jailbee.apply import run_apply
    from jailbee.config import DockerRegistryMirrorRepoConfig
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    object.__setattr__(
        cfg,
        "docker_registry_mirror",
        DockerRegistryMirrorRepoConfig(
            extra_registries=["803520778560.dkr.ecr.eu-north-1.amazonaws.com"]
        ),
    )
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._mirror_endpoint_or_warn",
        return_value=("10.0.0.99", 3128),
    )
    mocker.patch(
        "jailbee.apply._read_mirror_ca_or_warn",
        return_value="CA",
    )
    mocker.patch("jailbee.apply._list_containers", return_value=[])
    apply_registries = mocker.patch("jailbee.registry.apply_mirror_registries")

    run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    apply_registries.assert_called_once_with(
        incus, ["803520778560.dkr.ecr.eu-north-1.amazonaws.com"]
    )


def test_restart_one_runs_autostart_on_start(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Regression: `gie apply` restart must run on_start autostart.

    Otherwise the container comes back without services (docker daemon,
    pnpm dev, etc.) and is effectively unusable until the user manually
    runs `gie restart`.
    """
    from jailbee.apply import _restart_one
    from jailbee.autostart import AutostartTrigger

    cfg = make_cfg(tmp_path)
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []

    mocker.patch("jailbee.lifecycle.restart_container")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    apply_hosts = mocker.patch("jailbee.hosts.apply_hosts")
    run_autostart = mocker.patch("jailbee.autostart.run_autostart")

    _restart_one(cfg, incus, "a")

    apply_hosts.assert_not_called()  # loose mode
    run_autostart.assert_called_once()
    call = run_autostart.call_args
    assert call.args[2] == "a"
    assert call.args[3] is AutostartTrigger.ON_START
    assert call.kwargs["repo_dir"] == "/home/dev/repo"


def test_restart_one_pins_hosts_in_strict_mode(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Strict-mode containers also get /etc/hosts re-pinned before autostart."""
    from jailbee.apply import _restart_one

    cfg = make_cfg(tmp_path)
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []

    mocker.patch("jailbee.lifecycle.restart_container")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    apply_hosts = mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch("jailbee.autostart.run_autostart")

    _restart_one(cfg, incus, "a", mirror_endpoint=("10.0.0.5", 5000))

    apply_hosts.assert_called_once()
    assert apply_hosts.call_args.kwargs["mirror_endpoint"] == ("10.0.0.5", 5000)


def test_restart_one_does_not_launch_gui_apps(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """`gie apply` must not pop up Chrome/IDE on every restarted container.

    Only the shell-step autostart runs; GUI launchers live in
    cli._post_start_actions and are exclusive to `gie start` / `gie restart`.
    """
    from jailbee.apply import _restart_one

    cfg = make_cfg(tmp_path)
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []

    mocker.patch("jailbee.lifecycle.restart_container")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.autostart.run_autostart")
    open_chrome = mocker.patch("jailbee.gui.open_chrome")
    open_ide = mocker.patch("jailbee.gui.open_ide")

    _restart_one(cfg, incus, "a")

    open_chrome.assert_not_called()
    open_ide.assert_not_called()


def test_run_apply_creates_claude_shared_dir_when_enabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Enabling claude after the initial `gie init` means the next
    `gie apply` must create `<shared_dir>/claude`, `<shared_dir>/claude-install`
    and seed `<shared_dir>/claude/.claude.json` so the binds profile's dir
    mounts have valid sources. Otherwise Incus refuses to start (or to
    accept the profile edit/assign for) the container with a "Missing
    source path ... for disk shared-claude-install" error.
    """
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    shared = tmp_path / "shared"
    cfg = make_cfg(tmp_path, shared_dir=shared, claude={"enabled": True})
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch("jailbee.apply._list_containers", return_value=[])

    assert not (shared / "claude").exists()
    assert not (shared / "claude-install").exists()
    assert not (shared / "claude" / ".claude.json").exists()

    run_apply(cfg, incus, gcfg, assume_yes=True, confirm_fn=lambda _m: False)

    assert (shared / "claude").is_dir()
    # The shared version store (`~/.local/share/claude` inside the container)
    # — the disk device whose missing source path broke `gie apply`/assign.
    assert (shared / "claude-install").is_dir()
    assert (shared / "claude" / ".claude.json").read_text() == "{}\n"
    # The rest of the shared-dir tree should also be present.
    assert (shared / "caches" / "pnpm-store").is_dir()


def test_run_apply_does_not_create_claude_shared_dir_when_disabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """When `claude.enabled=false`, the binds profile has no claude
    mounts, so `gie apply` must NOT create the claude subdir/json
    file. The non-claude shared-dir tree (caches/pnpm-store etc.)
    must still be created.
    """
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    shared = tmp_path / "shared"
    cfg = make_cfg(tmp_path, shared_dir=shared, claude={"enabled": False})
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch("jailbee.apply._list_containers", return_value=[])

    run_apply(cfg, incus, gcfg, assume_yes=True, confirm_fn=lambda _m: False)

    assert not (shared / "claude").exists()
    assert not (shared / "claude-install").exists()
    assert not (shared / "claude" / ".claude.json").exists()
    # The rest of the shared-dir tree IS still created.
    assert (shared / "caches" / "pnpm-store").is_dir()
    assert (shared / "ssh").is_dir()


def test_run_apply_creates_jetbrains_shared_dirs_when_enabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Enabling jetbrains after the initial `gie init` (or upgrading gie
    while the share_idea cache is new) means the next `gie apply` must
    create `<shared_dir>/jetbrains-{config,data,idea}` so the binds
    profile + per-container under-repo attach have valid sources.
    Otherwise Incus refuses to add the disk device with
    "Missing source path".
    """
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    shared = tmp_path / "shared"
    cfg = make_cfg(tmp_path, shared_dir=shared, jetbrains={"enabled": True})
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch("jailbee.apply._list_containers", return_value=[])

    assert not (shared / "jetbrains-config").exists()
    assert not (shared / "jetbrains-data").exists()
    assert not (shared / "jetbrains-idea").exists()

    run_apply(cfg, incus, gcfg, assume_yes=True, confirm_fn=lambda _m: False)

    assert (shared / "jetbrains-config").is_dir()
    assert (shared / "jetbrains-data").is_dir()
    assert (shared / "jetbrains-idea").is_dir()


def test_run_apply_does_not_create_jetbrains_idea_when_share_idea_off(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """share_idea=False suppresses just the .idea subdir; config/data
    still appear because jetbrains.enabled is still on."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    shared = tmp_path / "shared"
    cfg = make_cfg(
        tmp_path,
        shared_dir=shared,
        jetbrains={"enabled": True, "share_idea": False},
    )
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch("jailbee.apply._list_containers", return_value=[])

    run_apply(cfg, incus, gcfg, assume_yes=True, confirm_fn=lambda _m: False)

    assert (shared / "jetbrains-config").is_dir()
    assert (shared / "jetbrains-data").is_dir()
    assert not (shared / "jetbrains-idea").exists()


def test_run_apply_does_not_create_jetbrains_dirs_when_disabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """jetbrains.enabled=False suppresses all jetbrains subdirs."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    shared = tmp_path / "shared"
    cfg = make_cfg(tmp_path, shared_dir=shared, jetbrains={"enabled": False})
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch("jailbee.apply._list_containers", return_value=[])

    run_apply(cfg, incus, gcfg, assume_yes=True, confirm_fn=lambda _m: False)

    assert not (shared / "jetbrains-config").exists()
    assert not (shared / "jetbrains-data").exists()
    assert not (shared / "jetbrains-idea").exists()


def test_run_apply_skips_extra_registries_when_mirror_disabled(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Mirror not running → nothing to push to. Skip silently."""
    from jailbee.apply import run_apply
    from jailbee.config import DockerRegistryMirrorRepoConfig
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    object.__setattr__(
        cfg,
        "docker_registry_mirror",
        DockerRegistryMirrorRepoConfig(extra_registries=["ecr.example.com"]),
    )
    gcfg = GlobalConfig()  # autouse fixture forces mirror disabled
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.network_get.return_value = ""

    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch("jailbee.apply._list_containers", return_value=[])
    apply_registries = mocker.patch("jailbee.registry.apply_mirror_registries")

    run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    apply_registries.assert_not_called()


def test_run_apply_migrates_offline_container_to_strict(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A container still on <prefix>-net-offline is reassigned to net-strict.

    switch_network cannot do this: once `offline` leaves net_by_mode the
    profile is unrecognised and it raises "has no network profile attached".
    """
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.profiles import profile_names

    cfg = make_cfg(tmp_path)
    names = profile_names(cfg)
    stale = f"{cfg.container_prefix}-net-offline"
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = [
        {
            "name": f"{cfg.container_prefix}-feat-x",
            "status": "Stopped",
            "profiles": ["default", names.base, names.binds, stale],
            "state": None,
            "config": {},
        }
    ]
    incus.network_get.return_value = ""
    incus.profile_exists.return_value = True
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)

    result = run_apply(cfg, incus, GlobalConfig(), assume_yes=True)

    incus.profile_assign.assert_called_once_with(
        f"{cfg.container_prefix}-feat-x",
        ["default", names.base, names.binds, names.net_strict],
    )
    incus.profile_delete.assert_called_once_with(stale)
    assert result.offline_migrated == [f"{cfg.container_prefix}-feat-x"]


def test_run_apply_deletes_offline_profile_with_no_containers_on_it(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Nothing to migrate, but the leftover profile is still removed."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    incus.profile_exists.return_value = True
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)

    result = run_apply(cfg, incus, GlobalConfig(), assume_yes=True)

    incus.profile_assign.assert_not_called()
    incus.profile_delete.assert_called_once_with(f"{cfg.container_prefix}-net-offline")
    assert result.offline_migrated == []


def test_run_apply_skips_offline_profile_delete_when_absent(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A repo that never had the profile must not see a delete attempt."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig

    cfg = make_cfg(tmp_path)
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    incus.profile_exists.return_value = False
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)

    run_apply(cfg, incus, GlobalConfig(), assume_yes=True)

    incus.profile_delete.assert_not_called()


def test_run_apply_warns_but_continues_when_offline_profile_delete_fails(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A stuck profile must not abort apply — the rest of the run still matters."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import IncusError

    cfg = make_cfg(tmp_path)
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    incus.profile_exists.return_value = True
    incus.profile_delete.side_effect = IncusError("profile is currently in use")
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    warn = mocker.patch("jailbee.tui.warn")

    result = run_apply(cfg, incus, GlobalConfig(), assume_yes=True)

    assert result.fully_successful is True
    assert any("net-offline" in str(c.args[0]) for c in warn.call_args_list)


def test_run_apply_reconciles_port_forwards_on_every_container(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Stopped containers are reconciled too: a proxy device on a stopped
    container takes effect on its next boot, so skipping it hides drift."""
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.ports import ReconcileResult

    cfg = make_cfg(tmp_path, host_ports=[{"name": "adb", "port": 5037}])
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch("jailbee.hosts.apply_hosts")
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name=f"{cfg.container_prefix}-a",
                state="Running",
                network="strict",
                ip=None,
                memory_limit=None,
                repo=cfg.container_prefix,
            ),
            ContainerInfo(
                name=f"{cfg.container_prefix}-b",
                state="Stopped",
                network="strict",
                ip=None,
                memory_limit=None,
                repo=cfg.container_prefix,
            ),
        ],
    )
    reconcile = mocker.patch(
        "jailbee.ports.reconcile_config_ports",
        side_effect=[
            ReconcileResult(added=["port-cfg-adb"], replaced=[], removed=[]),
            ReconcileResult(added=[], replaced=[], removed=[]),
        ],
    )

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert reconcile.call_count == 2
    assert [c.args[2] for c in reconcile.call_args_list] == [
        f"{cfg.container_prefix}-a",
        f"{cfg.container_prefix}-b",
    ]
    assert result.ports_changed == [f"{cfg.container_prefix}-a"]


def test_run_apply_reconciles_even_with_no_host_ports(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Deleting the last host_ports entry must still clean up its device.

    So reconciliation is unconditional: gating it on `cfg.host_ports` would
    strand a `port-cfg-*` device forever.
    """
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.ports import ReconcileResult

    cfg = make_cfg(tmp_path)  # no host_ports at all
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name=f"{cfg.container_prefix}-a",
                state="Stopped",
                network="strict",
                ip=None,
                memory_limit=None,
                repo=cfg.container_prefix,
            ),
        ],
    )
    reconcile = mocker.patch(
        "jailbee.ports.reconcile_config_ports",
        return_value=ReconcileResult(added=[], replaced=[], removed=["port-cfg-gone"]),
    )

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    assert reconcile.call_count == 1
    assert result.ports_changed == [f"{cfg.container_prefix}-a"]


def test_run_apply_reports_and_continues_on_a_port_forward_failure(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A translated port-forward failure on one container must not abort the
    rest of the sweep: `apply` is asked to reconcile every container of the
    repo, and one container's proxy device being refused (e.g. something
    already listening on its container-side port) must not block the
    profile/ACL/hosts work already done this run for the others. Mirrors
    `restart_failures`'s report-and-continue behaviour below.
    """
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.ports import PortError, ReconcileResult

    cfg = make_cfg(tmp_path, host_ports=[{"name": "adb", "port": 5037}])
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name=f"{cfg.container_prefix}-a",
                state="Stopped",
                network="strict",
                ip=None,
                memory_limit=None,
                repo=cfg.container_prefix,
            ),
            ContainerInfo(
                name=f"{cfg.container_prefix}-b",
                state="Stopped",
                network="strict",
                ip=None,
                memory_limit=None,
                repo=cfg.container_prefix,
            ),
        ],
    )
    mocker.patch("jailbee.ports.list_forwards", return_value={})
    reconcile = mocker.patch(
        "jailbee.ports.reconcile_config_ports",
        side_effect=[
            PortError("something is already listening on port 5037 inside the container"),
            ReconcileResult(added=["port-cfg-adb"], replaced=[], removed=[]),
        ],
    )

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    # Both containers were attempted — the first's failure did not stop
    # the loop before it reached the second.
    assert reconcile.call_count == 2
    assert result.port_failures == [
        (
            f"{cfg.container_prefix}-a",
            "something is already listening on port 5037 inside the container",
        )
    ]
    assert result.ports_changed == [f"{cfg.container_prefix}-b"]
    assert result.fully_successful is False


def test_run_apply_reconciles_ports_from_one_prefetched_call(
    make_cfg, tmp_path: Path, mocker: MockerFixture
) -> None:
    """`apply` fetches every container's forwards in a single `list_forwards`
    call and hands each container its own slice via `reconcile_config_ports`'s
    `forwards` kwarg — instead of that function re-querying Incus (via
    `forwards_for`) once per container.
    """
    from jailbee.apply import run_apply
    from jailbee.global_config import GlobalConfig
    from jailbee.lifecycle import ContainerInfo
    from jailbee.ports import Endpoint, Forward

    cfg = make_cfg(tmp_path, host_ports=[{"name": "adb", "port": 5037}])
    gcfg = GlobalConfig()
    incus = MagicMock(spec=Incus)
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_get.return_value = ""
    mocker.patch("jailbee.apply._profile_differs", return_value=False)
    mocker.patch("jailbee.apply._acl_differs", return_value=False)
    mocker.patch(
        "jailbee.apply._list_containers",
        return_value=[
            ContainerInfo(
                name=f"{cfg.container_prefix}-a",
                state="Stopped",
                network="strict",
                ip=None,
                memory_limit=None,
                repo=cfg.container_prefix,
            ),
        ],
    )
    fwd = Forward(
        device="port-cfg-adb",
        direction="to-container",
        proto="tcp",
        container=Endpoint(proto="tcp", address="127.0.0.1", port=5037, raw="tcp:127.0.0.1:5037"),
        host=Endpoint(proto="tcp", address="127.0.0.1", port=5037, raw="tcp:127.0.0.1:5037"),
        source="config",
    )
    list_forwards = mocker.patch(
        "jailbee.ports.list_forwards",
        return_value={f"{cfg.container_prefix}-a": [fwd]},
    )
    forwards_for = mocker.patch("jailbee.ports.forwards_for")

    result = run_apply(cfg, incus, gcfg, confirm_fn=lambda _m: False)

    list_forwards.assert_called_once_with(incus, [f"{cfg.container_prefix}-a"])
    # The already-matching forward was handed in directly, so
    # `reconcile_config_ports` never fell back to its own per-container query.
    forwards_for.assert_not_called()
    assert result.ports_changed == []


def test_apply_warns_and_continues_when_the_mirror_is_down(
    tmp_path, mocker, _no_mirror_lookup: Any
):
    """`apply` is the repair command; it must not die on the one thing the
    user might be running it to fix. The CA half already warns
    (`_read_mirror_ca_or_warn`) — the endpoint half must match."""
    from jailbee import apply as apply_mod
    from jailbee.global_config import DockerRegistryMirror, GlobalConfig
    from tests.conftest import make_cfg

    # The autouse `_no_mirror_lookup` fixture above blanket-mocks this exact
    # function to return_value=None so every other test in this module gets
    # "mirror disabled" for free. This test is testing that function's own
    # body, so it must undo just that one patch (not `mocker.stopall()`,
    # which would also tear down conftest's other autouse patches, e.g.
    # runtime-mounts and kitty-autodetect) before patching the two things
    # the real implementation actually calls.
    mocker.stop(_no_mirror_lookup)
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint",
        side_effect=ValueError("jailbee-registry-mirror container not found."),
    )
    # Patch the source module, not captured output: `warn` prints through a
    # Rich console that hard-wraps at 80 columns, so a substring assertion
    # would depend on terminal width. The helper imports `warn` lazily, so
    # this patch is what it resolves.
    warn = mocker.patch("jailbee.tui.warn")
    cfg = make_cfg(tmp_path / "repo", golden={"stacks": {"docker": True}})
    gcfg = GlobalConfig(
        docker_registry_mirror=DockerRegistryMirror(data_dir=tmp_path / "registry"),
    )

    assert apply_mod._mirror_endpoint_or_warn(cfg, mocker.MagicMock(), gcfg) is None
    warn.assert_called_once()


def test_sweep_orphan_extra_acls_deletes_acls_with_no_container(make_cfg, tmp_path, mocker):
    from jailbee.apply import _sweep_orphan_extra_acls

    cfg = make_cfg(tmp_path / "myrepo")
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": "myrepo-live", "status": "Running", "profiles": [], "config": {}, "devices": {}}
    ]
    incus.network_acl_list.return_value = [
        "myrepo-allowlist",
        "myrepo-live-extra",
        "myrepo-gone-extra",
        "other-repo-x-extra",
    ]

    deleted = _sweep_orphan_extra_acls(cfg, incus)

    assert deleted == ["myrepo-gone-extra"]
    incus.network_acl_delete.assert_called_once_with("myrepo-gone-extra")


def test_sweep_leaves_another_repos_extra_acls_alone(make_cfg, tmp_path, mocker):
    from jailbee.apply import _sweep_orphan_extra_acls

    cfg = make_cfg(tmp_path / "myrepo")
    incus = mocker.MagicMock()
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = ["other-repo-x-extra"]

    assert _sweep_orphan_extra_acls(cfg, incus) == []
    incus.network_acl_delete.assert_not_called()
