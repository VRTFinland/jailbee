"""`jailbee-litellm` lifecycle through a MagicMock Incus (style of test_registry.py)."""

import base64
import hashlib
import json
import os
import re
import subprocess
from importlib import resources
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from jailbee import litellm as ll
from jailbee.config.models_litellm import LiteLLMRepoOverlay, LiteLLMRepoView
from jailbee.egress import EgressEntry
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.litellm_inputs import LiteLLMInputError
from jailbee.network import services_acl_yaml


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        ll,
        "_resolve_egress",
        lambda hosts: [
            EgressEntry(
                destinations=["192.0.2.1"],
                port=int(h.rsplit(":", 1)[1]) if ":" in h else 443,
                description=h if ":" in h else f"{h}:443",
            )
            for h in hosts
        ],
    )  # no DNS in tests
    monkeypatch.setattr(ll.time, "sleep", lambda _s: None)
    return tmp_path


def _gcfg(enabled: bool = True, **litellm: object) -> GlobalConfig:
    return GlobalConfig.model_validate({"litellm": {"enabled": enabled, **litellm}})


_KIMI = {
    "model": "openrouter/moonshotai/kimi-k3",
    "context_window": 262144,
    "api_key": "OPENROUTER_API_KEY",
}
_TWO = {
    "accounts": ["personal", "work"],
    "routes": {"sol-low": {"model": "chatgpt/gpt-6.1-sol", "effort": "low"}},
    "profiles": {"codex": {"account": "personal"}, "work": {"account": "work", "opus": "sol-low"}},
}


def _secrets(xdg: Path, text: str) -> None:
    path = xdg / "config" / "jailbee" / "litellm" / "secrets.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def _incus(
    *,
    present: bool,
    running: bool = True,
    installed: str | None = "1.103.1",
    login: str = "present",
    ack: str = "auto",
) -> MagicMock:
    incus = MagicMock()
    incus.network_get.return_value = "10.79.115.1/24"
    incus.network_exists.return_value = True
    incus.profile_exists.return_value = True
    incus.network_acl_exists.return_value = True
    incus.config_show.return_value = yaml.safe_dump(
        {"devices": {"state": {"type": "disk", "path": ll.CONTAINER_STATE_DIR}}}
    )
    incus.profile_show.return_value = yaml.safe_dump(
        {"devices": {"root": {"type": "disk", "path": "/", "pool": "default"}}}
    )
    incus.storage_volume_exists.return_value = True
    incus.list_containers.return_value = (
        [{"name": ll.LITELLM_CONTAINER, "status": "Running" if running else "Stopped"}]
        if present
        else []
    )

    reads = {"ack": 0}

    def exec_(name, cmd, **_kw):
        text = " ".join(cmd)
        if "importlib.metadata" in text:
            if installed is None:
                raise IncusError("no venv")
            return f"{installed}\n"
        if "is-active" in text:
            return "active\n"
        if "health/liveliness" in text:
            return "ok\n"
        if "auth.json" in text:
            return f"{login}\n"
        if text.startswith("cat ") and text.endswith("/applied.json"):
            if ack == "none":
                raise IncusError("no such file")
            account = text.split(f"{ll.CONTAINER_STATE_DIR}/")[1].split("/")[0]
            pushed = _pushed(incus).get(f"{ll.CONTAINER_STATE_DIR}/{account}/hot.json")
            if pushed is None:
                raise IncusError("no such file")
            digest = hashlib.sha256(pushed.encode()).hexdigest()
            reads["ack"] += 1
            # "stale": the proxy only ever acknowledged an older file; "late": it catches up.
            if ack == "stale" or (ack == "late" and reads["ack"] == 1):
                digest = hashlib.sha256(b"old").hexdigest()
            return json.dumps({"hot_digest": digest, "error": "boom" if ack == "error" else None})
        return ""

    incus.exec.side_effect = exec_
    return incus


def _pushed(incus: MagicMock) -> dict[str, str]:
    """Container path -> content, for every file a push script wrote."""
    files: dict[str, str] = {}
    for call in incus.exec_with_input.call_args_list:
        for path, blob in re.findall(r"^put (\S+) ([A-Za-z0-9+/=]+)$", call.args[2], re.M):
            files[path] = base64.b64decode(blob).decode()
    return files


def _install_script(incus: MagicMock) -> str:
    return next(
        c.args[2] for c in incus.exec_with_input.call_args_list if "/root/install.sh" in c.args[2]
    )


def _execs(incus: MagicMock) -> list[str]:
    return [" ".join(c.args[1]) for c in incus.exec.call_args_list]


def _rotate_key(xdg: Path, account: str = "default") -> None:
    """A cold change: the master key sits in instance.env, which only a restart re-reads."""
    (xdg / "jailbee" / "litellm" / account / "master.key").write_text("sk-jb-rotated\n")


def test_up_refuses_when_disabled():
    with pytest.raises(ValueError, match=r"litellm\.enabled"):
        ll.litellm_up(_incus(present=False), _gcfg(enabled=False))


def test_up_creates_provisions_and_opens_services_rule():
    incus = _incus(present=False)
    result = ll.litellm_up(incus, _gcfg())
    incus.init.assert_called_once_with("images:ubuntu/26.04/cloud", ll.LITELLM_CONTAINER)
    assert "/root/install.sh" in _install_script(incus)
    assert result.ip == "10.79.115.3" and result.ports == {"default": 4100}
    assert result.installed is True
    services = [
        c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == "jailbee-services"
    ]
    assert yaml.safe_load(services[-1].args[1])["egress"][0]["destination"] == "10.79.115.3/32"


def test_up_gives_the_loose_bridge_its_acl_chain_before_any_nic_acl():
    """Incus flushes `acl.<bridge>` on a NIC-ACL edit; it only exists with a bridge ACL."""
    incus = _incus(present=False)
    incus.network_get.side_effect = lambda _net, key: (
        "10.79.115.1/24" if key == "ipv4.address" else ""
    )
    ll.litellm_up(incus, _gcfg())
    calls = incus.mock_calls
    attach = next(
        i
        for i, c in enumerate(calls)
        if c[0] == "network_set" and c.args[1:] == ("security.acls", "jailbee-loose-baseline")
    )
    first_nic_acl = next(i for i, c in enumerate(calls) if c[0] == "network_acl_set_yaml")
    assert attach < first_nic_acl


def test_install_is_package_restricted_and_auth_mount_is_added_after_install():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    calls = incus.mock_calls
    install = next(i for i, c in enumerate(calls) if c[0] == "exec_with_input")
    mount = next(i for i, c in enumerate(calls) if c[0] == "config_device_add")
    assert install < mount
    before = [c for c in calls[:install] if c[0] == "network_acl_set_yaml"]
    assert before
    assert {
        r.get("description", "").removeprefix("allowlisted: ")
        for r in yaml.safe_load(before[-1].args[1])["egress"]
    } >= {"pypi.org:443", "files.pythonhosted.org:443", "archive.ubuntu.com:80"}
    assert all(
        "security.acls" in yaml.safe_load(c.args[1])["devices"]["eth0"]
        for c in calls
        if c[0] == "profile_set_yaml"
    )
    final = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert final["ipv4.address"] == "10.79.115.3"


def test_provision_streams_real_lock_at_subprocess_boundary(mocker):
    from jailbee.incus import Incus

    run = mocker.patch("jailbee.incus.subprocess.run")
    run.return_value = subprocess.CompletedProcess([], 0, "", "")
    ll._provision(Incus(), "1.103.1", True)
    args, kwargs = run.call_args
    assert args[0][-2:] == ["bash", "-s"]
    assert len(kwargs["input"]) > 200_000
    assert "litellm==1.103.1" in kwargs["input"]
    assert all(len(arg) < 4096 for arg in args[0])


def test_provision_failure_does_not_echo_install_script_in_error(mocker):
    from jailbee.incus import Incus

    mocker.patch(
        "jailbee.incus.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, "", "apt failed"),
    )
    with pytest.raises(IncusError) as caught:
        ll._provision(Incus(), "1.103.1", True)
    assert "apt failed" in str(caught.value)
    assert "litellm==1.103.1" not in str(caught.value)


def test_reinstall_detaches_auth_before_package_egress_and_reattaches_after():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(), reinstall=True)
    calls = incus.mock_calls
    stopped = next(i for i, c in enumerate(calls) if c[0] == "stop")
    removed = next(i for i, c in enumerate(calls) if c[0] == "config_device_remove")
    package = next(i for i, c in enumerate(calls) if c[0] == "network_acl_set_yaml")
    install = next(i for i, c in enumerate(calls) if c[0] == "exec_with_input")
    mount = next(i for i, c in enumerate(calls) if c[0] == "config_device_add")
    assert stopped < removed < package < install < mount
    assert calls[removed].kwargs == {}
    assert any(
        c[0] == "config_set" and c.args[1:] == ("boot.autostart", "false") for c in calls[:package]
    )


def test_install_failure_never_reattaches_auth_or_opens_services():
    incus = _incus(present=True)
    incus.exec_with_input.side_effect = IncusError("apt unavailable")
    with pytest.raises(IncusError, match="apt unavailable"):
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    incus.config_device_add.assert_not_called()
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)
    final = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert all(r.get("destination_port") in {"67", "547", "53"} for r in final["egress"])


def test_reinstall_aborts_before_package_egress_if_state_cannot_be_detached():
    incus = _incus(present=True)
    incus.config_device_remove.side_effect = IncusError("state device busy")
    with pytest.raises(IncusError, match="state device busy"):
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    incus.exec_with_input.assert_not_called()
    incus.config_device_add.assert_not_called()
    # Recovery uses DHCP/DNS-only egress, never a package-host ACL.
    assert all(
        r.get("destination_port") in {"67", "547", "53"}
        for c in incus.network_acl_set_yaml.call_args_list
        for r in yaml.safe_load(c.args[1])["egress"]
    )


