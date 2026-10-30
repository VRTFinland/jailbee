"""Tests for `jailbee.create_flow` — the create pipeline behind `jb new`.

Covers the pieces that need no terminal and no container: the error types,
`Interaction`, the host extras (registry mirror and LiteLLM) and the host
preparation (implicit apply, egress registration, cache-pool preflight).
"""

from __future__ import annotations

import pytest

from jailbee.branch_config import EscalationVerdict
from jailbee.config import Config
from jailbee.create_flow import (
    CreateError,
    CreateStarted,
    HostExtras,
    Interaction,
    NeedsOperator,
    ensure_profiles,
    host_extras,
    launch_background,
    preflight_background,
    prepare_fork_source,
    prepare_host,
    register_and_preflight,
    start_create,
)
from jailbee.egress_pool import RefreshResult
from jailbee.forking import ForkError, ForkSource
from jailbee.git import GitError
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.lifecycle import BranchAutostartAssessment, NewContainerOptions
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
    warn = mocker.MagicMock()

    prepare_host(cfg, incus, gcfg, interaction=Interaction.none(), on_warning=warn)

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


def test_register_and_preflight_partial_warning_defaults_to_the_log(mocker, tmp_path, caplog):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    _host_prep(mocker, status="partial", error="some.host")
    printed = mocker.patch("jailbee.tui.warn")

    with caplog.at_level("WARNING", logger="jailbee.create_flow"):
        register_and_preflight(cfg, incus, gcfg, interaction=Interaction.none())

    printed.assert_not_called()
    assert any("some.host" in r.getMessage() for r in caplog.records)


def test_register_and_preflight_partial_warning_goes_before_the_pool_preflight(mocker, tmp_path):
    # A pool refusal must not swallow the warning: it is surfaced as it
    # happens, not returned after the step that may raise.
    cfg, incus, gcfg = _env(tmp_path, mocker)
    _host_prep(mocker, status="partial", error="some.host", unresolved=["cache-a"])
    seen: list[str] = []

    with pytest.raises(CreateError):
        register_and_preflight(
            cfg, incus, gcfg, interaction=Interaction.none(), on_warning=seen.append
        )

    assert len(seen) == 1 and "some.host" in seen[0]


# ---- fork source


SRC = ForkSource("p-src", "feat/a", "abc123", "main")


def test_prepare_fork_source_pins_under_the_new_name(mocker, tmp_path):
    cfg, incus, _gcfg = _env(tmp_path, mocker)
    incus.exists.return_value = False
    prep = mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    pin = mocker.patch("jailbee.forking.pin_fork_commit")

    assert prepare_fork_source(cfg, incus, "src", "p-b") is SRC

    assert prep.call_args.args[1:] == (incus, "src")
    pin.assert_called_once_with(cfg, "p-b", "abc123")


def test_prepare_fork_source_existing_name_does_not_pin(mocker, tmp_path):
    cfg, incus, _gcfg = _env(tmp_path, mocker)
    incus.exists.return_value = True
    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    pin = mocker.patch("jailbee.forking.pin_fork_commit")

    with pytest.raises(CreateError) as e:
        prepare_fork_source(cfg, incus, "src", "p-b")

    assert e.value.exit_code == 2
    assert "Container 'b' already exists" in str(e.value)
    pin.assert_not_called()


def test_prepare_fork_source_fork_error_is_exit_2_without_the_branch_hint(mocker, tmp_path):
    cfg, incus, _gcfg = _env(tmp_path, mocker)
    mocker.patch(
        "jailbee.forking.prepare_fork",
        side_effect=ForkError("HEAD is detached. Use --branch <name> to specify."),
    )

    with pytest.raises(CreateError) as e:
        prepare_fork_source(cfg, incus, "src", "p-b")

    assert (str(e.value), e.value.exit_code) == ("HEAD is detached.", 2)


