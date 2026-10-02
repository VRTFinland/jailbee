"""Tests for the Squid egress-proxy container lifecycle (Incus mocked)."""

import os
import subprocess
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
    drop_fragment,
    endpoint_for_bridge,
    proxy_status,
    proxy_up,
    push_fragment,
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
    assert body["config"] == {}
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
    work_ip = rig.devices["cl-work"]["ipv4.address"]
    assert f"{work_ip}/16" in rig.netplans[0]
    assert "10.0.0.50/24" in rig.netplans[0]


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
    # just provisioned in this call: no redundant reinstall
    assert len(rig.install_scripts) == 1


def test_up_reinstalls_once_when_present_but_inactive(set_service, mocker):
    mocker.patch.object(egress_proxy, "_SERVICE_WAIT_SECONDS", 0)
    rig = Rig(exists=True)
    rig.squid_installed = True
    base = rig._exec
    rig.incus.exec.side_effect = lambda n, c, **kw: (
        (_ for _ in ()).throw(IncusError("failed"))
        if c[:2] == ["systemctl", "is-active"]
        else base(n, c, **kw)
    )
    with pytest.raises(RuntimeError, match="jailbee apply"):
        proxy_up(rig.incus)
    assert len(rig.install_scripts) == 1


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


def _running_incus(output: str | Exception) -> MagicMock:
    incus = _status_incus("Running")
    if isinstance(output, Exception):
        incus.exec_with_input.side_effect = output
    else:
        incus.exec_with_input.return_value = output
    return incus


def test_push_unchanged_returns_false():
    incus = _running_incus("UNCHANGED\n")
    assert push_fragment(incus, "abc", "acl x dstdomain a.com\n") is False
    script = incus.exec_with_input.call_args.args[2]
    assert "live=/etc/squid/jailbee.d/abc.conf\n" in script
    assert '"$live.new"' in script
    assert "acl x dstdomain a.com" in script
    assert script.index("squid -k parse") < script.index("squid -k reconfigure")


def test_push_reconfigured_returns_true():
    assert push_fragment(_running_incus("RECONFIGURED\n"), "abc", "x\n") is True


def test_push_parse_failed_raises_with_stderr():
    err = IncusError("exit 1: PARSE_FAILED\nFATAL: bad acl line")
    with pytest.raises(RuntimeError, match="bad acl line"):
        push_fragment(_running_incus(err), "abc", "x\n")


def test_push_script_restores_backup_on_parse_failure():
    incus = _running_incus("UNCHANGED\n")
    push_fragment(incus, "abc", "x\n")
    script = incus.exec_with_input.call_args.args[2]
    assert '"$live.bak"' in script and "PARSE_FAILED" in script and "exit 1" in script


@pytest.mark.parametrize("state", [None, "Stopped"])
def test_push_and_drop_noop_when_proxy_absent(state):
    incus = _status_incus(state)
    assert push_fragment(incus, "abc", "x\n") is False
    assert drop_fragment(incus, "abc") is False
    incus.exec_with_input.assert_not_called()


def test_push_noop_when_degraded():
    incus = _status_incus("Running", active=False)
    assert push_fragment(incus, "abc", "x\n") is False
    incus.exec_with_input.assert_not_called()


def test_drop_removed_reconfigures():
    incus = _running_incus("RECONFIGURED\n")
    assert drop_fragment(incus, "abc") is True
    script = incus.exec_with_input.call_args.args[2]
    assert "rm" in script and "abc.conf" in script and "squid -k reconfigure" in script


def test_drop_absent_file_returns_false():
    assert drop_fragment(_running_incus("UNCHANGED\n"), "abc") is False


def test_push_ignores_noise_before_marker():
    assert push_fragment(_running_incus("squid: warning\nRECONFIGURED\n"), "abc", "x\n") is True
    assert (
        push_fragment(_running_incus("RECONFIGURED-ish noise\nUNCHANGED\n"), "abc", "x\n") is False
    )


def test_push_empty_output_is_false():
    assert push_fragment(_running_incus(""), "abc", "x\n") is False


