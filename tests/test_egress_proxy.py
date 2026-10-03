"""Tests for the Squid egress-proxy container lifecycle (Incus mocked)."""

import os
import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
import yaml
from sqlalchemy.exc import OperationalError

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
        if cmd[:2] == ["systemctl", "is-system-running"]:
            return "running\n"
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
    assert body["config"] == {"security.nesting": "true"}
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
    assert "mktemp" in script
    assert "flock 9" in script
    assert "/etc/squid/.jailbee-fragments.lock" in script
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
    assert sorted(p.name for p in frag.iterdir()) == ["abc.conf"]


def test_real_script_leaves_no_bak_after_a_successful_change(real_push):
    push, frag, _ = real_push
    push("good v1\n")
    assert push("good v2\n").endswith("RECONFIGURED|rc=0")
    assert sorted(p.name for p in frag.iterdir()) == ["abc.conf"]


# ---- per-repo rule collection and per-container environment ----------------


def _gen_raw(generation):
    marker = "myrepo-net-work-strict" if generation == "work" else "myrepo-net-strict"
    return {"name": "c", "profiles": ["default", marker]}


@pytest.mark.parametrize(
    ("generation", "always", "mode", "entries", "expected"),
    [
        ("work", True, "strict", [], egress_proxy.ProxyUse.FILTERED),
        ("work", True, "strict", ["*.a.com"], egress_proxy.ProxyUse.FILTERED),
        ("work", True, "loose", [], egress_proxy.ProxyUse.OPEN),
        ("work", True, None, ["*.a.com"], egress_proxy.ProxyUse.NONE),
        ("work", False, "strict", ["*.a.com"], egress_proxy.ProxyUse.FILTERED),
        ("work", False, "strict", ["a.com"], egress_proxy.ProxyUse.NONE),
        ("work", False, "loose", ["*.a.com"], egress_proxy.ProxyUse.NONE),
        ("legacy", True, "strict", ["a.com"], egress_proxy.ProxyUse.NONE),
        ("legacy", True, "strict", ["*.a.com"], egress_proxy.ProxyUse.FILTERED),
        ("legacy", True, "loose", ["*.a.com"], egress_proxy.ProxyUse.NONE),
    ],
)
def test_proxy_use_table(make_cfg, tmp_path, generation, always, mode, entries, expected):
    cfg = make_cfg(tmp_path, egress_proxy_always=always)
    assert egress_proxy.proxy_use(cfg, _gen_raw(generation), mode, entries) is expected


@pytest.mark.parametrize(
    ("generation", "always", "expected"),
    [
        ("work", True, True),
        ("work", False, False),
        ("legacy", True, False),
    ],
)
def test_always_on(make_cfg, tmp_path, generation, always, expected):
    cfg = make_cfg(tmp_path, egress_proxy_always=always)
    assert egress_proxy.always_on(cfg, _gen_raw(generation)) is expected


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
        _work(cfg, "myrepo-stopped", ip="10.9.0.9", status="Stopped"),
        _legacy("myrepo-loose", mode="loose", ip="10.1.0.9"),
        _legacy("other-x", prefix="other", ip="10.1.0.8"),
    ]
    _patch_entries(
        mocker, ["*.repo.com"], {"myrepo-new": ["*.foo.com"], "myrepo-stopped": ["*.bar.com"]}
    )

    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())
    assert "10.9.0.9" not in {ip for s in scopes for ip in s.sources}

    assert [(s.kind, s.key) for s in scopes] == [
        ("r", "myrepo"),
        ("c", "myrepo-new"),
        ("o", "myrepo"),
    ]
    assert scopes[-1].sources == ()
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
    assert [(s.kind, s.key) for s in scopes] == [
        ("r", "myrepo"),
        ("c", "myrepo-new"),
        ("o", "myrepo"),
    ]


def _cfg(make_cfg, tmp_path, **kw):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    return make_cfg(repo, **kw)


