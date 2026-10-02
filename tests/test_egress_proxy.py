"""Tests for the Squid egress-proxy container lifecycle (Incus mocked)."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
import yaml

from jailbee import egress_proxy
from jailbee.egress_proxy import (
    CLIENT_BRIDGES,
    PROXY_CONTAINER,
    PROXY_PROFILE,
    ProxyStatus,
    client_endpoints,
    endpoint_for_bridge,
    proxy_status,
    proxy_up,
)
from jailbee.egress_proxy_render import BASE_SQUID_CONF
from jailbee.incus import IncusError
from jailbee.network_generation import WORK_BRIDGE
from jailbee.services_acl import EGRESS_PROXY_LABEL


def test_constants():
    assert PROXY_CONTAINER == "jailbee-egress-proxy"
    assert PROXY_PROFILE == "jailbee-egress-proxy-profile"
    assert CLIENT_BRIDGES == ("incusbr0", WORK_BRIDGE)


def _status_incus(status: str | None, active: bool = True) -> MagicMock:
    incus = MagicMock()
    incus.list_containers.return_value = (
        [] if status is None else [{"name": PROXY_CONTAINER, "status": status}]
    )
    if active:
        incus.exec.return_value = "active\n"
    else:
        incus.exec.side_effect = IncusError("inactive")
    return incus


def test_status_running():
    assert proxy_status(_status_incus("Running")) == ProxyStatus.RUNNING


def test_status_degraded():
    assert proxy_status(_status_incus("Running", active=False)) == ProxyStatus.DEGRADED


def test_status_stopped_does_not_probe():
    incus = _status_incus("Stopped")
    assert proxy_status(incus) == ProxyStatus.STOPPED
    incus.exec.assert_not_called()


def test_status_missing():
    incus = _status_incus(None)
    assert proxy_status(incus) == ProxyStatus.MISSING
    incus.exec.assert_not_called()


def test_status_probe_uses_timeout_10():
    incus = _status_incus("Running")
    proxy_status(incus)
    assert incus.exec.call_args.kwargs["timeout"] == 10
    assert incus.exec.call_args.args[1] == ["systemctl", "is-active", "squid"]


class Rig:
    """A mocked Incus whose container list reflects device adds."""

    def __init__(self, *, bridges=("incusbr0", WORK_BRIDGE), devices=None, exists=False):
        self.devices: dict[str, dict[str, str]] = dict(devices or {})
        self.exists = exists
        self.calls: list[str] = []
        self.install_scripts: list[str] = []
        self.netplans: list[str] = []
        self.squid_installed = False
        incus = MagicMock()
        self.incus = incus
        incus.network_exists.side_effect = lambda n: n in bridges or n == "jailbee-loose"
        incus.network_get.side_effect = lambda n, k: {
            "incusbr0": "10.0.0.1/24",
            WORK_BRIDGE: "10.9.0.1/16",
            "jailbee-loose": "10.79.1.1/24",
        }[n]
        incus.profile_exists.return_value = False
        incus.list_containers.side_effect = self._list
        incus.start.side_effect = lambda n: self._rec("start")
        incus.init.side_effect = lambda *a: self._rec("init")
        incus.profile_set_yaml.side_effect = lambda *a: self._rec("profile_set_yaml")
        incus.config_device_add.side_effect = self._dev_add
        incus.exec_with_input.side_effect = self._exec_input
        incus.exec.side_effect = self._exec

    def _rec(self, what):
        self.calls.append(what)
        if what == "init":
            self.exists = True

    def _list(self):
        if not self.exists:
            return []
        return [
            {
                "name": PROXY_CONTAINER,
                "status": "Running",
                "devices": self.devices,
            }
        ]

    def _dev_add(self, name, dev, typ, props):
        self.calls.append(f"device:{dev}")
        self.devices[dev] = {"type": typ, **props}

    def _exec_input(self, name, cmd, text, **kw):
        assert cmd == ["bash", "-s"]
        if "netplan" in text and "60-jailbee-egress-proxy" in text:
            self.calls.append("netplan")
            self.netplans.append(text)
        else:
            self.calls.append("install")
            self.install_scripts.append(text)
            self.squid_installed = True
        return ""

    def _exec(self, name, cmd, **kw):
        if cmd[:2] == ["test", "-x"]:
            if not self.squid_installed:
                raise IncusError("missing")
            return ""
        return "active\n"


@pytest.fixture
def set_service(mocker):
    return mocker.patch("jailbee.egress_proxy.set_service")


def test_up_fresh_order_and_service_rule(set_service):
    rig = Rig()
    rig.incus.set_service = None
    set_service_calls = set_service

    def rec(*a):
        rig.calls.append("set_service")

    set_service_calls.side_effect = rec

    proxy_up(rig.incus)

    assert rig.calls == [
        "profile_set_yaml",
        "init",
        "start",
        "device:cl-incusbr0",
        "device:cl-work",
        "netplan",
        "install",
        "set_service",
    ]
    args = set_service.call_args.args
    assert args[0] is rig.incus and args[1] == EGRESS_PROXY_LABEL
    ips = sorted(d["ipv4.address"] for d in rig.devices.values())
    assert args[2] == (ips, [3128])
    assert len(ips) == 2


def test_up_profile_yaml_and_autostart(set_service):
    rig = Rig()
    proxy_up(rig.incus)
    rig.incus.profile_create.assert_called_once_with(PROXY_PROFILE)
    body = yaml.safe_load(rig.incus.profile_set_yaml.call_args.args[1])
    assert body["devices"]["eth0"]["network"] == "jailbee-loose"
    assert body["devices"]["eth0"]["ipv4.address"] == "10.79.1.4"
    assert "security.nesting" not in body["config"] if body.get("config") else True
    rig.incus.config_set.assert_any_call(PROXY_CONTAINER, "boot.autostart", "true")


def test_up_nic_properties(set_service):
    rig = Rig()
    proxy_up(rig.incus)
    nic = rig.devices["cl-incusbr0"]
    assert nic["type"] == "nic"
    assert nic["network"] == "incusbr0"
    assert nic["name"] == "eth1"
    assert nic["security.ipv4_filtering"] == "true"
    assert rig.devices["cl-work"]["name"] == "eth2"


def test_up_without_work_bridge_only_incusbr0(set_service):
    rig = Rig(bridges=("incusbr0",))
    proxy_up(rig.incus)
    assert list(rig.devices) == ["cl-incusbr0"]
    assert set_service.call_args.args[2][1] == [3128]
    assert len(set_service.call_args.args[2][0]) == 1


def test_up_existing_devices_keep_addresses(set_service):
    devices = {
        "cl-incusbr0": {
            "type": "nic",
            "network": "incusbr0",
            "name": "eth1",
            "ipv4.address": "10.0.0.50",
        },
        "cl-work": {
            "type": "nic",
            "network": WORK_BRIDGE,
            "name": "eth2",
            "ipv4.address": "10.9.0.50",
        },
    }
    rig = Rig(devices=devices, exists=True)
    proxy_up(rig.incus)
    rig.incus.config_device_add.assert_not_called()
    assert set_service.call_args.args[2] == (["10.0.0.50", "10.9.0.50"], [3128])
    plan = rig.netplans[0]
    assert "10.0.0.50/24" in plan and "10.9.0.50/16" in plan


def test_up_adds_missing_work_device_later(set_service):
    devices = {
        "cl-incusbr0": {
            "type": "nic",
            "network": "incusbr0",
            "name": "eth1",
            "ipv4.address": "10.0.0.50",
        }
    }
    rig = Rig(devices=devices, exists=True)
    proxy_up(rig.incus)
    assert [c.args[1] for c in rig.incus.config_device_add.call_args_list] == ["cl-work"]
    assert "10.9.0.2" in rig.netplans[0] or "cl-work" in rig.devices


def test_up_work_allocation_under_lock(set_service, mocker):
    events: list[str] = []

    @contextmanager
    def fake_lock():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    mocker.patch("jailbee.egress_proxy.work_network_lock", fake_lock)

    def fake_free(incus, bridge):
        events.append(f"free:{bridge}")
        return "10.9.0.7" if bridge == WORK_BRIDGE else "10.0.0.7"

    mocker.patch("jailbee.egress_proxy.free_ipv4", fake_free)
    rig = Rig()
    rig.incus.config_device_add.side_effect = lambda n, d, t, p: (
        events.append(f"add:{d}"),
        rig._dev_add(n, d, t, p),
    )
    proxy_up(rig.incus)
    i = events.index(f"free:{WORK_BRIDGE}")
    assert events[i - 1] == "enter"
    assert events[i + 1] == "add:cl-work"
    assert events[i + 2] == "exit"
    assert events.count("enter") == 1


def test_netplan_has_no_routes_or_gateway(set_service):
    rig = Rig()
    proxy_up(rig.incus)
    text = rig.netplans[0]
    assert "netplan apply" in text
    assert "/etc/netplan/60-jailbee-egress-proxy.yaml" in text
    body = text.split("<<'JAILBEE_NETPLAN_EOF'\n", 1)[1].split("\nJAILBEE_NETPLAN_EOF", 1)[0]
    plan = yaml.safe_load(body)["network"]["ethernets"]
    assert plan["eth0"]["dhcp4"] is True
    for nic in ("eth1", "eth2"):
        assert plan[nic]["dhcp4"] is False
        assert "routes" not in plan[nic]
        assert "gateway4" not in plan[nic]
        assert "gateway6" not in plan[nic]
        assert len(plan[nic]["addresses"]) == 1
    assert plan["eth1"]["addresses"][0].endswith("/24")
    assert plan["eth2"]["addresses"][0].endswith("/16")


def test_provision_script(set_service):
    rig = Rig()
    proxy_up(rig.incus)
    script = rig.install_scripts[0]
    assert "apt-get install -y --no-install-recommends squid" in script
    assert "/etc/squid/jailbee.d/00-empty.conf" in script
    assert BASE_SQUID_CONF.rstrip() in script
    assert script.index("cat > /etc/squid/squid.conf") < script.rindex("squid -k parse")
    assert script.rindex("squid -k parse") < script.index("systemctl enable --now squid")
    assert script.index("systemctl enable --now squid") < script.index("systemctl restart squid")


def test_up_skips_provision_when_squid_present(set_service):
    rig = Rig(exists=True)
    rig.squid_installed = True
    proxy_up(rig.incus)
    assert rig.install_scripts == []


def test_up_starts_stopped_container(set_service):
    rig = Rig(exists=True)
    rig.squid_installed = True
    original = rig._list
    rig.incus.list_containers.side_effect = lambda: [{**c, "status": "Stopped"} for c in original()]
    proxy_up(rig.incus)
    rig.incus.start.assert_called_once_with(PROXY_CONTAINER)
    rig.incus.init.assert_not_called()


def test_up_raises_naming_apply_when_service_never_active(set_service, mocker):
    mocker.patch.object(egress_proxy, "_SERVICE_WAIT_SECONDS", 0)
    rig = Rig()
    base = rig._exec
    rig.incus.exec.side_effect = lambda n, c, **kw: (
        (_ for _ in ()).throw(IncusError("failed"))
        if c[:2] == ["systemctl", "is-active"]
        else base(n, c, **kw)
    )
    with pytest.raises(RuntimeError, match="jailbee apply"):
        proxy_up(rig.incus)
    set_service.assert_not_called()
    # first provision + one reinstall
    assert len(rig.install_scripts) == 2


def test_endpoints_read_device_addresses():
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": PROXY_CONTAINER,
            "devices": {
                "cl-incusbr0": {"type": "nic", "network": "incusbr0", "ipv4.address": "10.0.0.5"},
                "cl-work": {"type": "nic", "network": WORK_BRIDGE, "ipv4.address": "10.9.0.5"},
                "eth0": {"type": "nic", "network": "jailbee-loose"},
            },
        }
    ]
    assert client_endpoints(incus) == {"incusbr0": "10.0.0.5", WORK_BRIDGE: "10.9.0.5"}
    assert endpoint_for_bridge(incus, WORK_BRIDGE) == "10.9.0.5"
    assert endpoint_for_bridge(incus, "other") is None


def test_endpoints_empty_when_proxy_missing():
    incus = MagicMock()
    incus.list_containers.return_value = []
    assert client_endpoints(incus) == {}
    assert endpoint_for_bridge(incus, "incusbr0") is None
