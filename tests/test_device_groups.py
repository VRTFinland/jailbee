"""Tests for host_devices group-membership provisioning."""

from jailbee.config import Config
from jailbee.device_groups import (
    ensure_device_groups,
    resolve_device_groups,
)


def _cfg(host_devices):
    return Config.model_validate({"host_devices": host_devices})


def test_resolve_uses_explicit_group():
    cfg = _cfg([{"path": "/dev/kvm", "group": "kvm"}])
    assert resolve_device_groups(cfg) == ["kvm"]


def test_resolve_auto_derives_from_host_source_group(mocker):
    mocker.patch("jailbee.device_groups.Path.exists", return_value=True)
    mocker.patch(
        "jailbee.device_groups._host_source_group",
        return_value="kvm",
    )
    cfg = _cfg([{"path": "/dev/kvm"}])
    assert resolve_device_groups(cfg) == ["kvm"]


def test_resolve_explicit_group_overrides_auto(mocker):
    # _host_source_group must not even be consulted when group is explicit.
    spy = mocker.patch(
        "jailbee.device_groups._host_source_group",
        return_value="kvm",
    )
    cfg = _cfg([{"path": "/dev/kvm", "group": "libvirt"}])
    assert resolve_device_groups(cfg) == ["libvirt"]
    spy.assert_not_called()


def test_resolve_skips_root_group(mocker):
    mocker.patch("jailbee.device_groups.Path.exists", return_value=True)
    mocker.patch(
        "jailbee.device_groups._host_source_group",
        return_value="root",
    )
    cfg = _cfg([{"path": "/dev/something"}])
    assert resolve_device_groups(cfg) == []


def test_resolve_skips_absent_source_when_auto(mocker):
    mocker.patch("jailbee.device_groups.Path.exists", return_value=False)
    cfg = _cfg([{"path": "/dev/nope"}])
    assert resolve_device_groups(cfg) == []


def test_resolve_dedupes_preserving_order(mocker):
    mocker.patch("jailbee.device_groups.Path.exists", return_value=True)
    mocker.patch(
        "jailbee.device_groups._host_source_group",
        return_value="kvm",
    )
    cfg = _cfg(
        [
            {"path": "/dev/kvm", "group": "kvm"},
            {"path": "/dev/vhost-net", "group": "kvm"},
            {"path": "/dev/net/tun", "group": "tun"},
        ]
    )
    assert resolve_device_groups(cfg) == ["kvm", "tun"]


def test_ensure_runs_usermod_and_reports_added(mocker):
    cfg = _cfg([{"path": "/dev/kvm", "group": "kvm"}])
    incus = mocker.Mock()
    incus.exec.return_value = "ADDED\n"
    added = ensure_device_groups(cfg, incus, "c1")
    assert added == ["kvm"]
    incus.exec.assert_called_once()
    name, cmd = incus.exec.call_args.args
    assert name == "c1"
    joined = " ".join(cmd)
    assert "getent group kvm" in joined
    assert "usermod -aG kvm dev" in joined


def test_ensure_skips_missing_group(mocker):
    cfg = _cfg([{"path": "/dev/kvm", "group": "kvm"}])
    incus = mocker.Mock()
    incus.exec.return_value = "NOGROUP\n"
    assert ensure_device_groups(cfg, incus, "c1") == []


def test_ensure_noop_when_no_host_devices(mocker):
    cfg = _cfg([])
    incus = mocker.Mock()
    assert ensure_device_groups(cfg, incus, "c1") == []
    incus.exec.assert_not_called()