def test_collect_scopes_always_on_strict_work_container_without_wildcards(
    make_cfg, tmp_path, mocker
):
    cfg = _cfg(make_cfg, tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [_work(cfg, "myrepo-new")]
    _patch_entries(mocker, ["plain.com"], {})
    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())
    assert scopes[0].sources == ("10.9.0.7",)
    assert scopes[0].entries == ("plain.com",)
    assert scopes[-1].sources == ()


def test_collect_scopes_always_on_loose_work_container_is_open_only(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [_work(cfg, "myrepo-new", ip="10.9.0.8", mode="loose")]
    _patch_entries(mocker, ["*.repo.com"], {"myrepo-new": ["b.com"]})
    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())
    assert [(s.kind, s.sources) for s in scopes] == [("r", ()), ("o", ("10.9.0.8",))]


@pytest.mark.parametrize("mode", ["strict", "loose"])
def test_collect_scopes_puts_a_container_in_exactly_one_scope(make_cfg, tmp_path, mocker, mode):
    cfg = _cfg(make_cfg, tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [_work(cfg, "myrepo-new", mode=mode)]
    _patch_entries(mocker, ["*.repo.com"], {"myrepo-new": ["*.foo.com"]})
    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())
    in_repo = "10.9.0.7" in scopes[0].sources
    in_open = "10.9.0.7" in scopes[-1].sources
    assert in_repo != in_open
    assert in_open is (mode == "loose")


def test_collect_scopes_always_off_keeps_the_wildcard_rule(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path, egress_proxy_always=False)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _work(cfg, "myrepo-new"),
        _work(cfg, "myrepo-lo", ip="10.9.0.8", mode="loose"),
    ]
    _patch_entries(mocker, ["plain.com"], {})
    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())
    assert all(s.sources == () for s in scopes)