def test_prepare_fork_source_git_errors_are_exit_1(mocker, tmp_path):
    cfg, incus, _gcfg = _env(tmp_path, mocker)
    incus.exists.return_value = False
    mocker.patch("jailbee.forking.prepare_fork", side_effect=GitError("fetch failed: boom"))
    with pytest.raises(CreateError) as e:
        prepare_fork_source(cfg, incus, "src", "p-b")
    assert (str(e.value), e.value.exit_code) == ("fetch failed: boom", 1)

    mocker.patch("jailbee.forking.prepare_fork", return_value=SRC)
    mocker.patch("jailbee.forking.pin_fork_commit", side_effect=GitError("could not pin"))
    with pytest.raises(CreateError) as e:
        prepare_fork_source(cfg, incus, "src", "p-b")
    assert (str(e.value), e.value.exit_code) == ("could not pin", 1)


# ---- background preflight


def _opts(**kw):
    base = dict(
        container_branch="feat",
        name=None,
        network="strict",
        memory="4GiB",
        cpu=2,
        from_base="golden",
        clone=True,
    )
    base.update(kw)
    return NewContainerOptions(**base)


def _assessment_with_prompting_verdict(mocker, cfg):
    verdict = EscalationVerdict(
        widening_steps=("seed",),
        attached_mounts=(),
        baseline_source="origin/main",
        untrusted=True,
    )
    assert verdict.prompts
    rc = mocker.patch("jailbee.lifecycle.resolve_clone_ref", return_value=mocker.MagicMock())
    assessment = BranchAutostartAssessment(ref="abc", effective_cfg=cfg, verdict=verdict)
    mocker.patch("jailbee.lifecycle.assess_branch_autostart", return_value=assessment)
    mocker.patch("jailbee.remote_ssh.session.is_remote_session", return_value=False)
    return rc, assessment


def test_preflight_none_interaction_raises_needs_operator_on_widening(mocker, tmp_path):
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    _assessment_with_prompting_verdict(mocker, cfg)

    with pytest.raises(NeedsOperator) as e:
        preflight_background(cfg, _opts(), interaction=Interaction.none())

    assert e.value.exit_code == 2
    assert "no terminal to ask on" in str(e.value)
    assert "origin/main" in str(e.value)


def test_preflight_accept_escalation_policy_accepts(mocker, tmp_path):
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    _rc, assessment = _assessment_with_prompting_verdict(mocker, cfg)
    asked: list[object] = []

    def policy(v):
        asked.append(v)
        return True

    out = preflight_background(
        cfg, _opts(), interaction=Interaction(confirm=None, accept_escalation=policy)
    )

    assert out.approved_autostart_ref == assessment.ref == "abc"
    assert out.autofetch_done is True
    assert asked == [assessment.verdict]


def test_preflight_accept_escalation_policy_rejects_with_reasons(mocker, tmp_path):
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    _assessment_with_prompting_verdict(mocker, cfg)
    confirm = mocker.MagicMock(return_value=True)

    with pytest.raises(CreateError) as e:
        preflight_background(
            cfg,
            _opts(),
            interaction=Interaction(confirm=confirm, accept_escalation=lambda v: False),
        )

    assert e.value.exit_code == 2 and "loose" in str(e.value)
    assert not isinstance(e.value, NeedsOperator)
    confirm.assert_not_called()


def test_preflight_confirm_decline_is_exit_2(mocker, tmp_path):
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    _assessment_with_prompting_verdict(mocker, cfg)

    with pytest.raises(CreateError) as e:
        preflight_background(cfg, _opts(), interaction=Interaction(confirm=lambda _m: False))

    assert e.value.exit_code == 2
    assert "declined the target branch's autostart config" in str(e.value)


def test_preflight_assume_yes_skips_every_question(mocker, tmp_path):
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    _assessment_with_prompting_verdict(mocker, cfg)

    out = preflight_background(cfg, _opts(assume_yes=True), interaction=Interaction.none())

    assert out.approved_autostart_ref is None and out.autofetch_done is True


def test_preflight_remote_session_refuses_even_with_a_policy(mocker, tmp_path):
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    _assessment_with_prompting_verdict(mocker, cfg)
    mocker.patch("jailbee.remote_ssh.session.is_remote_session", return_value=True)

    with pytest.raises(CreateError) as e:
        preflight_background(
            cfg, _opts(), interaction=Interaction(confirm=None, accept_escalation=lambda v: True)
        )

    assert e.value.exit_code == 2


