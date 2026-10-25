"""Tests for `jailbee.create_flow` — the create pipeline behind `jb new`.

Covers the pieces that need no terminal and no container: the error types,
`Interaction`, the host extras (registry mirror and LiteLLM) and the host
preparation (implicit apply, egress registration, cache-pool preflight).
"""

from __future__ import annotations

import pytest

from jailbee.config import Config
from jailbee.create_flow import (
    CreateError,
    HostExtras,
    Interaction,
    NeedsOperator,
    ensure_profiles,
    host_extras,
    prepare_host,
    register_and_preflight,
)
from jailbee.egress_pool import RefreshResult
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from tests.conftest import make_cfg, memory_engine


def _env(tmp_path, mocker):
    cfg = make_cfg(tmp_path, shared_dir=tmp_path / "shared")
    object.__setattr__(cfg, "container_prefix", "p")
    incus = mocker.MagicMock()
    gcfg = GlobalConfig()
    return cfg, incus, gcfg


def _host_prep(mocker, *, status: str = "ok", error: str | None = None, unresolved=()):
    """Neutralise the state DB, egress registration and pool preflight."""
    mocker.patch("jailbee.db.get_engine", return_value=memory_engine())
    register = mocker.patch("jailbee.egress_pool.register_repo")
    refresh = mocker.patch(
        "jailbee.egress_pool.refresh_pool",
        return_value=RefreshResult(container_prefix="p", status=status, error=error),
    )
    pre = mocker.patch("jailbee.pool.preflight_pools", return_value=list(unresolved))
    return register, refresh, pre


# ---- errors


def test_create_error_carries_message_and_exit_code():
    e = CreateError("bad thing", 1)
    assert str(e) == "bad thing"
    assert (e.message, e.exit_code) == ("bad thing", 1)


def test_needs_operator_is_a_create_error_with_exit_2():
    e = NeedsOperator("needs a human", reason="confirm")
    assert isinstance(e, CreateError)
    assert e.exit_code == 2
    assert e.reason == "confirm"
    assert str(e) == "needs a human"


# ---- Interaction


def test_none_interaction_cannot_confirm_and_never_autofetches():
    assert Interaction.none().confirm is None
    assert Interaction.none().autofetch is False
    assert Interaction.none().accept_escalation is None


def test_terminal_interaction_confirms_only_on_a_terminal(mocker):
    from jailbee import tui

    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    assert Interaction.terminal().confirm is tui.default_confirm
    assert Interaction.terminal().autofetch is None

    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    assert Interaction.terminal().confirm is None


# ---- host_extras


def test_host_extras_without_mirror_or_litellm(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    mocker.patch("jailbee.create_flow.mirror_wanted", return_value=False)
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=None)

    e = host_extras(cfg, incus, gcfg)

    assert isinstance(e, HostExtras)
    assert (e.mirror_endpoint, e.mirror_ca_path, e.litellm_payload) == (None, None, None)
    assert e.warnings == ()


def test_host_extras_mirror_with_ca_keeps_endpoint_and_ca(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    object.__setattr__(gcfg.docker_registry_mirror, "data_dir", tmp_path / "registry")
    ca = tmp_path / "registry" / "ca" / "ca.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("cert")
    mocker.patch("jailbee.create_flow.mirror_wanted", return_value=True)
    mocker.patch("jailbee.docker_daemon.compute_mirror_endpoint", return_value=("10.0.0.5", 3128))
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=None)

    e = host_extras(cfg, incus, gcfg, network="strict")

    assert e.mirror_endpoint == ("10.0.0.5", 3128)
    assert e.mirror_ca_path == ca
    assert e.warnings == ()


def test_host_extras_mirror_without_ca_is_dropped_with_warning(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    # No `ca/ca.crt` under it, which is the whole point.
    object.__setattr__(gcfg.docker_registry_mirror, "data_dir", tmp_path / "registry")
    mocker.patch("jailbee.create_flow.mirror_wanted", return_value=True)
    mocker.patch("jailbee.docker_daemon.compute_mirror_endpoint", return_value=("10.0.0.5", 3128))
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=None)

    e = host_extras(cfg, incus, gcfg, network="loose")

    assert e.mirror_endpoint is None
    assert e.mirror_ca_path is None
    assert len(e.warnings) == 1
    assert "Mirror CA cert not found" in e.warnings[0]
    assert "Continuing without it" in e.warnings[0]


def test_host_extras_mirror_problem_in_strict_mode_raises_exit_1(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    mocker.patch("jailbee.create_flow.mirror_wanted", return_value=True)
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint", side_effect=ValueError("mirror down")
    )
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=None)

    with pytest.raises(CreateError) as exc:
        host_extras(cfg, incus, gcfg, network="strict")

    assert exc.value.exit_code == 1
    assert str(exc.value) == "mirror down"


def test_host_extras_network_defaults_to_the_config_default(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    cfg = cfg.model_copy(update={"defaults": cfg.defaults.model_copy(update={"network": "loose"})})
    mocker.patch("jailbee.create_flow.mirror_wanted", return_value=True)
    mocker.patch(
        "jailbee.docker_daemon.compute_mirror_endpoint", side_effect=ValueError("mirror down")
    )
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=None)

    e = host_extras(cfg, incus, gcfg)

    assert e.mirror_endpoint is None
    assert len(e.warnings) == 1 and e.warnings[0].startswith("mirror down")


