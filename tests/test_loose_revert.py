"""Unit tests for loose_revert.check_and_revert_loose."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jailbee.incus import Incus
from jailbee.loose_revert import RevertResult, check_and_revert_loose
from tests.conftest import make_cfg


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 20, 12, 10, 0, tzinfo=UTC)


def _container(prefix: str, name: str, *, profile_mode: str = "loose") -> dict:
    return {
        "name": f"{prefix}-{name}",
        "profiles": [f"{prefix}-base", f"{prefix}-net-{profile_mode}"],
    }


def test_disabled_policy_without_a_label_changes_nothing(tmp_path, mocker, now):
    """A disabled policy means gie schedules no TTL of its own — an unlabelled
    container is left alone, and no network switch happens."""
    cfg = make_cfg(tmp_path, loose_auto_revert={"enabled": False})
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    incus.config_get.return_value = None
    switch = mocker.patch("jailbee.loose_revert.switch_network")

    results = check_and_revert_loose(cfg, incus, now=now)

    assert results == []
    switch.assert_not_called()
    incus.config_unset.assert_not_called()


def test_expired_label_reverts_even_when_the_policy_is_disabled(tmp_path, mocker, now):
    """An explicit `gie net loose --for` beats the config switch.

    `_switch` writes `user.jailbee.loose_until` whenever the user names a TTL,
    regardless of policy. If the timer then ignored the label because the
    policy is off, `gie ls` and `gie net status` would count a TTL down to 0s
    on a container that stays loose forever — a lie in the unsafe direction.
    """
    cfg = make_cfg(tmp_path, loose_auto_revert={"enabled": False})
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    past = (now - timedelta(minutes=1)).isoformat()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": past,
        "user.jailbee.loose_revert_to": "strict",
        "user.jailbee.autostart_in_progress": None,
    }.get(key)
    switch = mocker.patch("jailbee.loose_revert.switch_network")
    mocker.patch(
        "jailbee.loose_revert.current_network_mode",
        return_value="loose",
    )

    results = check_and_revert_loose(cfg, incus, now=now)

    assert results == [RevertResult(container=f"{prefix}-feat-x", reverted_to="strict")]
    switch.assert_called_once_with(
        cfg,
        incus,
        f"{prefix}-feat-x",
        "strict",
        mirror_endpoint=None,
    )
    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.loose_until" in unset_keys


def test_skip_when_no_loose_until(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    incus.config_get.return_value = None
    results = check_and_revert_loose(cfg, incus, now=now)
    assert results == []


def test_skip_when_loose_until_in_future(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    future = (now + timedelta(minutes=3)).isoformat()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": future,
        "user.jailbee.autostart_in_progress": None,
    }.get(key)
    results = check_and_revert_loose(cfg, incus, now=now)
    assert results == []


def test_revert_to_strict_when_expired(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    past = (now - timedelta(minutes=1)).isoformat()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": past,
        "user.jailbee.loose_revert_to": "strict",
        "user.jailbee.autostart_in_progress": None,
    }.get(key)
    switch = mocker.patch("jailbee.loose_revert.switch_network")
    mocker.patch(
        "jailbee.loose_revert.current_network_mode",
        return_value="loose",
    )

    results = check_and_revert_loose(cfg, incus, now=now)

    assert results == [RevertResult(container=f"{prefix}-feat-x", reverted_to="strict")]
    switch.assert_called_once_with(
        cfg,
        incus,
        f"{prefix}-feat-x",
        "strict",
        mirror_endpoint=None,
    )
    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.loose_until" in unset_keys
    assert "user.jailbee.loose_revert_to" in unset_keys


def test_revert_to_strict_for_unrecognised_recorded_mode(tmp_path, mocker, now):
    """A recorded revert target that is not a real network mode must not wedge
    the timer: switch_network would raise ValueError on it, the surrounding
    except would log, and the container would be retried every tick forever."""
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    past = (now - timedelta(minutes=1)).isoformat()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": past,
        "user.jailbee.loose_revert_to": "bogus",
        "user.jailbee.autostart_in_progress": None,
    }.get(key)
    switch = mocker.patch("jailbee.loose_revert.switch_network")
    mocker.patch(
        "jailbee.loose_revert.current_network_mode",
        return_value="loose",
    )

    check_and_revert_loose(cfg, incus, now=now)

    switch.assert_called_once_with(
        cfg,
        incus,
        f"{prefix}-feat-x",
        "strict",
        mirror_endpoint=None,
    )


def test_clean_orphan_labels_when_not_loose(tmp_path, mocker, now):
    """User manually switched to strict but label cleanup failed →
    timer cleans the orphan without changing the network."""
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [
        _container(prefix, "feat-x", profile_mode="strict"),
    ]
    past = (now - timedelta(minutes=1)).isoformat()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": past,
        "user.jailbee.loose_revert_to": "strict",
        "user.jailbee.autostart_in_progress": None,
    }.get(key)
    switch = mocker.patch("jailbee.loose_revert.switch_network")
    mocker.patch(
        "jailbee.loose_revert.current_network_mode",
        return_value="strict",
    )

    results = check_and_revert_loose(cfg, incus, now=now)
    assert results == [RevertResult(container=f"{prefix}-feat-x", reverted_to=None)]
    switch.assert_not_called()
    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.loose_until" in unset_keys


def test_skip_when_autostart_in_progress(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    past = (now - timedelta(minutes=1)).isoformat()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": past,
        "user.jailbee.loose_revert_to": "strict",
        "user.jailbee.autostart_in_progress": "1",
    }.get(key)
    switch = mocker.patch("jailbee.loose_revert.switch_network")
    results = check_and_revert_loose(cfg, incus, now=now)
    assert results == []
    switch.assert_not_called()
    assert incus.config_unset.call_count == 0  # labels preserved


def test_switch_failure_keeps_labels(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    past = (now - timedelta(minutes=1)).isoformat()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": past,
        "user.jailbee.loose_revert_to": "strict",
        "user.jailbee.autostart_in_progress": None,
    }.get(key)
    mocker.patch(
        "jailbee.loose_revert.switch_network",
        side_effect=RuntimeError("incus down"),
    )
    mocker.patch(
        "jailbee.loose_revert.current_network_mode",
        return_value="loose",
    )

    results = check_and_revert_loose(cfg, incus, now=now)
    assert len(results) == 1
    assert results[0].reverted_to is None
    assert results[0].error == "incus down"
    assert incus.config_unset.call_count == 0  # retry next cycle


def test_corrupt_loose_until_clears_labels(tmp_path, mocker, now):
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": "not-a-timestamp",
        "user.jailbee.loose_revert_to": "strict",
        "user.jailbee.autostart_in_progress": None,
    }.get(key)
    switch = mocker.patch("jailbee.loose_revert.switch_network")

    check_and_revert_loose(cfg, incus, now=now)
    switch.assert_not_called()
    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.loose_until" in unset_keys


def test_skips_other_repos_containers(tmp_path, mocker, now):
    """Container without this repo's `<prefix>-base` profile is ignored."""
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [
        {
            "name": "other-feat-x",
            "profiles": ["other-base", "other-net-loose"],
        },
    ]
    results = check_and_revert_loose(cfg, incus, now=now)
    assert results == []
    incus.config_get.assert_not_called()