@pytest.fixture
def real_push(tmp_path, monkeypatch):
    """Run the generated push script under real bash with a fake `squid`."""
    frag = tmp_path / "jailbee.d"
    frag.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "squid.log"
    fake = bindir / "squid"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        f'if [ "$1 $2" = "-k parse" ] && grep -rq BAD "{frag}"; then exit 1; fi\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(egress_proxy, "FRAGMENT_DIR", str(frag))

    def push(text: str) -> str | Exception:
        incus = _running_incus("")
        try:
            push_fragment(incus, "abc", text)
        except RuntimeError as e:
            return e
        script = incus.exec_with_input.call_args.args[2]
        result = subprocess.run(
            ["bash", "-s"],
            input=script,
            text=True,
            capture_output=True,
            env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        )
        return result.stdout.strip() + f"|rc={result.returncode}"

    return push, frag, log


def test_real_script_bad_first_fragment_leaves_directory_empty(real_push):
    push, frag, _ = real_push
    assert push("BAD rule\n").endswith("PARSE_FAILED|rc=1")
    assert list(frag.iterdir()) == []


def test_real_script_bad_v2_restores_v1_and_good_push_reconfigures(real_push):
    push, frag, log = real_push
    assert push("good v1\n").endswith("RECONFIGURED|rc=0")
    assert (frag / "abc.conf").read_text() == "good v1\n"
    assert push("good v1\n").endswith("UNCHANGED|rc=0")
    assert push("BAD v2\n").endswith("PARSE_FAILED|rc=1")
    assert (frag / "abc.conf").read_text() == "good v1\n"
    assert log.read_text().count("-k reconfigure") == 1


# ---- per-repo rule collection and per-container environment ----------------


@pytest.mark.parametrize("mode", ["strict", "loose", None])
@pytest.mark.parametrize(
    ("repo", "extras", "has_wildcard"),
    [(["*.a.com"], [], True), ([], ["*.b.com"], True), (["a.com"], ["b.com"], False)],
)
def test_container_wants_proxy_truth_table(mode, repo, extras, has_wildcard):
    expected = mode == "strict" and has_wildcard
    assert egress_proxy.container_wants_proxy(repo, extras, mode) is expected


def _legacy(name, status="Running", ip="10.1.0.5", prefix="myrepo", mode="strict"):
    addresses = [{"family": "inet", "scope": "global", "address": ip}] if ip else []
    return {
        "name": name,
        "status": status,
        "profiles": ["default", f"{prefix}-base", f"{prefix}-net-{mode}"],
        "devices": {},
        "expanded_devices": {"eth0": {"type": "nic", "network": "incusbr0"}},
        "state": {"network": {"eth0": {"addresses": addresses}}},
    }


def _work(cfg, name, ip="10.9.0.7", status="Running", mode="strict"):
    from jailbee.network import acl_name

    prefix = cfg.container_prefix
    return {
        "name": name,
        "status": status,
        "profiles": ["default", f"{prefix}-base", f"{prefix}-net-work-{mode}"],
        "devices": {
            "eth0": {
                "type": "nic",
                "network": WORK_BRIDGE,
                "security.ipv4_filtering": "true",
                "ipv4.address": ip,
                "security.acls": acl_name(cfg) if mode == "strict" else "",
            }
        },
        "state": {"network": {"eth0": {"addresses": []}}},
    }


def _patch_entries(mocker, repo_entries, extras_by_name):
    mocker.patch("jailbee.egress_scope.effective_repo_entries", return_value=repo_entries)
    mocker.patch(
        "jailbee.egress_scope.container_extras",
        side_effect=lambda _incus, name: extras_by_name.get(name, []),
    )


def test_collect_scopes(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _legacy("myrepo-old"),
        _work(cfg, "myrepo-new"),
        _legacy("myrepo-off", status="Stopped", ip=None),
        _legacy("myrepo-loose", mode="loose", ip="10.1.0.9"),
        _legacy("other-x", prefix="other", ip="10.1.0.8"),
    ]
    _patch_entries(mocker, ["*.repo.com"], {"myrepo-new": ["*.foo.com"]})

    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())

    assert [s.key for s in scopes] == ["myrepo", "myrepo-new"]
    assert set(scopes[0].sources) == {"10.1.0.5", "10.9.0.7"}
    assert scopes[0].entries == ("*.repo.com",)
    assert scopes[1].sources == ("10.9.0.7",)
    assert scopes[1].entries == ("*.foo.com",)


def test_collect_scopes_skips_container_without_ipv4(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [_legacy("myrepo-old", ip=None)]
    _patch_entries(mocker, ["*.repo.com"], {})
    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())
    assert scopes[0].sources == ()


def test_collect_scopes_wildcards_only_in_extras(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [_legacy("myrepo-old"), _work(cfg, "myrepo-new")]
    _patch_entries(mocker, ["plain.com"], {"myrepo-new": ["*.foo.com"]})
    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())
    assert scopes[0].sources == ("10.9.0.7",)
    assert [s.key for s in scopes] == ["myrepo", "myrepo-new"]


