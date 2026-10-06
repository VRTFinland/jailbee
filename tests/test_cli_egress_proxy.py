"""`jailbee net egress proxy up|down|status` — the shared Squid proxy's own lifecycle."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.egress_proxy import ProxyStatus
from jailbee.incus import IncusError
from tests.conftest import make_cfg

runner = CliRunner()


@pytest.fixture
def host(mocker, tmp_path):
    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    return cfg


@pytest.mark.parametrize("prefix", [["net", "egress"], ["egress"]])
def test_up_starts_the_proxy_without_recreating(mocker, host, prefix):
    up = mocker.patch("jailbee.egress_proxy.proxy_up")
    result = runner.invoke(app, [*prefix, "proxy", "up"])
    assert result.exit_code == 0, result.output
    assert up.call_args.kwargs["recreate"] is False
    assert up.call_args.kwargs["storage_pool"] is None
    assert callable(up.call_args.kwargs["on_step"])


def test_up_recreate_is_passed_through(mocker, host):
    up = mocker.patch("jailbee.egress_proxy.proxy_up")
    result = runner.invoke(app, ["net", "egress", "proxy", "up", "--recreate"])
    assert result.exit_code == 0, result.output
    assert up.call_args.kwargs["recreate"] is True


def test_up_creates_on_the_global_service_pool(mocker, host):
    host._service_storage_pool = "cow"
    up = mocker.patch("jailbee.egress_proxy.proxy_up")
    result = runner.invoke(app, ["net", "egress", "proxy", "up"])
    assert result.exit_code == 0, result.output
    assert up.call_args.kwargs["storage_pool"] == "cow"


def test_up_reports_an_expected_failure_without_a_traceback(mocker, host):
    mocker.patch("jailbee.egress_proxy.proxy_up", side_effect=IncusError("apt timed out"))
    result = runner.invoke(app, ["net", "egress", "proxy", "up"])
    assert result.exit_code == 1
    assert "apt timed out" in result.output
    assert "Traceback" not in result.output


def test_down_stops_the_proxy(mocker, host):
    down = mocker.patch("jailbee.egress_proxy.proxy_down")
    result = runner.invoke(app, ["net", "egress", "proxy", "down"])
    assert result.exit_code == 0, result.output
    down.assert_called_once()
    assert "stopped" in result.output


def test_status_prints_the_state(mocker, host):
    mocker.patch("jailbee.egress_proxy.proxy_status", return_value=ProxyStatus.RUNNING)
    result = runner.invoke(app, ["net", "egress", "proxy", "status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output
