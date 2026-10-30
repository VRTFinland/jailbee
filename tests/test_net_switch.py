from datetime import UTC, datetime

import pytest

from tests.conftest import make_cfg


def test_switch_mode_records_and_clears_expiry(tmp_path, mocker):
    from jailbee.net_switch import switch_mode

    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    switch = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch("jailbee.net_switch.mirror_endpoint_or_none", return_value=None)
    until = datetime(2026, 10, 30, 12, tzinfo=UTC)
    switch_mode(cfg, incus, "c", "loose", until=until)
    switch.assert_called_once_with(cfg, incus, "c", "loose", mirror_endpoint=None)
    incus.config_set.assert_any_call("c", "user.jailbee.loose_until", until.isoformat())
    switch_mode(cfg, incus, "c", "strict", until=None)
    incus.config_unset.assert_any_call("c", "user.jailbee.loose_until")


def test_switch_mode_failure_does_not_store_ttl(tmp_path, mocker):
    from jailbee.net_switch import switch_mode

    mocker.patch("jailbee.net_switch.mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.lifecycle.switch_network", side_effect=ValueError("failed"))
    incus = mocker.Mock()
    with pytest.raises(ValueError, match="failed"):
        switch_mode(make_cfg(tmp_path), incus, "c", "loose", until=None)
    incus.config_set.assert_not_called()