def test_bridge_lease_collision_rejected_before_instance_changes():
    incus = _incus(present=False)
    incus.network_leases.return_value = [{"address": "10.79.115.3", "hostname": "other"}]
    with pytest.raises(RuntimeError, match=r"10\.79\.115\.3.*other.*jailbee-loose"):
        ll.litellm_up(incus, _gcfg())
    incus.init.assert_not_called()


def test_bridge_static_nic_collision_rejected_even_without_lease():
    incus = _incus(present=False)
    incus.list_containers.return_value = [{"name": "other", "status": "Stopped"}]
    incus.config_show.return_value = yaml.safe_dump(
        {
            "devices": {
                "eth0": {"type": "nic", "network": "jailbee-loose", "ipv4.address": "10.79.115.3"}
            }
        }
    )
    with pytest.raises(RuntimeError, match=r"10\.79\.115\.3.*other"):
        ll.litellm_up(incus, _gcfg())


def test_bridge_self_lease_and_static_nic_are_allowed():
    incus = _incus(present=True)
    incus.network_leases.return_value = [
        {"address": "10.79.115.3", "hostname": ll.LITELLM_CONTAINER}
    ]
    incus.config_show.return_value = yaml.safe_dump(
        {
            "devices": {
                "eth0": {"type": "nic", "network": "jailbee-loose", "ipv4.address": "10.79.115.3"}
            }
        }
    )
    assert ll.litellm_up(incus, _gcfg()).ip == "10.79.115.3"


def test_bridge_self_lease_can_be_identified_by_nic_mac_without_hostname():
    incus = _incus(present=True)
    incus.list_containers.return_value = [
        {
            "name": ll.LITELLM_CONTAINER,
            "status": "Running",
            "config": {"volatile.eth0.hwaddr": "00:16:3e:01:02:03"},
        }
    ]
    incus.network_leases.return_value = [
        {"address": "10.79.115.3", "hostname": "", "hwaddr": "00:16:3e:01:02:03"}
    ]
    assert ll.litellm_up(incus, _gcfg()).ip == "10.79.115.3"


def test_sync_publishes_json_only_after_private_key():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    ll.sync_container(incus, "repo-branch", ll.container_sync_payload(incus, _gcfg()))
    script = incus.exec_with_input.call_args.args[2]
    assert script.index('mv "$tmp" /etc/jailbee/litellm-default.key') < script.index(
        'mv "$tmp" ' + ll.CONTAINER_FILE
    )


def test_up_does_not_start_an_account_without_a_login():
    """Unlogged, LiteLLM blocks in its device-code prompt and never turns healthy."""
    incus = _incus(present=True, login="missing")
    result = ll.litellm_up(incus, _gcfg())
    assert result.awaiting_login == ["default"] and result.restarted == []
    execs = _execs(incus)
    assert not any("systemctl restart" in e or "systemctl enable" in e for e in execs)
    assert any("systemctl disable --now jailbee-litellm@default.service" in e for e in execs)
    assert not any("health/liveliness" in e for e in execs)
    incus.network_acl_set_yaml.assert_called()


def test_up_starts_an_api_key_only_account_without_any_login(xdg):
    """Only a `chatgpt/` route makes LiteLLM wait in its device-code prompt."""
    _secrets(xdg, "OPENROUTER_API_KEY=k\n")
    cfg = _gcfg(
        routes={"kimi": _KIMI},
        profiles={"codex": {"fable": "kimi", "opus": "kimi", "sonnet": "kimi", "haiku": "kimi"}},
    )
    incus = _incus(present=True, login="missing")
    result = ll.litellm_up(incus, cfg)
    assert result.awaiting_login == [] and result.restarted == ["default"]
    assert not any("auth.json" in e for e in _execs(incus))


def test_up_holds_back_only_the_account_that_serves_chatgpt(xdg):
    _secrets(xdg, "OPENROUTER_API_KEY=k\n")
    cfg = _gcfg(
        accounts=["default", "keys"],
        routes={"kimi": _KIMI},
        profiles={"kimi": {"account": "keys", "opus": "kimi"}},
    )
    result = ll.litellm_up(_incus(present=True, login="missing"), cfg)
    assert result.awaiting_login == ["default"] and result.restarted == ["keys"]


def test_up_after_login_starts_the_instance():
    incus = _incus(present=True, login="present")
    result = ll.litellm_up(incus, _gcfg())
    assert result.awaiting_login == [] and result.restarted == ["default"]


def test_reconcile_skips_an_account_without_a_login():
    incus = _incus(present=True, login="present")
    ll.litellm_up(incus, _gcfg())
    base = incus.exec.side_effect

    def logged_out(name, cmd, **kw):
        text = " ".join(cmd)
        if "is-active" in text:
            return "inactive\n"
        if "auth.json" in text:
            return "missing\n"
        return base(name, cmd, **kw)

    incus.exec.side_effect = logged_out
    incus.exec.reset_mock()
    result = ll.litellm_reconcile(incus, _gcfg())
    assert result is not None and result.awaiting_login == ["default"]
    assert not any("systemctl restart" in e for e in _execs(incus))


def test_up_is_quiet_when_nothing_changed():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.exec.reset_mock()
    result = ll.litellm_up(incus, _gcfg())
    assert result.restarted == []
    assert not any("systemctl restart" in e for e in _execs(incus))


def test_up_restarts_once_after_config_change(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.exec.reset_mock()
    _rotate_key(xdg)
    result = ll.litellm_up(incus, _gcfg())
    assert result.restarted == ["default"]
    assert sum("systemctl restart" in e for e in _execs(incus)) == 1


def test_up_restarts_again_after_a_run_that_failed_before_the_restart(xdg):
    """Files are on disk after the failed run, so `changed` alone would skip the restart."""
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    _rotate_key(xdg)
    changed = _gcfg()
    healthy = incus.exec.side_effect

    def restart_fails(name, cmd, **kw):
        if "restart" in cmd:
            raise IncusError("restart failed")
        return healthy(name, cmd, **kw)

    incus.exec.side_effect = restart_fails
    with pytest.raises(IncusError, match="restart failed"):
        ll.litellm_up(incus, changed)

    incus.exec.side_effect = healthy
    incus.exec.reset_mock()
    result = ll.litellm_up(incus, changed)
    assert result.restarted == ["default"]
    assert sum("systemctl restart" in e for e in _execs(incus)) == 1
    incus.exec.reset_mock()
    assert ll.litellm_up(incus, changed).restarted == []


def test_up_reattaches_state_mount_after_a_failure_between_install_and_mount():
    incus = _incus(present=True)
    incus.config_show.return_value = yaml.safe_dump({"devices": {}})
    result = ll.litellm_up(incus, _gcfg())
    assert result.installed is False
    incus.config_device_add.assert_called_once()
    assert incus.config_device_add.call_args.args[:3] == (ll.LITELLM_CONTAINER, "state", "disk")
    incus.config_set.assert_any_call(ll.LITELLM_CONTAINER, "boot.autostart", "true")
    names = [c[0] for c in incus.mock_calls]
    final_acl = max(
        i
        for i, c in enumerate(incus.mock_calls)
        if c[0] == "network_acl_set_yaml" and c.args[0] == ll.EGRESS_ACL
    )
    assert final_acl < names.index("config_device_add")


def test_up_does_not_touch_an_existing_state_mount():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.config_device_add.assert_not_called()
    assert not any(c.args[1:2] == ("boot.autostart",) for c in incus.config_set.call_args_list)


def _services_acl_with_rule() -> str:
    return yaml.safe_dump(
        {
            "name": "jailbee-services",
            "egress": [
                {
                    "action": "allow",
                    "destination": "10.79.115.3/32",
                    "protocol": "tcp",
                    "description": "jailbee LiteLLM proxy",
                }
            ],
        }
    )


def test_reconcile_drops_a_rule_whose_container_is_gone():
    incus = _incus(present=False)
    incus.network_acl_show.return_value = _services_acl_with_rule()
    assert ll.reconcile_services_acl(incus) is True
    written = incus.network_acl_set_yaml.call_args
    assert written.args[0] == "jailbee-services"
    assert yaml.safe_load(written.args[1])["egress"] == []


def test_reconcile_services_acl_keeps_the_egress_proxy_rule():
    incus = _incus(present=False)
    incus.network_acl_show.return_value = services_acl_yaml(
        {
            "jailbee LiteLLM proxy": (["10.79.115.3"], [4000]),
            "jailbee egress proxy": (["10.1.0.2"], [3128]),
        }
    )
    assert ll.reconcile_services_acl(incus) is True
    written = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert [(r["description"], r["destination"]) for r in written["egress"]] == [
        ("jailbee egress proxy", "10.1.0.2/32")
    ]


def test_reconcile_ignores_an_acl_holding_only_the_egress_proxy_rule():
    incus = _incus(present=False)
    incus.network_acl_show.return_value = services_acl_yaml(
        {"jailbee egress proxy": (["10.1.0.2"], [3128])}
    )
    assert ll.reconcile_services_acl(incus) is False
    incus.network_acl_set_yaml.assert_not_called()


def test_reconcile_keeps_the_rule_while_the_container_exists():
    incus = _incus(present=True, running=False)
    incus.network_acl_show.return_value = _services_acl_with_rule()
    assert ll.reconcile_services_acl(incus) is False
    incus.network_acl_set_yaml.assert_not_called()


def test_reconcile_leaves_an_already_empty_or_missing_acl_alone():
    incus = _incus(present=False)
    incus.network_acl_show.return_value = yaml.safe_dump({"name": "jailbee-services", "egress": []})
    assert ll.reconcile_services_acl(incus) is False
    incus.network_acl_exists.return_value = False
    assert ll.reconcile_services_acl(incus) is False
    incus.network_acl_set_yaml.assert_not_called()


@pytest.mark.parametrize(
    ("acls", "expected"),
    [
        ({"incusbr0": "r-allowlist,jailbee-services", "jailbee-work": ""}, []),
        ({"incusbr0": "r-allowlist", "jailbee-work": "r-allowlist"}, ["incusbr0", "jailbee-work"]),
        ({"incusbr0": "", "jailbee-work": "r-allowlist,jailbee-services"}, []),  # unmanaged
    ],
)
def test_bridges_missing_services_acl(acls, expected):
    incus = MagicMock()
    incus.network_exists.return_value = True
    incus.network_get.side_effect = lambda bridge, _key: acls[bridge]
    assert ll.bridges_missing_services_acl(incus) == expected


def test_bridges_missing_services_acl_skips_absent_bridges():
    incus = MagicMock()
    incus.network_exists.side_effect = lambda bridge: bridge == "incusbr0"
    incus.network_get.return_value = "r-allowlist"
    assert ll.bridges_missing_services_acl(incus) == ["incusbr0"]


def test_version_mismatch_reinstalls():
    incus = _incus(present=True, installed="1.90.0")
    result = ll.litellm_up(incus, _gcfg())
    assert result.installed is True


def test_down_empties_services_rule_and_deletes_container():
    incus = _incus(present=True)
    ll.litellm_down(incus)
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    services = [
        c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == "jailbee-services"
    ]
    assert yaml.safe_load(services[-1].args[1])["egress"] == []


def test_status_missing():
    status = ll.litellm_status(_incus(present=False), _gcfg())
    assert status.container == ll.ContainerState.MISSING


def test_status_running_reports_instance_and_login(xdg):
    incus = _incus(present=True, login="present")
    ll.litellm_up(incus, _gcfg())
    status = ll.litellm_status(incus, _gcfg())
    assert status.container == ll.ContainerState.RUNNING
    assert status.version == "1.103.1"
    assert status.instances == [
        ll.InstanceStatus(account="default", port=4100, active=True, healthy=True, login="present")
    ]


def test_service_limits_token_file_mode(tmp_path: Path):
    unit = resources.files("jailbee.provision").joinpath("litellm", "jailbee-litellm@.service")
    mask_lines = [line for line in unit.read_text().splitlines() if line.startswith("UMask=")]
    assert mask_lines == ["UMask=0077"]
    # Emulate a file created by the proxy under the service's declared mask.
    old_mask = os.umask(int(mask_lines[0].split("=", 1)[1], 8))
    try:
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token": "example"}')
        assert auth_file.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_mask)