def test_collect_scopes_legacy_container_in_always_on_repo_needs_a_wildcard(
    make_cfg, tmp_path, mocker
):
    cfg = _cfg(make_cfg, tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [_legacy("myrepo-old")]
    _patch_entries(mocker, ["plain.com"], {})
    scopes = egress_proxy.collect_scopes(cfg, incus, MagicMock())
    assert all(s.sources == () for s in scopes)


def test_repo_scope_carries_every_entry_the_repo_acl_is_built_from(make_cfg, tmp_path, mocker):
    """Parity: the repo ACL (egress_pool) and the Squid repo scope share one source."""
    from jailbee import egress_scope

    cfg = _cfg(make_cfg, tmp_path, egress_allow=["github.com:443", "10.0.0.0/8", "*.a.com"])
    mocker.patch("jailbee.egress_scope.legacy_repo_extras", return_value=["legacy.example.com"])
    mocker.patch("jailbee.egress_scope.container_extras", return_value=[])
    incus = MagicMock()
    incus.list_containers.return_value = [_work(cfg, "myrepo-new")]
    session = MagicMock()
    scopes = egress_proxy.collect_scopes(cfg, incus, session)
    assert list(scopes[0].entries) == egress_scope.effective_repo_entries(cfg, session)
    assert set(cfg.effective_egress_allow()) <= set(scopes[0].entries)
    assert "legacy.example.com" in scopes[0].entries


def _running_proxy_raw():
    return {"name": PROXY_CONTAINER, "status": "Running", "devices": {}}


def test_sync_repo_rules_does_nothing_but_one_list_without_a_running_proxy(
    make_cfg, tmp_path, mocker
):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [_legacy("myrepo-old")]
    collect = mocker.patch.object(egress_proxy, "collect_scopes")
    push = mocker.patch.object(egress_proxy, "push_fragment")
    drop = mocker.patch.object(egress_proxy, "drop_fragment")
    assert egress_proxy.sync_repo_rules(cfg, incus, MagicMock()) is False
    assert incus.list_containers.call_count == 1
    collect.assert_not_called()
    push.assert_not_called()
    drop.assert_not_called()


def test_sync_repo_rules_drops_when_no_sources(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus = MagicMock()
    incus.list_containers.return_value = [_running_proxy_raw()]
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
    incus.list_containers.return_value = [_running_proxy_raw(), _legacy("myrepo-old")]
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


def test_sync_container_env_pushes_changes_into_the_running_tmux(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus, _ = _env_incus({"environment.FOO_UNRELATED": "x"})
    _patch_entries(mocker, ["*.repo.com"], {})
    push = mocker.patch("jailbee.tmux.set_server_environment")
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-old", "strict")
    push.assert_called_once()
    name, env = push.call_args.args[1:]
    assert name == "myrepo-old"
    assert set(env) == set(egress_proxy.PROXY_ENV_KEYS)
    assert env["HTTPS_PROXY"] == "http://10.0.0.2:3128"


def test_sync_container_env_pushes_only_the_unset_keys_to_tmux(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus, _ = _env_incus({"environment.HTTP_PROXY": "http://10.0.0.2:3128"})
    _patch_entries(mocker, ["*.repo.com"], {})
    push = mocker.patch("jailbee.tmux.set_server_environment")
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-old", "loose")
    push.assert_called_once_with(incus, "myrepo-old", {"HTTP_PROXY": None})


def test_sync_container_env_leaves_tmux_alone_when_nothing_changed(make_cfg, tmp_path, mocker):
    from jailbee.egress_proxy_render import proxy_env

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    current = {f"environment.{k}": v for k, v in proxy_env("10.0.0.2", ["*.repo.com"]).items()}
    incus, _ = _env_incus(current)
    _patch_entries(mocker, ["*.repo.com"], {})
    push = mocker.patch("jailbee.tmux.set_server_environment")
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-old", "strict")
    push.assert_not_called()


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
    mocker.patch.object(egress_proxy, "sync_repo_rules", side_effect=IncusError("boom"))
    warn = mocker.patch("jailbee.tui.warn_plain")
    egress_proxy.sync_container(cfg, incus, "myrepo-old", "strict")
    warn.assert_called_once()
    assert "boom" in warn.call_args.args[0]


def _work_env_incus(cfg, *, mode="strict", current=None, proxy=True):
    incus = MagicMock()
    listed = [_work(cfg, "myrepo-new", mode=mode)]
    if proxy:
        listed.insert(
            0,
            {
                "name": PROXY_CONTAINER,
                "status": "Running",
                "devices": {"cl-work": {"ipv4.address": "10.9.0.2"}},
            },
        )
    incus.list_containers.return_value = listed
    incus.network_acl_exists.return_value = False
    store = dict(current or {})
    incus.config_get.side_effect = lambda _n, key: store.get(key)
    return incus


def _as_config(env):
    return {f"environment.{k}": v for k, v in env.items()}


def test_env_always_on_strict_work_container_without_wildcards(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path)
    mocker.patch("jailbee.tmux.set_server_environment")
    _patch_entries(mocker, ["github.com:443", "10.0.0.0/8"], {})
    changed = egress_proxy.sync_container_env(
        cfg, _work_env_incus(cfg), MagicMock(), "myrepo-new", "strict"
    )
    assert changed["HTTPS_PROXY"] == "http://10.9.0.2:3128"
    assert changed["NO_PROXY"] == "localhost,127.0.0.1,.incus"


@pytest.mark.parametrize("entries", [["a.com"], ["10.0.0.0/8"], ["1.2.3.4:5"], ["*.a.com:8443"]])
def test_env_always_on_does_not_change_when_entries_do(make_cfg, tmp_path, mocker, entries):
    cfg = _cfg(make_cfg, tmp_path)
    mocker.patch("jailbee.tmux.set_server_environment")
    _patch_entries(mocker, [], {})
    first = egress_proxy.sync_container_env(
        cfg, _work_env_incus(cfg), MagicMock(), "myrepo-new", "strict"
    )
    incus = _work_env_incus(cfg, current=_as_config(first))
    _patch_entries(mocker, entries, {})
    assert egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-new", "strict") == {}
    incus.config_set.assert_not_called()
    incus.config_unset.assert_not_called()


def test_env_always_on_survives_a_mode_switch(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path)
    mocker.patch("jailbee.tmux.set_server_environment")
    _patch_entries(mocker, ["*.repo.com"], {})
    first = egress_proxy.sync_container_env(
        cfg, _work_env_incus(cfg), MagicMock(), "myrepo-new", "strict"
    )
    incus = _work_env_incus(cfg, mode="loose", current=_as_config(first))
    assert egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-new", "loose") == {}
    incus.config_unset.assert_not_called()


def test_env_always_on_keeps_the_environment_when_the_proxy_is_down(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path)
    incus = _work_env_incus(
        cfg, current={"environment.HTTPS_PROXY": "http://10.9.0.2:3128"}, proxy=False
    )
    _patch_entries(mocker, [], {})
    warn = mocker.patch("jailbee.tui.warn")
    assert egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-new", "strict") == {}
    warn.assert_called_once()
    incus.config_set.assert_not_called()
    incus.config_unset.assert_not_called()


def test_env_always_on_keeps_the_environment_when_the_mode_is_unknown(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path)
    incus = _work_env_incus(cfg, current={"environment.HTTPS_PROXY": "http://10.9.0.2:3128"})
    _patch_entries(mocker, [], {})
    push = mocker.patch("jailbee.tmux.set_server_environment")
    assert egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-new", None) == {}
    incus.config_set.assert_not_called()
    incus.config_unset.assert_not_called()
    push.assert_not_called()


def test_env_always_off_clears_the_environment_when_the_mode_is_unknown(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path, egress_proxy_always=False)
    incus = _work_env_incus(cfg, current={"environment.HTTPS_PROXY": "http://10.9.0.2:3128"})
    _patch_entries(mocker, [], {})
    mocker.patch("jailbee.tmux.set_server_environment")
    changed = egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-new", None)
    assert changed == {"HTTPS_PROXY": None}
    incus.config_unset.assert_called_once_with("myrepo-new", "environment.HTTPS_PROXY")


def test_env_always_off_work_container_without_wildcards_gets_nothing(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path, egress_proxy_always=False)
    incus = _work_env_incus(cfg)
    _patch_entries(mocker, ["a.com"], {})
    assert egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-new", "strict") == {}
    incus.config_set.assert_not_called()


@pytest.mark.parametrize(("changed", "expected"), [({"HTTPS_PROXY": "x"}, True), ({}, False)])
def test_sync_container_reports_whether_the_env_changed(
    make_cfg, tmp_path, mocker, changed, expected
):
    cfg = _cfg(make_cfg, tmp_path)
    mocker.patch.object(egress_proxy, "sync_repo_rules")
    mocker.patch.object(egress_proxy, "sync_container_env", return_value=changed)
    assert (
        egress_proxy.sync_container(cfg, _work_env_incus(cfg), "myrepo-new", "strict") is expected
    )


def test_sync_container_returns_false_on_a_swallowed_error(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path)
    mocker.patch.object(egress_proxy, "sync_repo_rules", side_effect=IncusError("boom"))
    mocker.patch("jailbee.tui.warn_plain")
    assert egress_proxy.sync_container(cfg, _work_env_incus(cfg), "myrepo-new", "strict") is False


def test_sync_container_starts_the_proxy_for_an_always_on_container(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path)
    incus = _work_env_incus(cfg, proxy=False)
    up = mocker.patch.object(egress_proxy, "proxy_up_or_warn", return_value=True)
    mocker.patch.object(egress_proxy, "sync_repo_rules")
    mocker.patch.object(egress_proxy, "sync_container_env", return_value={})
    egress_proxy.sync_container(cfg, incus, "myrepo-new", "loose")
    up.assert_called_once_with(incus)


@pytest.mark.parametrize("case", ["running", "always_off", "legacy", "no_mode"])
def test_sync_container_leaves_the_proxy_alone(make_cfg, tmp_path, mocker, case):
    cfg = _cfg(make_cfg, tmp_path, egress_proxy_always=case != "always_off")
    incus = _work_env_incus(cfg, proxy=case == "running")
    mode = None if case == "no_mode" else "strict"
    if case == "legacy":
        incus.list_containers.return_value = [_legacy("myrepo-new")]
    up = mocker.patch.object(egress_proxy, "proxy_up_or_warn")
    mocker.patch.object(egress_proxy, "sync_repo_rules")
    mocker.patch.object(egress_proxy, "sync_container_env", return_value={})
    egress_proxy.sync_container(cfg, incus, "myrepo-new", mode)
    up.assert_not_called()


# --- boot wait and netplan idempotence ---------------------------------------


def _booting_exec(rig, states):
    base = rig._exec
    seq = iter(states)

    def fn(n, c, **kw):
        if c[:2] == ["systemctl", "is-system-running"]:
            state = next(seq, "running")
            if state in ("running", "degraded"):
                if state == "degraded":
                    raise IncusError("exit 1: degraded")
                return "running\n"
            raise IncusError(f"Failed to connect to bus ({state})")
        return base(n, c, **kw)

    rig.incus.exec.side_effect = fn


def test_up_waits_for_boot_before_netplan(set_service, mocker):
    sleep = mocker.patch("jailbee.egress_proxy.time.sleep")
    rig = Rig()
    _booting_exec(rig, ["starting", "starting", "running"])
    proxy_up(rig.incus)
    assert sleep.call_count == 2
    assert rig.calls.index("netplan") > rig.calls.index("start")


def test_up_accepts_degraded_boot(set_service, mocker):
    mocker.patch("jailbee.egress_proxy.time.sleep")
    rig = Rig()
    _booting_exec(rig, ["degraded"])
    proxy_up(rig.incus)
    assert "netplan" in rig.calls


def test_up_boot_timeout_raises_before_netplan(set_service, mocker):
    mocker.patch("jailbee.egress_proxy.time.sleep")
    mocker.patch.object(egress_proxy, "_BOOT_WAIT_SECONDS", 0)
    rig = Rig()
    _booting_exec(rig, ["starting"] * 50)
    with pytest.raises(RuntimeError, match="did not finish booting"):
        proxy_up(rig.incus)
    assert "netplan" not in rig.calls
    set_service.assert_not_called()


@pytest.fixture
def real_netplan(tmp_path, monkeypatch):
    """Run the generated netplan script under real bash with a fake `netplan`."""
    target = tmp_path / "60-jailbee-egress-proxy.yaml"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "netplan.log"
    fake = bindir / "netplan"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n')
    fake.chmod(0o755)
    monkeypatch.setattr(egress_proxy, "_NETPLAN_PATH", str(target))

    def write(addresses: dict[str, str]) -> subprocess.CompletedProcess[str]:
        incus = MagicMock()
        incus.network_get.return_value = "10.0.0.1/24"
        egress_proxy._write_netplan(incus, addresses)
        script = incus.exec_with_input.call_args.args[2]
        return subprocess.run(
            ["bash", "-s"],
            input=script,
            text=True,
            capture_output=True,
            env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        )

    return write, target, log


def test_netplan_applies_when_new_then_skips_when_unchanged(real_netplan):
    write, target, log = real_netplan
    assert write({"incusbr0": "10.0.0.5"}).returncode == 0
    assert "10.0.0.5/24" in target.read_text()
    assert log.read_text().count("apply") == 1
    assert write({"incusbr0": "10.0.0.5"}).returncode == 0
    assert log.read_text().count("apply") == 1


def test_netplan_applies_again_when_changed(real_netplan):
    write, target, log = real_netplan
    write({"incusbr0": "10.0.0.5"})
    write({"incusbr0": "10.0.0.6"})
    assert "10.0.0.6/24" in target.read_text()
    assert log.read_text().count("apply") == 2


def test_netplan_failed_apply_restores_previous(real_netplan, tmp_path):
    write, target, _log = real_netplan
    write({"incusbr0": "10.0.0.5"})
    (tmp_path / "bin" / "netplan").write_text("#!/bin/sh\nexit 1\n")
    assert write({"incusbr0": "10.0.0.6"}).returncode != 0
    assert "10.0.0.5/24" in target.read_text()


# --- install.sh -------------------------------------------------------------


def test_install_sh_pins_ip_forward_off():
    text = egress_proxy._read_provision_text("install.sh")
    assert "/etc/sysctl.d/60-jailbee-egress-proxy.conf" in text
    assert "net.ipv4.ip_forward=0" in text


# --- proxy_up_or_warn --------------------------------------------------------


def test_proxy_up_or_warn_warns_plain_with_bracketed_reason(mocker):
    mocker.patch.object(
        egress_proxy, "proxy_up", side_effect=RuntimeError("failed ['systemctl', 'x']")
    )
    warn = mocker.patch("jailbee.egress_proxy.tui.warn_plain")
    assert egress_proxy.proxy_up_or_warn(MagicMock()) is False
    assert "['systemctl', 'x']" in warn.call_args.args[0]


def test_proxy_up_or_warn_quiet_on_success(mocker):
    up = mocker.patch.object(egress_proxy, "proxy_up")
    warn = mocker.patch("jailbee.egress_proxy.tui.warn_plain")
    assert egress_proxy.proxy_up_or_warn(MagicMock()) is True
    up.assert_called_once()
    warn.assert_not_called()


# --- final-review wave -------------------------------------------------------


def test_proxy_up_or_warn_also_catches_value_error(mocker):
    mocker.patch.object(egress_proxy, "proxy_up", side_effect=ValueError("no free ip"))
    warn = mocker.patch("jailbee.egress_proxy.tui.warn_plain")
    assert egress_proxy.proxy_up_or_warn(MagicMock()) is False
    assert "no free ip" in warn.call_args.args[0]


@pytest.mark.parametrize(
    "error", [ValueError("bad cidr"), OperationalError("stmt", {}, Exception("db locked"))]
)
def test_sync_container_never_raises_for_value_or_db_errors(make_cfg, tmp_path, mocker, error):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch.object(egress_proxy, "sync_repo_rules", side_effect=error)
    warn = mocker.patch("jailbee.tui.warn_plain")
    egress_proxy.sync_container(cfg, MagicMock(), "myrepo-old", "strict")
    warn.assert_called_once()


def test_sync_container_pushes_rules_before_setting_the_env(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    order: list[str] = []
    mocker.patch.object(
        egress_proxy, "sync_repo_rules", side_effect=lambda *_a: order.append("rules")
    )
    mocker.patch.object(
        egress_proxy, "sync_container_env", side_effect=lambda *_a, **_k: order.append("env")
    )
    egress_proxy.sync_container(cfg, MagicMock(), "myrepo-old", "strict")
    assert order == ["rules", "env"]


def test_env_only_never_raises_and_does_not_push_rules(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch.object(egress_proxy, "sync_container_env", side_effect=ValueError("x"))
    rules = mocker.patch.object(egress_proxy, "sync_repo_rules")
    warn = mocker.patch("jailbee.tui.warn_plain")
    egress_proxy.sync_container_env_only(cfg, MagicMock(), "myrepo-old", "strict")
    rules.assert_not_called()
    warn.assert_called_once()


def test_sync_repo_never_raises(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    mocker.patch.object(egress_proxy, "sync_repo_rules", side_effect=IncusError("down"))
    warn = mocker.patch("jailbee.tui.warn_plain")
    egress_proxy.sync_repo(cfg, MagicMock())
    warn.assert_called_once()


def test_env_with_a_snapshot_reads_config_from_it_and_lists_nothing(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus, _ = _env_incus()
    raws = incus.list_containers.return_value
    raws[1] = {**raws[1], "config": {"environment.HTTP_PROXY": "http://stale:1"}}
    incus.list_containers.reset_mock()
    incus.network_acl_exists.return_value = False
    _patch_entries(mocker, ["plain.com"], {})
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-old", "strict", raws=raws)
    incus.list_containers.assert_not_called()
    incus.config_get.assert_not_called()
    incus.config_unset.assert_called_once_with("myrepo-old", "environment.HTTP_PROXY")


def test_env_no_proxy_lists_the_other_jailbee_services(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    incus, _ = _env_incus()
    _patch_entries(mocker, ["*.repo.com"], {})
    mocker.patch.object(egress_proxy, "other_service_ips", return_value=["10.79.1.5"])
    egress_proxy.sync_container_env(cfg, incus, MagicMock(), "myrepo-old", "strict")
    no_proxy = next(
        c.args[2] for c in incus.config_set.call_args_list if c.args[1] == "environment.NO_PROXY"
    )
    assert "10.79.1.5" in no_proxy.split(",")


def test_proxy_env_direct_hosts_are_pure_input():
    from jailbee.egress_proxy_render import proxy_env

    env = proxy_env("10.0.0.2", [], ["10.79.1.5", "10.79.1.5"])
    assert env["NO_PROXY"].split(",").count("10.79.1.5") == 1
    assert "10.79.1.5" not in proxy_env("10.0.0.2", [])["NO_PROXY"]


def test_other_service_ips_excludes_the_proxy_rules():
    from jailbee.network import services_acl_yaml
    from jailbee.services_acl import LITELLM_LABEL, other_service_ips

    incus = MagicMock()
    incus.network_acl_exists.return_value = True
    incus.network_acl_show.return_value = services_acl_yaml(
        {LITELLM_LABEL: (["10.79.1.5"], [4000, 4001]), EGRESS_PROXY_LABEL: (["10.0.0.2"], [3128])}
    )
    assert other_service_ips(incus, EGRESS_PROXY_LABEL) == ["10.79.1.5"]


def test_other_service_ips_without_the_acl():
    from jailbee.services_acl import other_service_ips

    incus = MagicMock()
    incus.network_acl_exists.return_value = False
    assert other_service_ips(incus, EGRESS_PROXY_LABEL) == []


def test_push_takes_a_lock_and_leaves_no_staging_files(real_push):
    push, frag, _ = real_push
    assert push("good v1\n").endswith("RECONFIGURED|rc=0")
    assert not list(frag.glob("*.new"))
    assert (frag.parent / ".jailbee-fragments.lock").exists()


def test_proxy_needed_for_a_repo_wildcard_without_listing(make_cfg, tmp_path, mocker):
    cfg = _cfg(make_cfg, tmp_path)
    incus = MagicMock()
    _patch_entries(mocker, ["*.repo.com"], {})
    assert egress_proxy.proxy_needed(cfg, incus, MagicMock()) is True
    incus.list_containers.assert_not_called()


@pytest.mark.parametrize(
    ("always", "raws", "extras", "expected"),
    [
        (True, "work_stopped", [], True),
        (False, "work_stopped", [], False),
        (True, "legacy", [], False),
        (True, "legacy", ["*.foo.com"], True),
    ],
)
def test_proxy_needed(make_cfg, tmp_path, mocker, always, raws, extras, expected):
    cfg = _cfg(make_cfg, tmp_path, egress_proxy_always=always)
    incus = MagicMock()
    raw = (
        _work(cfg, "myrepo-new", status="Stopped")
        if raws == "work_stopped"
        else _legacy("myrepo-new")
    )
    incus.list_containers.return_value = [raw]
    _patch_entries(mocker, ["plain.com"], {"myrepo-new": extras})
    assert egress_proxy.proxy_needed(cfg, incus, MagicMock()) is expected