def test_host_extras_litellm_failure_becomes_a_warning(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    mocker.patch("jailbee.create_flow.mirror_wanted", return_value=False)
    mocker.patch("jailbee.litellm.container_sync_payload", side_effect=IncusError("offline"))

    e = host_extras(cfg, incus, gcfg)

    assert e.litellm_payload is None
    assert len(e.warnings) == 1
    assert "Could not resolve LiteLLM settings" in e.warnings[0]


def test_host_extras_unserved_litellm_warning_is_collected(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    payload: dict[str, object] = {"k": "v"}
    mocker.patch("jailbee.create_flow.mirror_wanted", return_value=False)
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=payload)
    mocker.patch("jailbee.litellm.unserved_warning", return_value="not served")

    e = host_extras(cfg, incus, gcfg)

    assert e.litellm_payload is payload
    assert e.warnings == ("not served",)


# ---- prepare_host / ensure_profiles / register_and_preflight


def test_prepare_host_dns_error_raises_exit_1(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    _host_prep(mocker, status="dns_error", error="boom")

    with pytest.raises(CreateError) as e:
        prepare_host(cfg, incus, gcfg, interaction=Interaction.none())

    assert e.value.exit_code == 1 and "boom" in str(e.value)


def test_prepare_host_partial_resolution_only_warns(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    _host_prep(mocker, status="partial", error="some.host")
    warn = mocker.patch("jailbee.create_flow.warn")

    prepare_host(cfg, incus, gcfg, interaction=Interaction.none())

    assert warn.call_count == 1
    assert "some.host" in warn.call_args.args[0]


def test_prepare_host_unresolved_pool_raises_exit_2(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    _host_prep(mocker, unresolved=["cache-a"])

    with pytest.raises(CreateError) as e:
        prepare_host(cfg, incus, gcfg, interaction=Interaction.none())

    assert e.value.exit_code == 2
    assert "cache-a" in str(e.value)
    assert "Nothing was created" in str(e.value)


def test_prepare_host_passes_confirm_to_pool_preflight(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    _register, _refresh, pre = _host_prep(mocker)

    def confirm(_m: str) -> bool:
        return True

    prepare_host(cfg, incus, gcfg, interaction=Interaction(confirm=confirm))

    assert pre.call_args.kwargs["confirm"] is confirm


def test_prepare_host_registers_before_preflight(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    register, refresh, pre = _host_prep(mocker)
    calls = mocker.MagicMock()
    calls.attach_mock(register, "register")
    calls.attach_mock(refresh, "refresh")
    calls.attach_mock(pre, "preflight")

    prepare_host(cfg, incus, gcfg, interaction=Interaction.none())

    assert [name for name, _, _ in calls.mock_calls] == ["register", "refresh", "preflight"]


def test_prepare_host_runs_apply_only_for_synthetic_without_profiles(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    _host_prep(mocker)
    run_apply = mocker.patch("jailbee.apply.run_apply")
    recorded: list[Config] = []

    # A configured repo: never applies, whatever the profiles look like.
    incus.profile_exists.return_value = False
    prepare_host(cfg, incus, gcfg, interaction=Interaction.none(), record_apply=recorded.append)
    assert run_apply.call_count == 0
    assert recorded == []

    mocker.patch.object(Config, "is_synthetic", return_value=True)

    # Synthetic with every profile present: nothing to create.
    incus.profile_exists.return_value = True
    prepare_host(cfg, incus, gcfg, interaction=Interaction.none(), record_apply=recorded.append)
    assert run_apply.call_count == 0
    assert recorded == []

    # Synthetic with one profile missing: applies once, unattended, and records it.
    incus.profile_exists.side_effect = lambda name: not name.endswith("-net-loose")
    prepare_host(cfg, incus, gcfg, interaction=Interaction.none(), record_apply=recorded.append)
    assert run_apply.call_count == 1
    assert run_apply.call_args.kwargs == {"assume_yes": True, "no_restart": True}
    assert recorded == [cfg]


def test_ensure_profiles_default_recorder_writes_the_apply_watermark(mocker, tmp_path):
    from sqlmodel import Session

    from jailbee import __version__
    from jailbee.db.models import RepoUpgradeState

    cfg, incus, gcfg = _env(tmp_path, mocker)
    engine = memory_engine()
    mocker.patch("jailbee.db.get_engine", return_value=engine)
    mocker.patch("jailbee.apply.run_apply")
    mocker.patch.object(Config, "is_synthetic", return_value=True)
    incus.profile_exists.return_value = False

    assert ensure_profiles(cfg, incus, gcfg) is True

    with Session(engine) as session:
        row = session.get(RepoUpgradeState, "p")
    assert row is not None
    assert (row.apply_version, row.apply_observed) == (__version__, True)


def test_ensure_profiles_reports_false_when_nothing_ran(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    run_apply = mocker.patch("jailbee.apply.run_apply")

    assert ensure_profiles(cfg, incus, gcfg, record_apply=lambda _c: None) is False
    run_apply.assert_not_called()


def test_register_and_preflight_does_not_apply(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    register, _refresh, _pre = _host_prep(mocker)
    run_apply = mocker.patch("jailbee.apply.run_apply")
    mocker.patch.object(Config, "is_synthetic", return_value=True)
    incus.profile_exists.return_value = False

    register_and_preflight(cfg, incus, gcfg, interaction=Interaction.none())

    run_apply.assert_not_called()
    register.assert_called_once()