def test_install_uses_only_hash_locked_requirements_by_default():
    script = resources.files("jailbee.provision").joinpath("litellm", "install.sh")
    assert (
        "pip install --require-hashes --no-deps -r /root/litellm-requirements.lock"
        in script.read_text()
    )
    assert "pip install --upgrade pip" not in script.read_text()


def test_unlocked_version_is_shell_quoted():
    incus = _incus(present=False)
    malicious = "1.103.1; touch /root/unwanted"
    ll._provision(incus, malicious, pinned=False)
    command = incus.exec_with_input.call_args.args[2]
    assert "JAILBEE_LITELLM_UNLOCKED_VERSION='1.103.1; touch /root/unwanted'" in command


def test_up_fails_closed_to_dev_containers_on_install_error():
    incus = _incus(present=False)
    incus.exec.side_effect = IncusError("apt unavailable")
    with pytest.raises(IncusError, match="apt unavailable"):
        ll.litellm_up(incus, _gcfg())
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)


@pytest.mark.parametrize(
    "operation",
    ["init", "profile_assign", "start"],
)
def test_new_container_setup_failure_deletes_possible_unrestricted_instance(operation: str):
    incus = _incus(present=False)
    startup_error = IncusError(f"{operation} reported failure")
    getattr(incus, operation).side_effect = startup_error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is startup_error
    # `init`/`start` can report an error after taking effect. Never infer
    # absence from the error or an out-of-date container listing.
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)


def test_ambiguous_start_failure_does_not_leave_autostarting_container():
    incus = _incus(present=False)
    start_error = IncusError("start timed out after instance reached Running")

    def start_then_fail(_name):
        incus.list_containers.return_value = [{"name": ll.LITELLM_CONTAINER, "status": "Running"}]
        raise start_error

    def delete_instance(_name, *, force):
        assert force
        incus.list_containers.return_value = []

    incus.start.side_effect = start_then_fail
    incus.delete.side_effect = delete_instance
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is start_error
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    assert incus.list_containers() == []


def test_new_container_autostart_is_enabled_only_after_acl_is_attached():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    restricted = [
        idx
        for idx, call in enumerate(incus.mock_calls)
        if call[0] == "profile_set_yaml"
        and yaml.safe_load(call.args[1])["devices"]["eth0"].get("security.acls") == ll.EGRESS_ACL
    ]
    autostart = [
        idx
        for idx, call in enumerate(incus.mock_calls)
        if call[0] == "config_set" and call.args[1:] == ("boot.autostart", "true")
    ]
    assert len(autostart) == 1
    assert restricted and restricted[-1] < autostart[0]


def test_ambiguous_autostart_failure_leaves_restrictive_acl_attached():
    incus = _incus(present=False)
    error = IncusError("config_set timed out after enabling autostart")
    incus.config_set.side_effect = error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL
    assert profile["security.acls.default.egress.action"] == "reject"


def test_failed_new_container_delete_disables_autostart_before_force_stop():
    incus = _incus(present=False)
    startup_error = IncusError("start timed out")
    incus.start.side_effect = startup_error
    incus.delete.side_effect = IncusError("delete failed")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is startup_error
    incus.config_set.assert_any_call(ll.LITELLM_CONTAINER, "boot.autostart", "false")
    incus.stop.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    assert "delete failed" in str(caught.value)


def test_failed_new_container_cleanup_reports_unrestricted_instance():
    incus = _incus(present=False)
    startup_error = IncusError("start timed out")
    incus.start.side_effect = startup_error
    incus.delete.side_effect = IncusError("delete failed")
    incus.stop.side_effect = IncusError("stop failed")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is startup_error
    assert "SECURITY" in str(caught.value)
    assert "stop failed" in str(caught.value)


@pytest.mark.parametrize("present,reinstall", [(False, False), (True, True)])
def test_failed_install_restores_restrictive_nic_acl(present: bool, reinstall: bool):
    incus = _incus(present=present)
    incus.network_acl_exists.return_value = False
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=reinstall)
    assert caught.value is install_error
    acl_calls = [
        call for call in incus.network_acl_set_yaml.call_args_list if call.args[0] == ll.EGRESS_ACL
    ]
    assert len(acl_calls) >= 2  # package ACL, then DHCP/DNS-only recovery
    acl = yaml.safe_load(acl_calls[-1].args[1])
    assert all(rule["destination_port"] in {"67", "547", "53"} for rule in acl["egress"])
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL
    assert profile["security.acls.default.egress.action"] == "reject"
    assert profile["security.acls.default.ingress.action"] == "reject"
    assert all(
        call.args[0] != "jailbee-services" for call in incus.network_acl_set_yaml.call_args_list
    )
    if present:
        incus.stop.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)
    else:
        incus.stop.assert_not_called()


def test_failed_acl_restore_force_stops_container_without_masking_install_error():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = [None, IncusError("ACL edit unavailable")]
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert caught.value is install_error
    assert any("ACL edit unavailable" in note for note in caught.value.__notes__)
    assert "ACL edit unavailable" in str(caught.value)
    assert "force-stopped" in str(caught.value)
    assert incus.stop.call_count >= 1
    incus.config_set.assert_any_call(ll.LITELLM_CONTAINER, "boot.autostart", "false")


def test_failed_acl_restore_deletes_if_autostart_cannot_be_disabled():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = [None, IncusError("ACL edit unavailable")]
    incus.config_set.side_effect = [None, IncusError("autostart disable unavailable")]
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert caught.value is install_error
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)


def test_failed_acl_restore_deletes_container_if_force_stop_fails():
    incus = _incus(present=False)
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = [None, IncusError("ACL edit unavailable")]
    incus.stop.side_effect = IncusError("stop unavailable")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is install_error
    incus.delete.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)


def test_failed_acl_restore_reports_if_even_delete_fails():
    incus = _incus(present=True)
    install_error = IncusError("apt unavailable")
    incus.exec_with_input.side_effect = install_error
    incus.network_acl_set_yaml.side_effect = [None, IncusError("ACL edit unavailable")]
    incus.stop.side_effect = [None, IncusError("stop unavailable")]
    incus.delete.side_effect = IncusError("delete unavailable")
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert caught.value is install_error
    assert "stop unavailable" in str(caught.value.__notes__)
    assert "delete unavailable" in str(caught.value.__notes__)
    assert "SECURITY" in str(caught.value)
    assert "delete unavailable" in str(caught.value)


def test_failed_acl_resolution_after_install_restricts_container(monkeypatch: pytest.MonkeyPatch):
    incus = _incus(present=False)

    resolver = ll._resolve_egress

    def unavailable(hosts):
        if hosts == ll.egress_hosts(_gcfg().litellm):
            raise OSError("DNS unavailable")
        return resolver(hosts)

    monkeypatch.setattr(ll, "_resolve_egress", unavailable)
    with pytest.raises(OSError, match="DNS unavailable"):
        ll.litellm_up(incus, _gcfg())
    acl = yaml.safe_load(incus.network_acl_set_yaml.call_args.args[1])
    assert all(rule["destination_port"] in {"67", "547", "53"} for rule in acl["egress"])
    assert all("destination" in rule for rule in acl["egress"])
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL
    assert all(c.args[0] != "jailbee-services" for c in incus.network_acl_set_yaml.call_args_list)