def test_preflight_bad_ref_is_exit_2(mocker, tmp_path):
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    mocker.patch("jailbee.lifecycle.resolve_clone_ref", side_effect=ValueError("no such ref"))

    with pytest.raises(CreateError) as e:
        preflight_background(cfg, _opts(), interaction=Interaction.none())

    assert (str(e.value), e.value.exit_code) == ("no such ref", 2)


@pytest.mark.parametrize(("override", "expected"), [(False, False), (True, True), (None, "cfg")])
def test_preflight_autofetch_override(mocker, tmp_path, override, expected):
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    rc = mocker.patch("jailbee.lifecycle.resolve_clone_ref", return_value=mocker.MagicMock())
    mocker.patch(
        "jailbee.lifecycle.assess_branch_autostart",
        return_value=BranchAutostartAssessment(ref=None, effective_cfg=cfg, verdict=None),
    )

    preflight_background(cfg, _opts(), interaction=Interaction(confirm=None, autofetch=override))

    want = cfg.new.autofetch if expected == "cfg" else expected
    assert rc.call_args.kwargs["autofetch"] is want


# ---- launch


def test_launch_background_spawns_worker_and_tracks(mocker, tmp_path, monkeypatch):
    import json

    from jailbee import background

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    popen = mocker.patch("jailbee.create_flow.subprocess.Popen")
    popen.return_value.pid = 4242
    engine = object()
    mocker.patch("jailbee.background.job_engine", return_value=engine)
    track = mocker.patch("jailbee.background.track_job")
    opts = _opts(name="p-x")

    started = launch_background(cfg, opts, container_name="p-x", config_path=None)

    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_new-worker"] and "--config" not in argv
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["cwd"] == str(cfg.repo_root)
    assert started == CreateStarted(container_name="p-x", pid=4242, log_path=started.log_path)
    assert started.log_path.name.startswith("p-x-") and started.log_path.is_file()
    job_file = argv[argv.index("--job") + 1]
    decoded, name, log_path = background.job_to_opts(json.loads(open(job_file).read()))
    assert (decoded, name, log_path) == (opts, "p-x", str(started.log_path))
    assert track.call_args.args[0] is engine
    assert track.call_args.args[2] == "record the new job row for p-x"


