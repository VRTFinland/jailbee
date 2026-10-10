"""Unit tests for timer-driven optional mount removal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from jailbee.incus import Incus
from jailbee.mount_revert import MountRevertResult, check_and_revert_mounts
from tests.conftest import make_cfg

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
PAST = (NOW - timedelta(minutes=1)).isoformat()
FUTURE = (NOW + timedelta(minutes=10)).isoformat()


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


def _raw(cfg, name="feat-x", *, labels=None, devices=(), base=True):
    prefix = cfg.container_prefix
    return {
        "name": f"{prefix}-{name}",
        "profiles": [f"{prefix}-base"] if base else ["default"],
        "config": {f"user.jailbee.mount_until.{k}": v for k, v in (labels or {}).items()},
        "devices": {f"optional-{d}": {"type": "disk"} for d in devices},
    }


def _incus(mocker, rows, *, held=False):
    incus = mocker.Mock(spec=Incus)
    incus.list_containers.return_value = rows
    incus.config_show.side_effect = lambda name: yaml.safe_dump(next(r for r in rows if r["name"] == name))
    mocker.patch("jailbee.mount_revert._autostart_holds", return_value=held)
    return incus


def test_no_labels_does_nothing(cfg, mocker):
    incus = _incus(mocker, [_raw(cfg, devices=("aws",))])
    assert check_and_revert_mounts(cfg, incus, now=NOW) == []
    incus.config_device_remove.assert_not_called()
    incus.config_unset_checked.assert_not_called()


def test_not_yet_expired_is_left(cfg, mocker):
    incus = _incus(mocker, [_raw(cfg, labels={"aws": FUTURE}, devices=("aws",))])
    assert check_and_revert_mounts(cfg, incus, now=NOW) == []
    incus.config_device_remove.assert_not_called()
    incus.config_unset_checked.assert_not_called()


@pytest.mark.parametrize("deadline", [PAST, NOW.isoformat()])
def test_expired_removes_device_then_label(cfg, mocker, deadline):
    row = _raw(cfg, labels={"aws": deadline}, devices=("aws",))
    incus = _incus(mocker, [row])
    assert check_and_revert_mounts(cfg, incus, now=NOW) == [
        MountRevertResult(container=row["name"], kind="aws", removed=True)
    ]
    assert [c[0] for c in incus.mock_calls if c[0] not in ("list_containers", "config_show")] == [
        "config_device_remove",
        "config_unset_checked",
    ]
    incus.config_device_remove.assert_called_once_with(row["name"], "optional-aws")
    incus.config_unset_checked.assert_called_once_with(row["name"], "user.jailbee.mount_until.aws")


def test_kind_no_longer_in_config_still_expires(cfg, mocker):
    row = _raw(cfg, labels={"retired": PAST}, devices=("retired",))
    incus = _incus(mocker, [row])
    out = check_and_revert_mounts(cfg, incus, now=NOW)
    assert out[0].removed is True
    incus.config_device_remove.assert_called_once_with(row["name"], "optional-retired")


def test_remove_failure_keeps_the_label_for_retry(cfg, mocker):
    row = _raw(cfg, labels={"aws": PAST}, devices=("aws",))
    incus = _incus(mocker, [row])
    incus.config_device_remove.side_effect = RuntimeError("busy")
    assert check_and_revert_mounts(cfg, incus, now=NOW) == [
        MountRevertResult(container=row["name"], kind="aws", removed=False, error="busy")
    ]
    incus.config_unset_checked.assert_not_called()


def test_orphan_label_without_device_is_cleaned(cfg, mocker):
    row = _raw(cfg, labels={"aws": FUTURE})
    incus = _incus(mocker, [row])
    assert check_and_revert_mounts(cfg, incus, now=NOW) == [
        MountRevertResult(container=row["name"], kind="aws", removed=False)
    ]
    incus.config_device_remove.assert_not_called()
    incus.config_unset_checked.assert_called_once_with(row["name"], "user.jailbee.mount_until.aws")


@pytest.mark.parametrize("value", ["tomorrow", None, 123, "2026-10-10T11:59:00"])
def test_malformed_label_is_cleaned_without_detaching(cfg, mocker, value):
    row = _raw(cfg, labels={"aws": value}, devices=("aws",))
    incus = _incus(mocker, [row])
    assert check_and_revert_mounts(cfg, incus, now=NOW) == []
    incus.config_device_remove.assert_not_called()
    incus.config_unset_checked.assert_called_once_with(row["name"], "user.jailbee.mount_until.aws")


def test_naive_label_does_not_block_another_expired_kind(cfg, mocker):
    row = _raw(cfg, labels={"aws": "2026-10-10T11:59:00", "ssh": PAST}, devices=("aws", "ssh"))
    incus = _incus(mocker, [row])
    assert check_and_revert_mounts(cfg, incus, now=NOW) == [
        MountRevertResult(container=row["name"], kind="ssh", removed=True)
    ]
    incus.config_device_remove.assert_called_once_with(row["name"], "optional-ssh")
    assert incus.config_unset_checked.call_args_list == [
        mocker.call(row["name"], "user.jailbee.mount_until.aws"),
        mocker.call(row["name"], "user.jailbee.mount_until.ssh"),
    ]


def test_autostart_hold_skips_the_container(cfg, mocker):
    incus = _incus(mocker, [_raw(cfg, labels={"aws": PAST}, devices=("aws",))], held=True)
    assert check_and_revert_mounts(cfg, incus, now=NOW) == []
    incus.config_device_remove.assert_not_called()
    incus.config_unset_checked.assert_not_called()


def test_other_repos_containers_are_ignored(cfg, mocker):
    incus = _incus(mocker, [_raw(cfg, labels={"aws": PAST}, devices=("aws",), base=False)])
    assert check_and_revert_mounts(cfg, incus, now=NOW) == []
    incus.config_device_remove.assert_not_called()
    incus.config_unset_checked.assert_not_called()


def test_one_failing_container_does_not_stop_the_next(cfg, mocker):
    bad = _raw(cfg, "a", labels={"aws": PAST}, devices=("aws",))
    good = _raw(cfg, "b", labels={"aws": PAST}, devices=("aws",))
    incus = _incus(mocker, [bad, good])
    mocker.patch("jailbee.mount_revert._autostart_holds", side_effect=[RuntimeError("boom"), False])
    out = check_and_revert_mounts(cfg, incus, now=NOW)
    assert any(r.container == bad["name"] and r.error == "boom" for r in out)
    assert any(r.container == good["name"] and r.removed for r in out)


def test_profiles_null_mid_destroy_is_skipped(cfg, mocker):
    row = _raw(cfg, labels={"aws": PAST}, devices=("aws",))
    row["profiles"] = None
    incus = _incus(mocker, [row])
    assert check_and_revert_mounts(cfg, incus, now=NOW) == []