def test_every_proxy_acl_write_pins_dns_and_dhcp_to_the_bridge():
    """Install-time and final ACLs alike: no destination-less infrastructure rule."""
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    writes = [
        yaml.safe_load(c.args[1])
        for c in incus.network_acl_set_yaml.call_args_list
        if c.args[0] == ll.EGRESS_ACL
    ]
    assert len(writes) >= 2
    for acl in writes:
        infra = [r for r in acl["egress"] if not r["description"].startswith("allowlisted: ")]
        assert infra
        assert all("destination" in r for r in infra)
        dns = [r for r in infra if r["destination_port"] == "53"]
        assert dns and all(r["destination"].startswith("10.79.115.1/32") for r in dns)


def test_failed_acl_write_after_install_retries_restrictive_acl():
    incus = _incus(present=False)
    error = IncusError("ACL write unavailable")
    incus.network_acl_set_yaml.side_effect = [None, error, None]
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    assert incus.network_acl_set_yaml.call_count == 3
    profile = yaml.safe_load(incus.profile_set_yaml.call_args.args[1])["devices"]["eth0"]
    assert profile["security.acls"] == ll.EGRESS_ACL


def test_up_requires_static_bridge_address():
    incus = _incus(present=False)
    incus.network_get.return_value = "none"
    with pytest.raises(RuntimeError, match="static address"):
        ll.litellm_up(incus, _gcfg())
    incus.init.assert_not_called()


def test_status_stopped_does_not_probe_container():
    incus = _incus(present=True, running=False)
    assert ll.litellm_status(incus, _gcfg()) == ll.LiteLLMStatus(
        container=ll.ContainerState.STOPPED, ip="10.79.115.3", version=None, instances=[]
    )
    incus.exec.assert_not_called()


def test_login_runs_litellm_device_flow_with_private_umask(tmp_path: Path):
    incus = _incus(present=True)
    incus.exec_interactive.return_value = 0
    assert ll.litellm_login(incus, "default") == 0
    name, cmd = incus.exec_interactive.call_args.args
    script = cmd[-1]
    assert name == ll.LITELLM_CONTAINER
    assert cmd[:2] == ["bash", "-c"]
    assert ". /var/lib/jailbee-litellm/default/instance.env" in script
    assert "Authenticator().get_access_token()" in script
    # Run the login shell prefix with a harmless stand-in for Authenticator.
    # A newly created auth.json in the bind-mounted host directory must be 0600.
    env_file = tmp_path / "instance.env"
    env_file.write_text("CHATGPT_TOKEN_DIR=/var/lib/jailbee-litellm/default/auth\n")
    prefix = script.split("exec ", 1)[0].replace(
        "/var/lib/jailbee-litellm/default/instance.env", str(env_file)
    )
    auth_file = tmp_path / "auth.json"
    subprocess.run(
        ["bash", "-c", prefix + f'python -c \'open("{auth_file}", "w").write("token")\''],
        check=True,
    )
    assert auth_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "contents",
    [None, "", "CHATGPT_TOKEN_DIR=/tmp/unmounted/auth\n", "false\n"],
    ids=["missing", "empty-despite-inherited-env", "wrong-token-directory", "invalid-script"],
)
def test_login_does_not_invoke_authenticator_if_env_file_is_invalid(
    tmp_path: Path, contents: str | None
):
    incus = _incus(present=True)
    ll.litellm_login(incus, "default")
    command = incus.exec_interactive.call_args.args[1][-1]
    env_file = tmp_path / "instance.env"
    if contents is not None:
        env_file.write_text(contents)
    marker = tmp_path / "authenticator-invoked"
    # Execute the emitted shell command with Authenticator replaced by a
    # harmless marker. No real Incus or authentication subprocess is started.
    script = command.replace("/var/lib/jailbee-litellm/default/instance.env", str(env_file))
    script = script.split("exec ", 1)[0] + f"exec touch {marker}"
    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        env={
            "PATH": os.environ["PATH"],
            "CHATGPT_TOKEN_DIR": "/var/lib/jailbee-litellm/default/auth",
        },
    )
    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize("present,running", [(False, True), (True, False)])
def test_login_requires_running_container(present: bool, running: bool):
    incus = _incus(present=present, running=running)
    with pytest.raises(RuntimeError, match="jailbee litellm up"):
        ll.litellm_login(incus, "default")
    incus.exec_interactive.assert_not_called()


def test_logs_follow_flag():
    incus = _incus(present=True)
    incus.exec_interactive.return_value = 0
    assert ll.litellm_logs(incus, "default", follow=True) == 0
    assert incus.exec_interactive.call_args.args == (
        ll.LITELLM_CONTAINER,
        ["journalctl", "-u", "jailbee-litellm@default.service", "-n", "200", "--no-pager", "-f"],
    )


def test_stopped_container_gets_restrictive_acl_before_start():
    incus = _incus(present=True, running=False)
    ll.litellm_up(incus, _gcfg())
    start_at = next(i for i, call in enumerate(incus.mock_calls) if call[0] == "start")
    acl_at = next(
        i
        for i, call in enumerate(incus.mock_calls)
        if call[0] == "network_acl_set_yaml" and call.args[0] == ll.EGRESS_ACL
    )
    restricted_at = next(
        i
        for i, call in enumerate(incus.mock_calls)
        if call[0] == "profile_set_yaml"
        and yaml.safe_load(call.args[1])["devices"]["eth0"].get("security.acls") == ll.EGRESS_ACL
    )
    assert acl_at < restricted_at < start_at
    incus.delete.assert_not_called()


def test_stopped_container_ambiguous_start_failure_restricts_before_start():
    incus = _incus(present=True, running=False)
    error = IncusError("start timed out after container became Running")
    incus.start.side_effect = error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    start_at = next(i for i, call in enumerate(incus.mock_calls) if call[0] == "start")
    assert any(
        call[0] == "profile_set_yaml"
        and yaml.safe_load(call.args[1])["devices"]["eth0"].get("security.acls") == ll.EGRESS_ACL
        for call in incus.mock_calls[:start_at]
    )


def test_stopped_container_failed_acl_restore_force_stops_before_start():
    incus = _incus(present=True, running=False)
    error = IncusError("ACL setup failed")
    incus.network_acl_set_yaml.side_effect = error
    with pytest.raises(IncusError) as caught:
        ll.litellm_up(incus, _gcfg())
    assert caught.value is error
    incus.start.assert_not_called()
    incus.config_set.assert_any_call(ll.LITELLM_CONTAINER, "boot.autostart", "false")
    incus.stop.assert_called_once_with(ll.LITELLM_CONTAINER, force=True)


def test_sync_payload_none_when_disabled_or_absent():
    assert ll.container_sync_payload(_incus(present=True), _gcfg(enabled=False)) is None
    assert ll.container_sync_payload(_incus(present=False), _gcfg()) is None


def test_sync_payload_serving_nothing_still_names_the_unserved_profiles():
    """The docs' Accounts example, before `up` has brought either account up."""
    payload = ll.container_sync_payload(_incus(present=True), _gcfg(**_TWO))
    assert payload == {"json": None, "keys": {}, "unserved": ["codex", "work"]}
    assert "codex, work" in ll.unserved_warning(payload)
    assert "jailbee litellm up" in ll.unserved_warning(payload)


def test_a_payload_serving_nothing_retires_the_settings_like_none():
    incus = _incus(present=True)
    ll.sync_container(incus, "repo-branch", {"json": None, "keys": {}, "unserved": ["codex"]})
    script = incus.exec.call_args.args[1][-1]
    assert f"rm -f {ll.CONTAINER_FILE} {ll.CONTAINER_KEY_GLOB}" in script
    incus.exec_with_input.assert_not_called()


def test_apply_warns_and_retires_when_nothing_is_served(mocker):
    from jailbee import apply

    incus = _incus(present=True)
    warn = mocker.patch("jailbee.tui.warn")
    payload = apply._litellm_payload_or_warn(incus, _gcfg(**_TWO))
    assert "codex, work" in warn.call_args.args[0]
    ll.sync_container(incus, "repo-branch", payload)
    assert "rm -f" in incus.exec.call_args.args[1][-1]
    incus.exec_with_input.assert_not_called()


def test_unserved_warning_is_none_when_everything_is_served():
    assert ll.unserved_warning(None) is None
    assert ll.unserved_warning({"json": {}, "keys": {}, "unserved": []}) is None


def test_sync_payload_after_up():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    payload = ll.container_sync_payload(incus, _gcfg())
    assert payload is not None
    assert (payload["json"]["version"], payload["json"]["default_profile"]) == (1, "codex")
    assert payload["json"]["profiles"]["codex"]["base_url"] == "http://10.79.115.3:4100"
    assert payload["json"]["profiles"]["codex"]["key_file"] == "/etc/jailbee/litellm-default.key"
    assert str(payload["keys"]["default"]).endswith("default/master.key")
    assert payload["unserved"] == []
    assert "sk-jb-" not in repr(payload)


def test_sync_container_writes_json_and_key():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    payload = ll.container_sync_payload(incus, _gcfg())
    incus.exec_with_input.reset_mock()
    ll.sync_container(incus, "repo-branch", payload)
    name, cmd, script = incus.exec_with_input.call_args.args
    assert name == "repo-branch"
    assert cmd == ["bash", "-s"]
    assert "sk-jb-" not in repr(cmd)
    assert ll.CONTAINER_FILE in script and "/etc/jailbee/litellm-default.key" in script
    assert "chmod 0644" in script
    assert "chmod 0640" in script and "chown root:dev" in script
    assert "sk-jb-" in script