def test_skips_container_with_null_profiles(tmp_path, mocker, now):
    """A container mid-destroy can be reported with `"profiles": null`;
    skip it instead of crashing on `... not in None`."""
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [
        {"name": f"{cfg.container_prefix}-deleting", "profiles": None},
    ]
    results = check_and_revert_loose(cfg, incus, now=now)
    assert results == []
    incus.config_get.assert_not_called()


def test_stale_offline_label_reverts_to_strict(tmp_path, mocker, now):
    """The real migration guarantee: a label written before the offline mode
    was removed reverts the container to strict instead of wedging the timer."""
    cfg = make_cfg(tmp_path)
    prefix = cfg.container_prefix
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = [_container(prefix, "feat-x")]
    past = (now - timedelta(minutes=1)).isoformat()
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.loose_until": past,
        "user.jailbee.loose_revert_to": "offline",
        "user.jailbee.autostart_in_progress": None,
    }.get(key)
    switch = mocker.patch("jailbee.loose_revert.switch_network")
    mocker.patch(
        "jailbee.loose_revert.current_network_mode",
        return_value="loose",
    )

    check_and_revert_loose(cfg, incus, now=now)

    switch.assert_called_once_with(
        cfg,
        incus,
        f"{prefix}-feat-x",
        "strict",
        mirror_endpoint=None,
    )