def test_sync_repo_rules_drops_when_no_sources(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = []
    _patch_entries(mocker, ["plain.com"], {})
    push = mocker.patch.object(egress_proxy, "push_fragment")
    drop = mocker.patch.object(egress_proxy, "drop_fragment", return_value=True)
    assert egress_proxy.sync_repo_rules(cfg, incus, MagicMock()) is True
    drop.assert_called_once_with(incus, "myrepo")
    push.assert_not_called()


def test_sync_repo_rules_pushes_rendered_fragment(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [_legacy("myrepo-old")]
    _patch_entries(mocker, ["*.repo.com"], {})
    push = mocker.patch.object(egress_proxy, "push_fragment", return_value=True)
    assert egress_proxy.sync_repo_rules(cfg, incus, MagicMock()) is True
    prefix, text = push.call_args.args[1:]
    assert prefix == "myrepo"
    assert "10.1.0.5/32" in text
    assert ".repo.com" in text


def _env_incus(current=None):
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": PROXY_CONTAINER,
            "status": "Running",
            "devices": {
                "cl-incusbr0": {"ipv4.address": "10.0.0.2"},
                "cl-work": {"ipv4.address": "10.9.0.2"},
            },
        },
        {**_legacy("myrepo-old")},
        {
            "name": "myrepo-new",
            "devices": {"eth0": {"network": WORK_BRIDGE}},
        },
    ]
    store = dict(current or {})
    incus.config_get.side_effect = lambda _n, key: store.get(key)
    return incus, store


@pytest.mark.parametrize(("name", "ip"), [("myrepo-old", "10.0.0.2"), ("myrepo-new", "10.9.0.2")])
def test_sync_container_env_sets_per_bridge_ip(make_cfg, tmp_path, mocker, name, ip):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus, _ = _env_incus()
    _patch_entries(mocker, ["*.repo.com"], {})
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), name, "strict")
    keys = {c.args[1] for c in incus.config_set.call_args_list}
    assert keys == {f"environment.{k}" for k in egress_proxy.PROXY_ENV_KEYS}
    http = next(c for c in incus.config_set.call_args_list if c.args[1] == "environment.HTTP_PROXY")
    assert http.args[2] == f"http://{ip}:3128"


def test_sync_container_env_unsets_for_loose(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus, _ = _env_incus({"environment.HTTP_PROXY": "http://10.0.0.2:3128"})
    _patch_entries(mocker, ["*.repo.com"], {})
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-old", "loose")
    incus.config_set.assert_not_called()
    incus.config_unset.assert_called_once_with("myrepo-old", "environment.HTTP_PROXY")


def test_sync_container_env_writes_nothing_when_equal(make_cfg, tmp_path, mocker):
    from jailbee.egress_proxy_render import proxy_env

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    current = {f"environment.{k}": v for k, v in proxy_env("10.0.0.2", ["*.repo.com"]).items()}
    incus, _ = _env_incus(current)
    _patch_entries(mocker, ["*.repo.com"], {})
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-old", "strict")
    incus.config_set.assert_not_called()
    incus.config_unset.assert_not_called()


def test_sync_container_env_warns_and_unsets_without_endpoint(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus, _ = _env_incus({"environment.HTTPS_PROXY": "http://10.0.0.2:3128"})
    incus.list_containers.return_value = incus.list_containers.return_value[1:]  # no proxy
    _patch_entries(mocker, ["*.repo.com"], {})
    warn = mocker.patch("jailbee.tui.warn")
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-old", "strict")
    warn.assert_called_once()
    assert "egress proxy is not running" in warn.call_args.args[0]
    incus.config_set.assert_not_called()
    incus.config_unset.assert_called_once_with("myrepo-old", "environment.HTTPS_PROXY")


def test_sync_container_swallows_incus_error(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus, _ = _env_incus()
    _patch_entries(mocker, ["*.repo.com"], {})
    mocker.patch.object(egress_proxy, "push_fragment", side_effect=IncusError("boom"))
    mocker.patch.object(egress_proxy, "proxy_status", return_value=ProxyStatus.RUNNING)
    warn = mocker.patch("jailbee.tui.warn")
    egress_proxy.sync_container(cfg, incus, "myrepo-old", "strict")
    warn.assert_called_once()
    assert "boom" in warn.call_args.args[0]