def test_sync_container_removes_when_none():
    incus = _incus(present=True)
    ll.sync_container(incus, "repo-branch", None)
    script = incus.exec.call_args.args[1][-1]
    assert f"rm -f {ll.CONTAINER_FILE} {ll.CONTAINER_KEY_GLOB}" in script
    incus.exec_with_input.assert_not_called()


def test_upstream_reachable_checks_proxy_container_only():
    incus = _incus(present=True)
    incus.exec.side_effect = None
    incus.exec.return_value = "ok\n"
    assert ll.upstream_reachable(incus, "chatgpt.com", 443)
    assert incus.exec.call_args.args[0] == ll.LITELLM_CONTAINER
    assert "chatgpt.com" in incus.exec.call_args.args[1][-1]
    assert "443" in incus.exec.call_args.args[1][-1]
    incus.exec.side_effect = IncusError("blocked")
    assert not ll.upstream_reachable(incus, "chatgpt.com", 443)


def test_state_lives_in_an_incus_volume_on_the_default_pool():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    incus.config_device_add.assert_called_once_with(
        ll.LITELLM_CONTAINER,
        "state",
        "disk",
        {"pool": "default", "source": ll.STATE_VOLUME, "path": ll.CONTAINER_STATE_DIR},
    )


def test_the_state_volume_is_created_before_it_is_attached():
    incus = _incus(present=False)
    incus.storage_volume_exists.return_value = False
    ll.litellm_up(incus, _gcfg())
    names = [c[0] for c in incus.mock_calls]
    incus.storage_volume_create.assert_called_once_with("default", ll.STATE_VOLUME)
    assert names.index("storage_volume_create") < names.index("config_device_add")


def test_a_default_profile_without_a_root_pool_is_an_error():
    incus = _incus(present=False)
    incus.profile_show.return_value = yaml.safe_dump({"devices": {}})
    with pytest.raises(RuntimeError, match="root disk pool"):
        ll.litellm_up(incus, _gcfg())
    incus.init.assert_not_called()


def test_the_proxy_profile_maps_no_host_uid():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    for call in incus.profile_set_yaml.call_args_list:
        assert "raw.idmap" not in yaml.safe_load(call.args[1])["config"]


def test_rendered_files_are_pushed_after_the_volume_and_restricted_egress():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    calls = incus.mock_calls
    mount = next(i for i, c in enumerate(calls) if c[0] == "config_device_add")
    # min: the FIRST push must already follow the attach (no provision script has base64 -d).
    push = min(
        i for i, c in enumerate(calls) if c[0] == "exec_with_input" and "base64 -d" in c.args[2]
    )
    final_acl = max(
        i
        for i, c in enumerate(calls)
        if c[0] == "network_acl_set_yaml" and c.args[0] == ll.EGRESS_ACL
    )
    assert final_acl < mount < push
    files = _pushed(incus)
    env = files[f"{ll.CONTAINER_STATE_DIR}/default/instance.env"]
    assert "PORT=4100" in env and "LITELLM_MASTER_KEY=sk-jb-" in env
    assert f"{ll.CONTAINER_STATE_DIR}/default/config.yaml" in files
    assert f"{ll.CONTAINER_STATE_DIR}/callback/jailbee_callback.py" in files


def test_no_secret_reaches_incus_argv():
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    argv = [repr(c.args[1]) for c in incus.exec.call_args_list]
    argv += [repr(c.args[1]) for c in incus.exec_with_input.call_args_list]
    assert not any("sk-jb-" in a for a in argv)


def test_host_keeps_no_rendered_file_or_token(xdg: Path):
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg())
    root = xdg / "jailbee" / "litellm"
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert names == [
        ".allocation.lock",
        "default/applied-hot.sha256",
        "default/applied.sha256",
        "default/master.key",
        "ports.json",
    ]


def test_a_quiet_up_still_pushes_the_files():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.exec_with_input.reset_mock()
    assert ll.litellm_up(incus, _gcfg()).restarted == []
    assert _pushed(incus)


def test_status_reads_the_login_inside_the_container():
    incus = _incus(present=True)
    healthy = incus.exec.side_effect
    incus.exec.side_effect = lambda n, c, **kw: (
        "present\n" if "auth.json" in " ".join(c) else healthy(n, c, **kw)
    )
    ll.litellm_up(incus, _gcfg())
    assert ll.litellm_status(incus, _gcfg()).instances[0].login == "present"


def test_login_state_is_unknown_when_the_probe_fails():
    incus = _incus(present=True)
    incus.exec.side_effect = IncusError("exec failed")
    assert ll.auth_state(incus, "default") == "unknown"


def test_logout_removes_the_token_inside_the_container():
    incus = _incus(present=True)
    incus.exec.side_effect = None
    incus.exec.return_value = "removed\n"
    assert ll.litellm_logout(incus, "default") is True
    name, cmd = incus.exec.call_args.args
    assert name == ll.LITELLM_CONTAINER
    assert f"{ll.CONTAINER_STATE_DIR}/default/auth/auth.json" in cmd[-1]
    incus.exec.return_value = ""
    assert ll.litellm_logout(incus, "default") is False


@pytest.mark.parametrize("present,running", [(False, True), (True, False)])
def test_logout_requires_a_running_proxy(present: bool, running: bool):
    with pytest.raises(RuntimeError, match="jailbee litellm up"):
        ll.litellm_logout(_incus(present=present, running=running), "default")


def test_down_keeps_the_volume_and_purge_deletes_it_after_the_container():
    incus = _incus(present=True)
    ll.litellm_down(incus)
    incus.storage_volume_delete.assert_not_called()
    incus = _incus(present=True)
    ll.litellm_down(incus, purge=True)
    names = [c[0] for c in incus.mock_calls]
    incus.storage_volume_delete.assert_called_once_with("default", ll.STATE_VOLUME)
    assert names.index("delete") < names.index("storage_volume_delete")


def test_up_runs_one_instance_per_account_on_its_own_port():
    incus = _incus(present=False)
    result = ll.litellm_up(incus, _gcfg(**_TWO))
    assert result.ports == {"personal": 4100, "work": 4101}
    assert result.restarted == ["personal", "work"]
    restarts = [e for e in _execs(incus) if "systemctl restart" in e]
    assert restarts == [
        "systemctl restart jailbee-litellm@personal.service",
        "systemctl restart jailbee-litellm@work.service",
    ]
    services = [
        c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == "jailbee-services"
    ]
    ports = {r["destination_port"] for r in yaml.safe_load(services[-1].args[1])["egress"]}
    assert ports == {"4100", "4101"}
    own = [c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == ll.EGRESS_ACL]
    listen = {r.get("destination_port") for r in yaml.safe_load(own[-1].args[1])["ingress"]}
    assert {"4100", "4101"} <= listen
    files = _pushed(incus)
    work = yaml.safe_load(files[f"{ll.CONTAINER_STATE_DIR}/work/config.yaml"])
    assert {m["model_name"] for m in work["model_list"]} == {
        "jb-default-sol-low",
        "jb.work.capable",
        "claude-*",
    }


def test_up_restarts_only_the_account_whose_files_changed(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(**_TWO))
    incus.exec.reset_mock()
    _rotate_key(xdg, "work")
    assert ll.litellm_up(incus, _gcfg(**_TWO)).restarted == ["work"]


def test_up_retires_the_unit_of_a_removed_account():
    incus = _incus(present=True)
    healthy = incus.exec.side_effect
    scripts: list[str] = []
    listing = (
        # `systemctl list-units` half: a configured account and a removed one.
        "jailbee-litellm@default.service loaded active running JailBee LiteLLM proxy\n"
        "jailbee-litellm@old.service loaded active running JailBee LiteLLM proxy (old)\n"
        # `ls multi-user.target.wants/` half: the configured account's symlink and
        # a removed account that is only enabled, not loaded.
        "jailbee-litellm@default.service\n"
        "jailbee-litellm@enabled-only.service\n"
        "cloud-init.service\n"
    )

    def exec_(name, cmd, **kw):
        if "list-units" in " ".join(cmd):
            scripts.append(cmd[-1])
            return listing
        return healthy(name, cmd, **kw)

    incus.exec.side_effect = exec_
    result = ll.litellm_up(incus, _gcfg())
    assert "systemctl list-units" in scripts[0]
    assert "multi-user.target.wants" in scripts[0]
    assert result.retired == ["enabled-only", "old"]
    disabled = [e for e in _execs(incus) if "disable --now" in e]
    assert disabled == [
        "systemctl disable --now jailbee-litellm@enabled-only.service",
        "systemctl disable --now jailbee-litellm@old.service",
    ]


def _last_acl(incus: MagicMock, name: str) -> dict:
    writes = [c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == name]
    return yaml.safe_load(writes[-1].args[1])


def test_a_removed_accounts_port_leaves_both_acls():
    """`ports.json` keeps the removed account's port; neither ACL may still open it."""
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(**_TWO))
    assert ll.litellm_state.known_port("work") == 4101
    incus.network_acl_set_yaml.reset_mock()
    ll.litellm_up(incus, _gcfg(accounts=["personal"], profiles={"codex": {"account": "personal"}}))
    services = _last_acl(incus, "jailbee-services")
    assert {r["destination_port"] for r in services["egress"]} == {"4100"}
    listen = {r.get("destination_port") for r in _last_acl(incus, ll.EGRESS_ACL)["ingress"]}
    assert "4100" in listen and "4101" not in listen