def test_launch_background_passes_a_config_file(mocker, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    popen = mocker.patch("jailbee.create_flow.subprocess.Popen")
    popen.return_value.pid = 1
    mocker.patch("jailbee.background.track_job")

    launch_background(cfg, _opts(), container_name="p-x", config_path=tmp_path / "c.yaml")

    argv = popen.call_args.args[0]
    assert argv[-2:] == ["--config", str(tmp_path / "c.yaml")]


def test_launch_boot_background_forwards_prompt_and_tracks(mocker, tmp_path, monkeypatch):
    from jailbee.create_flow import launch_boot_background

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg, _incus, _gcfg = _env(tmp_path, mocker)
    popen = mocker.patch("jailbee.create_flow.subprocess.Popen")
    popen.return_value.pid = 4242
    mocker.patch("jailbee.background.track_job")
    started = launch_boot_background(
        cfg, container_name="p-x", initial_prompt="resume", config_path=None
    )
    argv = popen.call_args.args[0]
    assert argv[1:4] == ["-m", "jailbee", "_boot-worker"]
    assert argv[argv.index("--initial-prompt") + 1] == "resume"
    assert "--config" not in argv
    assert popen.call_args.kwargs["start_new_session"] is True
    assert started.pid == 4242
    assert started.log_path.is_file()


# ---- start_create


def _start_env(mocker, tmp_path):
    cfg, incus, gcfg = _env(tmp_path, mocker)
    incus.exists.return_value = False
    prep = mocker.patch("jailbee.create_flow.prepare_host")
    mocker.patch(
        "jailbee.create_flow.host_extras",
        return_value=HostExtras(("10.0.0.1", 5000), None, {"k": "v"}, ("mirror is odd",)),
    )
    pre = mocker.patch(
        "jailbee.create_flow.preflight_background", side_effect=lambda _c, o, **_k: o
    )
    started = CreateStarted(container_name="p-feat", pid=7, log_path=tmp_path / "l")
    launch = mocker.patch("jailbee.create_flow.launch_background", return_value=started)
    return cfg, incus, gcfg, prep, pre, launch, started


def test_start_create_existing_container_raises_before_launch(mocker, tmp_path):
    cfg, incus, gcfg, _prep, pre, launch, _started = _start_env(mocker, tmp_path)
    incus.exists.return_value = True

    with pytest.raises(CreateError) as e:
        start_create(cfg, incus, gcfg, _opts(), interaction=Interaction.none(), config_path=None)

    assert e.value.exit_code == 2
    assert "already exists" in str(e.value)
    incus.exists.assert_called_once_with("p-feat")
    pre.assert_not_called()
    launch.assert_not_called()


def test_start_create_fills_host_extras_into_job(mocker, tmp_path, caplog):
    cfg, incus, gcfg, prep, pre, launch, started = _start_env(mocker, tmp_path)
    interaction = Interaction.none()

    with caplog.at_level("WARNING", logger="jailbee.create_flow"):
        out = start_create(
            cfg, incus, gcfg, _opts(), interaction=interaction, config_path=tmp_path / "c"
        )

    assert out is started
    assert prep.call_args.kwargs["interaction"] is interaction
    assert pre.call_args.kwargs["interaction"] is interaction
    launched = launch.call_args.args[1]
    assert launched.mirror_endpoint == ("10.0.0.1", 5000)
    assert launched.litellm_payload == {"k": "v"}
    assert launch.call_args.kwargs == {"container_name": "p-feat", "config_path": tmp_path / "c"}
    assert any("mirror is odd" in r.getMessage() for r in caplog.records)


def test_start_create_refuses_prompt_for_agent_without_prompt_args(mocker, tmp_path):
    _cfg, incus, gcfg, prep, _pre, launch, _started = _start_env(mocker, tmp_path)
    cfg = make_cfg(tmp_path, agents={"aider": {"enabled": True, "autostart": True}})

    with pytest.raises(CreateError) as e:
        start_create(
            cfg,
            incus,
            gcfg,
            _opts(initial_prompt="do it"),
            interaction=Interaction.none(),
            config_path=None,
        )

    assert e.value.exit_code == 2
    assert "aider" in str(e.value) and "prompt_args" in str(e.value)
    prep.assert_not_called()
    launch.assert_not_called()


def test_start_create_refuses_blank_prompt(mocker, tmp_path):
    _cfg, incus, gcfg, prep, _pre, launch, _started = _start_env(mocker, tmp_path)
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True, "autostart": True}})

    with pytest.raises(CreateError) as e:
        start_create(
            cfg,
            incus,
            gcfg,
            _opts(initial_prompt=" \n"),
            interaction=Interaction.none(),
            config_path=None,
        )

    assert e.value.exit_code == 2 and "empty" in str(e.value)
    prep.assert_not_called()
    launch.assert_not_called()


def test_start_create_accepts_a_valid_prompt(mocker, tmp_path):
    _cfg, incus, gcfg, _prep, _pre, launch, started = _start_env(mocker, tmp_path)
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True, "autostart": True}})
    object.__setattr__(cfg, "container_prefix", "p")

    out = start_create(
        cfg,
        incus,
        gcfg,
        _opts(initial_prompt="do it"),
        interaction=Interaction.none(),
        config_path=None,
    )

    assert out is started
    assert launch.call_args.args[1].initial_prompt == "do it"


def test_start_create_passes_the_runner_to_prompt_problem(mocker, tmp_path):
    from jailbee.orchestration.runner import Runner

    _cfg, incus, gcfg, _prep, _pre, _launch, _started = _start_env(mocker, tmp_path)
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True, "autostart": True}})
    object.__setattr__(cfg, "container_prefix", "p")
    runner = Runner(agent="claude")
    check = mocker.patch("jailbee.initial_prompt.prompt_problem", return_value="no way")

    with pytest.raises(CreateError) as e:
        start_create(
            cfg,
            incus,
            gcfg,
            _opts(initial_prompt="do it", runner=runner),
            interaction=Interaction.none(),
            config_path=None,
        )

    check.assert_called_once_with(cfg, runner)
    assert e.value.exit_code == 2 and "no way" in str(e.value)