def test_reinstall_retires_the_enabled_unit_of_a_removed_account():
    """A reinstall keeps the rootfs, and with it the removed account's enabled symlink."""
    incus = _incus(present=True)
    healthy = incus.exec.side_effect

    def exec_(name, cmd, **kw):
        if "list-units" in " ".join(cmd):
            return "jailbee-litellm@default.service\njailbee-litellm@old.service\n"
        return healthy(name, cmd, **kw)

    incus.exec.side_effect = exec_
    result = ll.litellm_up(incus, _gcfg(), reinstall=True)
    assert result.installed is True
    assert result.retired == ["old"]
    assert "systemctl disable --now jailbee-litellm@old.service" in _execs(incus)


def test_an_unhealthy_restart_records_no_stamp_and_the_next_up_restarts(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
):
    incus = _incus(present=True)
    wait_healthy = ll._wait_healthy

    def never_healthy(*_args, **_kw):
        raise RuntimeError("did not become healthy")

    monkeypatch.setattr(ll, "_wait_healthy", never_healthy)
    with pytest.raises(RuntimeError, match="did not become healthy"):
        ll.litellm_up(incus, _gcfg())
    assert not (xdg / "jailbee" / "litellm" / "default" / "applied.sha256").exists()

    monkeypatch.setattr(ll, "_wait_healthy", wait_healthy)
    incus.exec.reset_mock()
    assert ll.litellm_up(incus, _gcfg()).restarted == ["default"]
    assert "systemctl restart jailbee-litellm@default.service" in _execs(incus)


def test_resumed_up_pushes_the_state_only_after_reattaching_the_volume():
    """Installed, but a run was cut off before the attach: the `not needs_install` path."""
    incus = _incus(present=True)
    incus.config_show.return_value = yaml.safe_dump({"devices": {}})
    result = ll.litellm_up(incus, _gcfg())
    assert result.installed is False
    calls = incus.mock_calls
    mount = next(
        i for i, c in enumerate(calls) if c[0] == "config_device_add" and c.args[1] == "state"
    )
    # min: the FIRST push must already follow the attach.
    push = min(
        i for i, c in enumerate(calls) if c[0] == "exec_with_input" and "base64 -d" in c.args[2]
    )
    assert mount < push


def test_up_refuses_a_missing_secret_before_touching_incus():
    incus = _incus(present=False)
    cfg = _gcfg(routes={"kimi": _KIMI})
    with pytest.raises(LiteLLMInputError, match="OPENROUTER_API_KEY"):
        ll.litellm_up(incus, cfg)
    assert incus.mock_calls == []


def test_a_secret_reaches_only_the_pushed_instance_env(xdg: Path):
    _secrets(xdg, "OPENROUTER_API_KEY=sk-or-test-123\n")
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg(routes={"kimi": _KIMI}))
    env = _pushed(incus)[f"{ll.CONTAINER_STATE_DIR}/default/instance.env"]
    assert "OPENROUTER_API_KEY='sk-or-test-123'" in env
    config = _pushed(incus)[f"{ll.CONTAINER_STATE_DIR}/default/config.yaml"]
    assert "sk-or-test-123" not in config
    argv = [
        repr(c.args[1]) for c in incus.exec.call_args_list + incus.exec_with_input.call_args_list
    ]
    assert not any("sk-or-test" in a for a in argv)
    on_disk = [
        p
        for p in xdg.rglob("*")
        if p.is_file() and p.name != "secrets.env" and b"sk-or-test" in p.read_bytes()
    ]
    assert on_disk == []


def test_a_changed_secret_restarts_the_instance(xdg: Path):
    _secrets(xdg, "OPENROUTER_API_KEY=sk-or-1\n")
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(routes={"kimi": _KIMI}))
    _secrets(xdg, "OPENROUTER_API_KEY=sk-or-2\n")
    assert ll.litellm_up(incus, _gcfg(routes={"kimi": _KIMI})).restarted == ["default"]


def test_up_allows_the_providers_of_every_served_route(xdg: Path):
    _secrets(xdg, "OPENROUTER_API_KEY=sk-or-1\n")
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg(routes={"kimi": _KIMI}, egress=["10.0.0.5:11434"]))
    own = [c for c in incus.network_acl_set_yaml.call_args_list if c.args[0] == ll.EGRESS_ACL]
    described = {
        r.get("description", "").removeprefix("allowlisted: ")
        for r in yaml.safe_load(own[-1].args[1])["egress"]
    }
    assert {"openrouter.ai:443", "chatgpt.com:443", "10.0.0.5:11434"} <= described


def test_the_extra_fragment_reaches_every_instance(tmp_path: Path):
    extra = tmp_path / "extra.yaml"
    extra.write_text("router_settings: {num_retries: 3}\n")
    incus = _incus(present=False)
    ll.litellm_up(incus, _gcfg(**_TWO, extra=str(extra)))
    for account in ("personal", "work"):
        config = yaml.safe_load(_pushed(incus)[f"{ll.CONTAINER_STATE_DIR}/{account}/config.yaml"])
        assert config["router_settings"] == {"num_retries": 3}


def test_status_lists_every_configured_account():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(**_TWO))
    status = ll.litellm_status(incus, _gcfg(**_TWO))
    assert [(i.account, i.port) for i in status.instances] == [("personal", 4100), ("work", 4101)]


def test_status_of_an_account_never_brought_up_has_no_port():
    incus = _incus(present=True)
    status = ll.litellm_status(incus, _gcfg(accounts=["default", "spare"]))
    assert [i.port for i in status.instances] == [None, None]


def test_sync_payload_gives_each_profile_its_account_and_key():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(**_TWO))
    payload = ll.container_sync_payload(incus, _gcfg(**_TWO))
    profiles = payload["json"]["profiles"]
    assert profiles["codex"]["base_url"] == "http://10.79.115.3:4100"
    assert profiles["work"]["key_file"] == "/etc/jailbee/litellm-work.key"
    assert sorted(payload["keys"]) == ["personal", "work"]
    assert "sk-jb-" not in repr(payload)


def test_sync_payload_names_profiles_whose_account_has_no_instance():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())  # only `default` exists
    cfg = _gcfg(
        accounts=["default", "work"],
        routes={"sol-low": {"model": "chatgpt/gpt-6.1-sol", "effort": "low"}},
        profiles={"work": {"account": "work", "opus": "sol-low"}},
    )
    payload = ll.container_sync_payload(incus, cfg)
    assert set(payload["json"]["profiles"]) == {"codex"}
    assert ll.unserved_profiles(payload) == ["work"]


def test_sync_container_writes_every_key_and_retires_stale_ones():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(**_TWO))
    payload = ll.container_sync_payload(incus, _gcfg(**_TWO))
    incus.exec_with_input.reset_mock()
    ll.sync_container(incus, "repo-branch", payload)
    script = incus.exec_with_input.call_args.args[2]
    assert "/etc/jailbee/litellm-personal.key" in script
    assert "/etc/jailbee/litellm-work.key" in script
    publish = script.index(f'mv "$tmp" {ll.CONTAINER_FILE}')
    for account in ("personal", "work"):
        assert script.index(f'mv "$tmp" /etc/jailbee/litellm-{account}.key') < publish
    # The old JSON may still name a stale key until the new one replaces it.
    assert publish < script.index(f"for f in {ll.CONTAINER_KEY_GLOB}")


def test_the_stale_key_loop_removes_only_unlisted_keys(tmp_path: Path):
    for name in ("litellm-default.key", "litellm-old.key", "litellm.json"):
        (tmp_path / name).write_text("x")
    loop = ll._stale_key_loop(
        [str(tmp_path / "litellm-default.key")], str(tmp_path / "litellm-*.key")
    )
    subprocess.run(["bash", "-c", loop], check=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["litellm-default.key", "litellm.json"]


def _repo(xdg: Path, prefix: str, litellm: dict) -> Path:
    path = xdg / "config" / "jailbee" / "repos" / f"{prefix}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"litellm": litellm}))
    return path


def test_up_renders_every_repo_scope(xdg):
    _repo(xdg, "myrepo", {"routes": {"sol-high": {"effort": "max"}}})
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    config = yaml.safe_load(_pushed(incus)[f"{ll.CONTAINER_STATE_DIR}/default/config.yaml"])
    names = {m["model_name"] for m in config["model_list"]}
    assert {"jb-default-sol-high", "jb-myrepo.sol-high"} <= names


def test_up_skips_a_broken_override_and_reports_it(xdg):
    broken = _repo(xdg, "broken", {"profiles": {"codex": {"opus": "gone"}}})
    _repo(xdg, "good", {"routes": {"sol-high": {"effort": "max"}}})
    incus = _incus(present=True)
    result = ll.litellm_up(incus, _gcfg())
    names = {
        m["model_name"]
        for m in yaml.safe_load(_pushed(incus)[f"{ll.CONTAINER_STATE_DIR}/default/config.yaml"])[
            "model_list"
        ]
    }
    assert "jb-good.sol-high" in names
    assert not any(n.startswith("jb-broken.") for n in names)
    assert len(result.issues) == 1 and str(broken) in result.issues[0]


def test_up_reads_a_secret_only_a_repo_scope_references(xdg):
    _repo(
        xdg,
        "myrepo",
        {
            "routes": {"kimi": _KIMI},
            "profiles": {"kimi": {"opus": "kimi"}},
        },
    )
    incus = _incus(present=True)
    with pytest.raises(LiteLLMInputError, match="OPENROUTER_API_KEY"):
        ll.litellm_up(incus, _gcfg())
    _secrets(xdg, "OPENROUTER_API_KEY=sk-or-test\n")
    ll.litellm_up(incus, _gcfg())
    env = _pushed(incus)[f"{ll.CONTAINER_STATE_DIR}/default/instance.env"]
    assert "OPENROUTER_API_KEY='sk-or-test'" in env


def test_sync_payload_for_a_repo_view_uses_its_scope_and_default_profile():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    host = _gcfg().litellm
    view = LiteLLMRepoView(
        config=host.with_overlay(
            LiteLLMRepoOverlay.model_validate(
                {
                    "routes": {"sol-high": {"effort": "max"}},
                    "profiles": {"lean": {"account": "default", "opus": "sol-medium"}},
                    "default_profile": "lean",
                }
            )
        ),
        scope="myrepo",
        origin="/x/repos/myrepo.yaml",
    )
    payload = ll.container_sync_payload(incus, _gcfg(), view=view)
    assert payload["json"]["default_profile"] == "lean"
    assert payload["json"]["profiles"]["codex"]["tiers"]["opus"] == "jb-myrepo.codex.capable"
    assert set(payload["json"]["profiles"]) == {"codex", "lean"}


def test_sync_payload_for_a_view_without_own_scope_uses_host_aliases():
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    view = LiteLLMRepoView(config=_gcfg().litellm, scope=None, origin="/x/repos/r.yaml")
    payload = ll.container_sync_payload(incus, _gcfg(), view=view)
    assert payload["json"]["profiles"]["codex"]["tiers"]["opus"] == "jb.codex.capable"


def _restarts(incus: MagicMock) -> list[str]:
    return [cmd for cmd in _execs(incus) if cmd.startswith("systemctl restart")]


def test_reconcile_is_none_when_disabled_missing_or_stopped():
    assert ll.litellm_reconcile(_incus(present=True), _gcfg(enabled=False)) is None
    assert ll.litellm_reconcile(_incus(present=False), _gcfg()) is None
    assert ll.litellm_reconcile(_incus(present=True, running=False), _gcfg()) is None


def test_reconcile_after_up_changes_nothing(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.reset_mock(return_value=False, side_effect=False)
    result = ll.litellm_reconcile(incus, _gcfg())
    assert result == ll.ReconcileResult()
    assert _restarts(incus) == []
    incus.exec_with_input.assert_not_called()


def test_reconcile_restarts_only_the_account_whose_cold_files_changed(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(**_TWO))
    incus.reset_mock(return_value=False, side_effect=False)
    _rotate_key(xdg, "work")
    result = ll.litellm_reconcile(incus, _gcfg(**_TWO))
    assert result.restarted == ["work"]
    assert _restarts(incus) == [f"systemctl restart {ll.unit('work')}"]
    assert f"{ll.CONTAINER_STATE_DIR}/personal/config.yaml" not in _pushed(incus)


def _keyed_scope(host):
    """A repo scope whose override adds a route naming a secret nobody defines."""
    scope = host.with_overlay(
        LiteLLMRepoOverlay.model_validate(
            {
                "routes": {
                    "mine": {
                        "model": "openrouter/x/y",
                        "context_window": 1000,
                        "api_key": "MINE_KEY",
                    }
                }
            }
        )
    )
    return {"app": scope}, "/h/repos/app.yaml"


def test_up_names_the_repo_file_behind_a_missing_secret(xdg, monkeypatch):
    gcfg = _gcfg()
    scopes, label = _keyed_scope(gcfg.litellm)
    monkeypatch.setattr(ll, "local_litellm_scopes", lambda cfg: (scopes, []))
    monkeypatch.setattr(ll, "scope_files", lambda s: [label])
    with pytest.raises(LiteLLMInputError, match=r"MINE_KEY.*named by /h/repos/app\.yaml"):
        ll.litellm_up(_incus(present=False), gcfg)


def test_reconcile_names_the_repo_file_behind_a_missing_secret(xdg, monkeypatch):
    gcfg = _gcfg()
    incus = _incus(present=True)
    ll.litellm_up(incus, gcfg)
    scopes, label = _keyed_scope(gcfg.litellm)
    monkeypatch.setattr(ll, "local_litellm_scopes", lambda cfg: (scopes, []))
    monkeypatch.setattr(ll, "scope_files", lambda s: [label])
    with pytest.raises(LiteLLMInputError, match=r"MINE_KEY.*named by /h/repos/app\.yaml"):
        ll.litellm_reconcile(incus, gcfg)


def test_reconcile_picks_up_a_new_repo_scope(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    _repo(xdg, "myrepo", {"routes": {"sol-high": {"effort": "max"}}})
    incus.reset_mock(return_value=False, side_effect=False)
    result = ll.litellm_reconcile(incus, _gcfg())
    assert (result.reloaded, result.restarted) == (["default"], [])
    config = _pushed(incus)[f"{ll.CONTAINER_STATE_DIR}/default/config.yaml"]
    assert "jb-myrepo.sol-high" in config


def test_reconcile_without_restart_touches_nothing_and_stays_pending(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    _rotate_key(xdg)
    changed = _gcfg()
    incus.reset_mock(return_value=False, side_effect=False)
    first = ll.litellm_reconcile(incus, changed, restart=False)
    assert (first.pending, first.restarted) == (["default"], [])
    assert _restarts(incus) == []
    incus.exec_with_input.assert_not_called()
    incus.network_acl_set_yaml.assert_not_called()
    second = ll.litellm_reconcile(incus, changed)
    assert second.restarted == ["default"]


def test_reconcile_without_restart_tells_a_stopped_instance_from_a_changed_one(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    real_exec = incus.exec.side_effect

    def down(name, cmd, **kw):
        return "inactive\n" if "is-active" in " ".join(cmd) else real_exec(name, cmd, **kw)

    incus.exec.side_effect = down
    result = ll.litellm_reconcile(incus, _gcfg(), restart=False)
    assert (result.pending, result.stopped) == (["default"], ["default"])
    incus.exec.side_effect = real_exec
    _rotate_key(xdg)
    changed = ll.litellm_reconcile(incus, _gcfg(), restart=False)
    assert (changed.pending, changed.stopped) == (["default"], [])


def test_reconcile_leaves_structural_changes_to_up(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.reset_mock(return_value=False, side_effect=False)
    grown = _gcfg(accounts=["default", "work"])
    result = ll.litellm_reconcile(incus, grown)
    assert result.needs_up is not None and "work" in result.needs_up
    assert _restarts(incus) == []

    stale = _incus(present=True, installed="1.0.0")
    result = ll.litellm_reconcile(stale, _gcfg())
    assert result.needs_up is not None and "1.103.1" in result.needs_up


def test_reconcile_reports_a_broken_override_and_still_applies_the_rest(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    broken = _repo(xdg, "broken", {"profiles": {"codex": {"opus": "gone"}}})
    incus.reset_mock(return_value=False, side_effect=False)
    result = ll.litellm_reconcile(incus, _gcfg(routes={"sol-high": {"effort": "max"}}))
    assert result.reloaded == ["default"]
    assert len(result.issues) == 1 and str(broken) in result.issues[0]


def test_reconcile_leaves_a_detached_state_volume_to_up(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.reset_mock(return_value=False, side_effect=False)
    incus.config_show.return_value = yaml.safe_dump({"devices": {}})
    changed = _gcfg(routes={"sol-high": {"effort": "max"}})
    result = ll.litellm_reconcile(incus, changed)
    assert result.needs_up is not None and "state volume" in result.needs_up
    assert result.restarted == [] and result.pending == []
    incus.exec_with_input.assert_not_called()
    incus.network_acl_set_yaml.assert_not_called()
    assert _restarts(incus) == []
    # No digest was recorded: once the volume is back, the change is still seen.
    incus.config_show.return_value = yaml.safe_dump({"devices": {"state": {"type": "disk"}}})
    assert ll.litellm_reconcile(incus, changed).reloaded == ["default"]


def test_up_reloads_instead_of_restarting_when_only_routes_changed(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.exec.reset_mock()
    result = ll.litellm_up(incus, _gcfg(routes={"sol-high": {"effort": "max"}}))
    assert (result.restarted, result.reloaded) == ([], ["default"])
    assert not any("systemctl restart" in e for e in _execs(incus))


def test_up_restarts_when_the_callback_source_changed(xdg, monkeypatch):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    real = ll._read
    monkeypatch.setattr(
        ll, "_read", lambda n: real(n) + "\n# new\n" if n == "jailbee_callback.py" else real(n)
    )
    result = ll.litellm_up(incus, _gcfg())
    assert (result.restarted, result.reloaded) == (["default"], [])


@pytest.mark.parametrize("ack", ["none", "error", "stale"])
def test_up_falls_back_to_a_restart_when_the_reload_is_not_confirmed(xdg, ack):
    ll.litellm_up(_incus(present=True), _gcfg())
    incus = _incus(present=True, ack=ack)
    result = ll.litellm_up(incus, _gcfg(routes={"sol-high": {"effort": "max"}}))
    assert (result.restarted, result.reloaded) == (["default"], [])
    assert ("refused" if ack == "error" else "did not acknowledge") in result.fallbacks["default"]
    assert sum("systemctl restart" in e for e in _execs(incus)) == 1


def test_up_keeps_polling_until_the_ack_matches(xdg):
    ll.litellm_up(_incus(present=True), _gcfg())
    incus = _incus(present=True, ack="late")
    result = ll.litellm_up(incus, _gcfg(routes={"sol-high": {"effort": "max"}}))
    assert (result.restarted, result.reloaded, result.fallbacks) == ([], ["default"], {})
    assert sum(e.endswith("/applied.json") for e in _execs(incus)) == 2


def test_a_restart_records_the_hot_stamp_so_the_next_run_is_quiet(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    _rotate_key(xdg)
    assert ll.litellm_up(incus, _gcfg()).restarted == ["default"]
    again = ll.litellm_up(incus, _gcfg())
    assert (again.restarted, again.reloaded) == ([], [])


def test_reconcile_reloads_only_the_account_whose_routes_changed(xdg):
    two = {**_TWO, "routes": {"sol-low": {"model": "chatgpt/gpt-6.1-sol", "effort": "low"}}}
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg(**two))
    incus.reset_mock(return_value=False, side_effect=False)
    changed = {**two, "routes": {"sol-low": {"model": "chatgpt/gpt-6.1-sol", "effort": "high"}}}
    result = ll.litellm_reconcile(incus, _gcfg(**changed))
    assert (result.reloaded, result.restarted) == (["work"], [])
    assert _restarts(incus) == []
    assert f"{ll.CONTAINER_STATE_DIR}/personal/hot.json" not in _pushed(incus)


def test_reconcile_without_restart_still_reloads(xdg):
    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    incus.reset_mock(return_value=False, side_effect=False)
    result = ll.litellm_reconcile(
        incus, _gcfg(routes={"sol-high": {"effort": "max"}}), restart=False
    )
    assert (result.reloaded, result.pending, result.restarted) == (["default"], [], [])
    assert _restarts(incus) == []


def test_reconcile_without_restart_leaves_an_unconfirmed_reload_pending_then_retries(xdg):
    ll.litellm_up(_incus(present=True), _gcfg())
    changed = _gcfg(routes={"sol-high": {"effort": "max"}})
    stuck = _incus(present=True, ack="none")
    first = ll.litellm_reconcile(stuck, changed, restart=False)
    assert (first.pending, first.reloaded) == (["default"], [])
    assert "did not acknowledge" in first.fallbacks["default"]
    assert _restarts(stuck) == []
    second = ll.litellm_reconcile(_incus(present=True), changed)
    assert (second.reloaded, second.restarted) == (["default"], [])


def test_reverting_after_an_unconfirmed_reload_pushes_the_old_routes_again(xdg):
    # The proxy may have taken the unconfirmed B; reverting to A must not be
    # skipped just because A was the last confirmed state.
    ll.litellm_up(_incus(present=True), _gcfg())
    stuck = _incus(present=True, ack="none")
    pending = ll.litellm_reconcile(
        stuck, _gcfg(routes={"sol-high": {"effort": "max"}}), restart=False
    )
    assert (pending.pending, pending.restarted) == (["default"], [])
    reverted = ll.litellm_reconcile(_incus(present=True), _gcfg(), restart=False)
    assert (reverted.reloaded, reverted.pending) == (["default"], [])


@pytest.mark.parametrize("restart", [True, False])
def test_a_hot_only_change_still_rewrites_the_egress_allowlist(xdg, monkeypatch, restart):
    from jailbee.litellm_render import egress_hosts

    incus = _incus(present=True)
    ll.litellm_up(incus, _gcfg())
    seen: list[list[str]] = []
    monkeypatch.setattr(
        ll, "_set_egress", lambda _i, entries, _ports: seen.append([e.description for e in entries])
    )
    route = {"model": "openai/x", "context_window": 1000, "api_base": "https://llm.example.com/v1"}
    gcfg = _gcfg(routes={"mine": route})
    result = ll.litellm_reconcile(incus, gcfg, restart=restart)
    expected = [h if ":" in h else f"{h}:443" for h in egress_hosts(gcfg.litellm)]
    assert (result.reloaded, result.restarted) == (["default"], [])
    assert seen
    assert seen[-1] == expected and any("llm.example.com" in h for h in expected)


_GROK_ROUTES = {"grok": {"model": "xai/grok-4.3", "oauth": True, "context_window": 256_000}}
_GROK_ONLY = {
    "routes": _GROK_ROUTES,
    "profiles": {
        "codex": {
            "account": "default",
            "fable": None,
            "opus": None,
            "sonnet": None,
            "haiku": "grok",
        },
    },
}


def _xai_state(incus: MagicMock, state: str) -> None:
    base = incus.exec.side_effect

    def exec_(name, cmd, **kw):
        if "xai-auth/auth.json" in " ".join(cmd):
            return f"{state}\n"
        return base(name, cmd, **kw)

    incus.exec.side_effect = exec_


def test_up_starts_an_xai_account_without_its_login_and_says_so():
    incus = _incus(present=True, login="missing")
    _xai_state(incus, "missing")
    result = ll.litellm_up(incus, _gcfg(**_GROK_ONLY))
    assert result.awaiting_login == [] and result.restarted == ["default"]
    assert result.missing_xai_login == ["default"]


def test_up_holds_back_a_mixed_account_without_either_login():
    mixed = {**_GROK_ONLY, "profiles": {"codex": {"account": "default", "haiku": "grok"}}}
    incus = _incus(present=True, login="missing")
    _xai_state(incus, "missing")
    result = ll.litellm_up(incus, _gcfg(**mixed))
    assert result.awaiting_login == ["default"] and result.missing_xai_login == []


def test_up_still_holds_back_a_chatgpt_account_without_its_login():
    incus = _incus(present=True, login="missing")
    result = ll.litellm_up(incus, _gcfg())
    assert result.awaiting_login == ["default"] and result.missing_xai_login == []


def test_auth_state_and_logout_use_the_provider_directory():
    incus = _incus(present=True)
    ll.auth_state(incus, "default", "xai")
    assert "/var/lib/jailbee-litellm/default/xai-auth/auth.json" in _execs(incus)[-1]
    ll.litellm_logout(incus, "default", "xai")
    assert "/var/lib/jailbee-litellm/default/xai-auth/auth.json" in incus.exec.call_args.args[1][-1]
    ll.litellm_logout(incus, "default")
    assert "/var/lib/jailbee-litellm/default/auth/auth.json" in incus.exec.call_args.args[1][-1]


def test_status_reports_the_xai_login_only_where_it_is_needed(xdg):
    incus = _incus(present=True, login="present")
    ll.litellm_up(incus, _gcfg())
    assert ll.litellm_status(incus, _gcfg()).instances[0].xai_login is None
    _xai_state(incus, "missing")
    assert ll.litellm_status(incus, _gcfg(**_GROK_ONLY)).instances[0].xai_login == "missing"


def _xai_login_incus(token_host: str = "auth.x.ai") -> MagicMock:
    incus = _incus(present=True)
    base = incus.exec.side_effect

    def exec_(name, cmd, **kw):
        if "openid-configuration" in " ".join(cmd):
            return f"{token_host}\n"
        return base(name, cmd, **kw)

    incus.exec.side_effect = exec_
    incus.exec_interactive.return_value = 0
    return incus


def test_xai_login_forwards_the_callback_port_for_the_login_only():
    incus = _xai_login_incus()
    assert ll.litellm_login_xai(incus, _gcfg(**_GROK_ONLY).litellm, "default") == 0
    incus.config_device_add.assert_called_once_with(
        ll.LITELLM_CONTAINER,
        "xai-login",
        "proxy",
        {"listen": "tcp:127.0.0.1:56121", "connect": "tcp:127.0.0.1:56121"},
    )
    removes = [c for c in incus.config_device_remove.call_args_list if c.args[1] == "xai-login"]
    assert len(removes) == 2  # a stale one first (missing_ok), then ours
    script = incus.exec_interactive.call_args.args[1][-1]
    assert "XAIOAuthAuthenticator().login(no_browser=True)" in script
    assert 'test "${XAI_OAUTH_TOKEN_DIR:-}" = /var/lib/jailbee-litellm/default/xai-auth' in script


def test_xai_login_removes_the_device_even_when_the_login_raises():
    incus = _xai_login_incus()
    incus.exec_interactive.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        ll.litellm_login_xai(incus, _gcfg(**_GROK_ONLY).litellm, "default")
    removes = [c for c in incus.config_device_remove.call_args_list if c.args[1] == "xai-login"]
    # The stale-device sweep first, then the cleanup; the sweep alone must not satisfy this.
    assert len(removes) == 2 and removes[-1].kwargs == {}


def test_xai_login_refuses_an_account_without_an_oauth_route():
    incus = _xai_login_incus()
    with pytest.raises(RuntimeError, match="oauth: true"):
        ll.litellm_login_xai(incus, _gcfg().litellm, "default")
    incus.config_device_add.assert_not_called()


def test_xai_login_names_a_moved_token_endpoint():
    incus = _xai_login_incus(token_host="accounts.x.ai")
    with pytest.raises(RuntimeError, match=r"accounts\.x\.ai"):
        ll.litellm_login_xai(incus, _gcfg(**_GROK_ONLY).litellm, "default")
    incus.config_device_add.assert_not_called()


def test_xai_login_explains_a_taken_host_port():
    incus = _xai_login_incus()
    incus.config_device_add.side_effect = IncusError("address already in use")
    with pytest.raises(RuntimeError, match=r"127\.0\.0\.1:56121.*address already in use"):
        ll.litellm_login_xai(incus, _gcfg(**_GROK_ONLY).litellm, "default")
    incus.exec_interactive.assert_not_called()


def test_xai_login_script_refuses_a_foreign_token_dir(tmp_path: Path):
    incus = _xai_login_incus()
    ll.litellm_login_xai(incus, _gcfg(**_GROK_ONLY).litellm, "default")
    command = incus.exec_interactive.call_args.args[1][-1]
    env_file = tmp_path / "instance.env"
    env_file.write_text("XAI_OAUTH_TOKEN_DIR=/tmp/elsewhere\n")
    marker = tmp_path / "authenticator-invoked"
    script = command.replace("/var/lib/jailbee-litellm/default/instance.env", str(env_file))
    script = script.split("exec ", 1)[0] + f"exec touch {marker}"
    result = subprocess.run(["bash", "-c", script], check=False, capture_output=True)
    assert result.returncode != 0 and not marker.exists()
