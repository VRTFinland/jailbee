"""Tests for autostart step iteration."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.autostart import (
    AutostartStepError,
    AutostartTrigger,
    has_graphical_session,
    run_autostart,
)
from jailbee.config import Autostart, AutostartStep, Config
from jailbee.tmux import TmuxStepError


def _cfg(steps_on_create=None, steps_on_start=None, **autostart_kwargs):
    """Construct a Config with autostart steps for testing."""
    auto = Autostart(
        on_create=steps_on_create or [],
        on_start=steps_on_start or [],
        **autostart_kwargs,
    )
    cfg = Config(autostart=auto)
    object.__setattr__(cfg, "repo_root", Path("/tmp"))
    object.__setattr__(cfg, "default_branch", "main")
    object.__setattr__(cfg, "container_prefix", "test")
    object.__setattr__(cfg, "shared_dir", Path("/tmp/shared"))
    return cfg


def _patch_tmux(mocker):
    """Patch tmux.ensure_session + tmux.run_step. Returns (ensure, run_step)."""
    ensure = mocker.patch("jailbee.autostart.tmux.ensure_session")
    run_step = mocker.patch("jailbee.autostart.tmux.run_step")
    return ensure, run_step


def test_no_steps_no_exec(mocker):
    ensure, run_step = _patch_tmux(mocker)
    cfg = _cfg()
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/home/dev/repo")
    ensure.assert_not_called()
    run_step.assert_not_called()


def test_run_autostart_calls_ensure_session_when_steps_exist(mocker):
    """ensure_session is called once before steps run."""
    ensure, _ = _patch_tmux(mocker)
    step = AutostartStep(name="s", run="echo hi")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    ensure.assert_called_once_with(incus, "c1", start_dir="/r")


def test_single_step_runs_with_repo_dir_env(mocker):
    _, run_step = _patch_tmux(mocker)
    step = AutostartStep(name="echo", run="echo hello")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/home/dev/repo")

    run_step.assert_called_once()
    kwargs = run_step.call_args.kwargs
    assert kwargs["command"] == "echo hello"
    assert kwargs["cwd"] == "/home/dev/repo"
    assert kwargs["env"]["REPO_DIR"] == "/home/dev/repo"
    assert kwargs["background"] is False


def test_step_env_overrides_global_env(mocker):
    _, run_step = _patch_tmux(mocker)
    step = AutostartStep(name="x", run="env", env={"FOO": "step", "BAR": "step"})
    cfg = _cfg(steps_on_create=[step], env={"FOO": "global", "GLOBAL_ONLY": "yes"})
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    env = run_step.call_args.kwargs["env"]
    assert env["FOO"] == "step"
    assert env["BAR"] == "step"
    assert env["GLOBAL_ONLY"] == "yes"


def test_step_working_dir_appended_to_repo_dir(mocker):
    _, run_step = _patch_tmux(mocker)
    step = AutostartStep(name="x", run="pwd", working_dir="frontend")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/home/dev/repo")
    assert run_step.call_args.kwargs["cwd"] == "/home/dev/repo/frontend"


def test_step_timeout_passed_through(mocker):
    _, run_step = _patch_tmux(mocker)
    step = AutostartStep(name="x", run="sleep 9999", timeout=5)
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    assert run_step.call_args.kwargs["timeout"] == 5


def test_step_default_timeout_is_step_timeout(mocker):
    _, run_step = _patch_tmux(mocker)
    step = AutostartStep(name="x", run="echo")
    cfg = _cfg(steps_on_create=[step], step_timeout=600)
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    assert run_step.call_args.kwargs["timeout"] == 600


def test_failing_step_aborts_remaining(mocker):
    _, run_step = _patch_tmux(mocker)
    run_step.side_effect = RuntimeError("step failed")
    step1 = AutostartStep(name="bad", run="false")
    step2 = AutostartStep(name="never", run="echo never")
    cfg = _cfg(steps_on_create=[step1, step2])
    incus = MagicMock()

    with pytest.raises(RuntimeError):
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    # Only first step attempted
    assert run_step.call_count == 1


def test_continue_on_error_lets_next_step_run(mocker):
    _, run_step = _patch_tmux(mocker)
    calls = {"n": 0}

    def _run_step(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first failed")
        return None

    run_step.side_effect = _run_step

    step1 = AutostartStep(name="bad", run="false", continue_on_error=True)
    step2 = AutostartStep(name="ok", run="echo ok")
    cfg = _cfg(steps_on_create=[step1, step2])
    incus = MagicMock()

    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    assert run_step.call_count == 2


def test_background_step_runs_in_tmux_window(mocker):
    """Background step delegates to tmux.run_step with background=True."""
    _, run_step = _patch_tmux(mocker)
    step = AutostartStep(name="bg", run="pnpm dev", background=True)
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    run_step.assert_called_once()
    kwargs = run_step.call_args.kwargs
    assert kwargs["name"] == "bg"
    assert kwargs["command"] == "pnpm dev"
    assert kwargs["background"] is True
    assert kwargs["cwd"] == "/r"


def test_step_with_mounts_added_then_removed(tmp_path, mocker):
    from jailbee.config import OptionalMount

    _, run_step = _patch_tmux(mocker)
    (tmp_path / "aws").mkdir()
    step = AutostartStep(name="pull", run="docker pull x", mounts=["aws"])
    cfg = _cfg(steps_on_create=[step])
    cfg = cfg.model_copy(
        update={
            "optional_mounts": {
                "aws": OptionalMount(
                    host=tmp_path / "aws",
                    container="/home/user/.aws",
                    readonly=True,
                ),
            },
        }
    )
    incus = MagicMock()

    # Track ordering of mount add/remove relative to run_step
    call_order: list[str] = []
    incus.config_device_add.side_effect = lambda *a, **kw: call_order.append("add")
    incus.config_device_remove.side_effect = lambda *a, **kw: call_order.append("remove")
    run_step.side_effect = lambda *a, **kw: call_order.append("step")

    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    assert call_order == ["add", "step", "remove"]


def test_mount_removed_even_if_step_fails(tmp_path, mocker):
    from jailbee.config import OptionalMount

    _, run_step = _patch_tmux(mocker)
    run_step.side_effect = RuntimeError("step failed")
    (tmp_path / "aws").mkdir()
    step = AutostartStep(name="pull", run="false", mounts=["aws"])
    cfg = _cfg(steps_on_create=[step])
    cfg = cfg.model_copy(
        update={
            "optional_mounts": {
                "aws": OptionalMount(
                    host=tmp_path / "aws",
                    container="/home/user/.aws",
                    readonly=True,
                ),
            },
        }
    )
    incus = MagicMock()

    with pytest.raises(RuntimeError):
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    methods = [c[0] for c in incus.method_calls]
    assert "config_device_remove" in methods


def test_step_with_network_swaps_and_restores(mocker):
    _patch_tmux(mocker)
    step = AutostartStep(name="pull", run="docker pull foo", network="loose")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    sw = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )

    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    # Two swap calls: pre-step (strict→loose) and post-step (loose→strict)
    assert sw.call_count == 2
    first_target = sw.call_args_list[0].args[3]
    second_target = sw.call_args_list[1].args[3]
    assert first_target == "loose"
    assert second_target == "strict"


def test_step_network_swap_forwards_mirror_endpoint(mocker):
    """The strict-mode `jailbee-registry-mirror.incus` pin must survive a
    `strict → loose → strict` round-trip — `run_autostart` forwards the
    caller-supplied `mirror_endpoint` to both `switch_network` calls so
    the restore step re-pins the mirror IP in /etc/hosts (the autostart
    edge of that fix)."""
    _patch_tmux(mocker)
    step = AutostartStep(name="pull", run="docker pull foo", network="loose")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    sw = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )

    run_autostart(
        cfg,
        incus,
        "c1",
        AutostartTrigger.ON_CREATE,
        repo_dir="/r",
        mirror_endpoint=("10.42.0.7", 3128),
    )

    assert sw.call_count == 2
    assert sw.call_args_list[0].kwargs.get("mirror_endpoint") == ("10.42.0.7", 3128)
    assert sw.call_args_list[1].kwargs.get("mirror_endpoint") == ("10.42.0.7", 3128)


def test_step_no_network_swap_when_already_in_target(mocker):
    _patch_tmux(mocker)
    step = AutostartStep(name="pull", run="docker pull foo", network="loose")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    sw = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="loose",
    )

    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    assert sw.call_count == 0


def test_step_network_restored_even_if_step_fails(mocker):
    _, run_step = _patch_tmux(mocker)
    run_step.side_effect = RuntimeError("step failed")
    step = AutostartStep(name="pull", run="docker pull foo", network="loose")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    sw = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )

    with pytest.raises(RuntimeError):
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    # Restore must still happen
    assert sw.call_count == 2


def test_step_log_includes_network_profile(capsys, mocker):
    """Step start log must include the network profile."""
    _patch_tmux(mocker)
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )
    step = AutostartStep(name="checkout", run="git pull")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    out = capsys.readouterr().out
    assert "checkout" in out
    assert "strict" in out


def test_step_log_uses_step_override_network(capsys, mocker):
    """When step.network is set, log it (not the current network)."""
    _patch_tmux(mocker)
    mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )
    step = AutostartStep(name="pull", run="docker pull foo", network="loose")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    out = capsys.readouterr().out
    assert "net: loose" in out


def test_step_log_includes_elapsed_time(capsys, mocker):
    """After each step, log the elapsed duration."""
    _patch_tmux(mocker)
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="loose",
    )
    step = AutostartStep(name="build", run="make")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    out = capsys.readouterr().out
    # Format: "    ↳ build: 0.0s" — we just assert name + 's' on the same line.
    assert "build:" in out
    assert "s" in out  # seconds suffix


def test_step_elapsed_logged_even_on_failure(capsys, mocker):
    """Elapsed log must fire even if the step raises (useful for slow failures)."""
    _, run_step = _patch_tmux(mocker)
    run_step.side_effect = RuntimeError("boom")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="loose",
    )
    step = AutostartStep(name="bad", run="false")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    with pytest.raises(RuntimeError):
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    out = capsys.readouterr().out
    assert "bad:" in out


def test_tmux_step_error_wrapped_as_autostart_step_error(mocker):
    """A failing step surfaces as AutostartStepError with structured fields
    (container, step name, exit code) — so the CLI can render a friendly
    one-liner without a Python traceback. Regression guard against the
    raw stack trace we used to print when a user's autostart step's
    command was missing from PATH."""
    _, run_step = _patch_tmux(mocker)
    run_step.side_effect = TmuxStepError(
        "step 'backend-warmup' exit code 127",
        step_name="backend-warmup",
        reason="exit",
        exit_code=127,
    )
    step = AutostartStep(name="backend-warmup", run="bin/warmup")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    with pytest.raises(AutostartStepError) as exc_info:
        run_autostart(cfg, incus, "gisgro-part4-fixes", AutostartTrigger.ON_CREATE, repo_dir="/r")

    err = exc_info.value
    assert err.container == "gisgro-part4-fixes"
    assert err.step_name == "backend-warmup"
    assert err.exit_code == 127
    assert err.reason == "exit"

    msg = str(err)
    # Human-readable cause: 127 is mapped to "command not found"
    assert "command not found" in msg
    # Names the failed step and container, and points at jailbee tmux for inspection
    assert "backend-warmup" in msg
    assert "gisgro-part4-fixes" in msg
    assert "jailbee tmux gisgro-part4-fixes" in msg


def test_autostart_error_hint_is_a_plain_attach():
    """The inspection hint is a bare `jailbee tmux <name>`.

    It renders for foreground failures too, where no background job row
    exists and `--force` would be cargo-culted noise; the attach guard now
    offers the failed container itself, so no flag is needed either way.
    """
    err = AutostartStepError(
        container="gisgro-feat-x",
        step_name="backend-warmup",
        reason="exit",
        exit_code=1,
    )
    rendered = str(err)
    assert "jailbee tmux gisgro-feat-x" in rendered
    assert "--force" not in rendered


def test_autostart_step_error_timeout_message(mocker):
    """Timeout errors render with 'timed out' (not 'exit code N')."""
    _, run_step = _patch_tmux(mocker)
    run_step.side_effect = TmuxStepError(
        "step 'slow' timed out after 60s",
        step_name="slow",
        reason="timeout",
    )
    step = AutostartStep(name="slow", run="sleep 999")
    cfg = _cfg(steps_on_create=[step])
    incus = MagicMock()

    with pytest.raises(AutostartStepError) as exc_info:
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")

    assert "timed out" in str(exc_info.value)


def test_has_graphical_session_true_with_wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert has_graphical_session() is True


def test_has_graphical_session_true_with_display(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    assert has_graphical_session() is True


def test_has_graphical_session_false(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert has_graphical_session() is False


# ---------- github-token autostart step


def test_github_token_step_returns_none_when_disabled(tmp_path):
    from jailbee.autostart import _github_token_step
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": False, "api_tokens": {"sampleapp": "github_pat_xxx"}},
    )
    assert _github_token_step(cfg) is None


def test_github_token_step_returns_none_when_prefix_absent_from_map(tmp_path):
    from jailbee.autostart import _github_token_step
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="not-in-map",
        github={"enabled": True, "api_tokens": {"sampleapp": "github_pat_xxx"}},
    )
    assert _github_token_step(cfg) is None


def test_github_token_step_returns_none_for_whitespace_only_token(tmp_path):
    from jailbee.autostart import _github_token_step
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": True, "api_tokens": {"sampleapp": "   "}},
    )
    assert _github_token_step(cfg) is None


def test_github_token_step_emits_step_with_quoted_token(tmp_path):
    from jailbee.autostart import _github_token_step
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={
            "enabled": True,
            "api_tokens": {"sampleapp": "github_pat_with's-quote"},
        },
    )
    step = _github_token_step(cfg)
    assert step is not None
    assert step.name == "github-token"
    assert step.network is None
    assert "/etc/profile.d/jailbee-github.sh" in step.run
    assert "export GH_TOKEN=" in step.run
    # Step runs as dev user; /etc/profile.d/ is root-owned, so the write
    # path goes through sudo (passwordless sudo is in the golden image).
    assert "sudo tee /etc/profile.d/jailbee-github.sh" in step.run
    assert "sudo chmod 0644" in step.run
    # shlex.quote of a string containing a single quote becomes
    # 'github_pat_with'"'"'s-quote'
    assert "github_pat_with'\"'\"'s-quote" in step.run
    # SecretStr.repr must not leak into the body.
    assert "SecretStr" not in step.run


def test_github_token_not_part_of_run_autostart(tmp_path):
    """The github-token step is injected by ``inject_github_token`` (infra),
    NOT by ``run_autostart`` — so --no-autostart can skip user steps without
    dropping GH_TOKEN."""
    from jailbee.autostart import AutostartTrigger, run_autostart
    from tests.conftest import make_cfg

    user_step = AutostartStep(name="user-step", run="echo hi")
    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": True, "api_tokens": {"sampleapp": "github_pat_xxx"}},
        autostart={"on_start": [user_step.model_dump()]},
    )

    incus = MagicMock()
    applied: list[str] = []

    def fake_apply(_cfg, _incus, _container, step, _repo_dir, **_kwargs):
        applied.append(step.name)

    import jailbee.autostart as autostart_mod

    original = autostart_mod._apply_step
    autostart_mod._apply_step = fake_apply
    try:
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")
    finally:
        autostart_mod._apply_step = original

    assert applied == ["user-step"]


def test_inject_github_token_applies_step_when_token_present(tmp_path):
    """inject_github_token ensures a tmux session and runs the github-token
    step directly, regardless of any autostart config."""
    from jailbee.autostart import inject_github_token
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": True, "api_tokens": {"sampleapp": "github_pat_xxx"}},
    )

    incus = MagicMock()
    applied: list[str] = []

    import jailbee.autostart as autostart_mod

    orig_apply = autostart_mod._apply_step
    orig_ensure = autostart_mod.tmux.ensure_session
    ensure_calls: list[str] = []
    autostart_mod._apply_step = lambda _cfg, _incus, _container, step, _repo_dir, **_kw: (
        applied.append(step.name)
    )
    autostart_mod.tmux.ensure_session = lambda _incus, container, start_dir=None: (
        ensure_calls.append(container)
    )
    try:
        inject_github_token(cfg, incus, "c1", "/r")
    finally:
        autostart_mod._apply_step = orig_apply
        autostart_mod.tmux.ensure_session = orig_ensure

    assert applied == ["github-token"]
    assert ensure_calls == ["c1"]


def test_inject_github_token_noop_when_disabled(tmp_path):
    """No token applies → no tmux session, no step."""
    from jailbee.autostart import inject_github_token
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, github={"enabled": False})

    incus = MagicMock()
    applied: list[str] = []

    import jailbee.autostart as autostart_mod

    orig_apply = autostart_mod._apply_step
    orig_ensure = autostart_mod.tmux.ensure_session
    ensure_calls: list[str] = []
    autostart_mod._apply_step = lambda _cfg, _incus, _container, step, _repo_dir, **_kw: (
        applied.append(step.name)
    )
    autostart_mod.tmux.ensure_session = lambda _incus, container, start_dir=None: (
        ensure_calls.append(container)
    )
    try:
        inject_github_token(cfg, incus, "c1", "/r")
    finally:
        autostart_mod._apply_step = orig_apply
        autostart_mod.tmux.ensure_session = orig_ensure

    assert applied == []
    assert ensure_calls == []


def test_on_start_unchanged_when_github_token_step_returns_none(tmp_path):
    from jailbee.autostart import AutostartTrigger, run_autostart
    from tests.conftest import make_cfg

    user_step = AutostartStep(name="user-step", run="echo hi")
    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": False},
        autostart={"on_start": [user_step.model_dump()]},
    )

    incus = MagicMock()
    applied: list[str] = []

    def fake_apply(_cfg, _incus, _container, step, _repo_dir, **_kwargs):
        applied.append(step.name)

    import jailbee.autostart as autostart_mod

    original = autostart_mod._apply_step
    autostart_mod._apply_step = fake_apply
    try:
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")
    finally:
        autostart_mod._apply_step = original

    assert applied == ["user-step"]


def test_on_create_does_not_get_github_token_step(tmp_path):
    """github-token is only injected into on_start (rotation friendliness),
    not on_create."""
    from jailbee.autostart import AutostartTrigger, run_autostart
    from tests.conftest import make_cfg

    user_step = AutostartStep(name="user-step", run="echo hi")
    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": True, "api_tokens": {"sampleapp": "github_pat_xxx"}},
        autostart={"on_create": [user_step.model_dump()]},
    )

    incus = MagicMock()
    applied: list[str] = []

    def fake_apply(_cfg, _incus, _container, step, _repo_dir, **_kwargs):
        applied.append(step.name)

    import jailbee.autostart as autostart_mod

    original = autostart_mod._apply_step
    autostart_mod._apply_step = fake_apply
    try:
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    finally:
        autostart_mod._apply_step = original

    assert applied == ["user-step"]


# ---------- claude autostart step


def test_claude_autostart_step_returns_none_when_off(tmp_path):
    from jailbee.autostart import _claude_autostart_step
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, claude={"enabled": True, "autostart": False})
    assert _claude_autostart_step(cfg) is None


def test_claude_autostart_step_shape(tmp_path):
    from jailbee.autostart import _claude_autostart_step
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, claude={"enabled": True, "autostart": True})
    step = _claude_autostart_step(cfg)
    assert step is not None
    assert step.name == "claude"
    assert step.background is True
    assert step.network is None
    assert step.run == "exec claude"


def test_claude_autostart_step_is_continue_on_error(tmp_path):
    """The claude step is an *optional* integration. If `claude` fails to
    launch (e.g. the binary never installed), `gie new` must degrade with a
    warning rather than hard-failing the whole provisioning — the dev
    container is still usable. So the synthetic step carries
    continue_on_error=True."""
    from jailbee.autostart import _claude_autostart_step
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, claude={"enabled": True, "autostart": True})
    step = _claude_autostart_step(cfg)
    assert step is not None
    assert step.continue_on_error is True


def test_claude_autostart_step_uses_custom_command(tmp_path):
    from jailbee.autostart import _claude_autostart_step
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        claude={
            "enabled": True,
            "autostart": True,
            "command": "claude --dangerously-skip-permissions",
        },
    )
    step = _claude_autostart_step(cfg)
    assert step is not None
    assert step.run == "exec claude --dangerously-skip-permissions"


def test_claude_step_appended_to_on_start(tmp_path):
    """Claude runs last so its tmux window is the most recently
    created — `jailbee tmux` lands in it by default."""
    from jailbee.autostart import AutostartTrigger, run_autostart
    from tests.conftest import make_cfg

    user_step = AutostartStep(name="user-step", run="echo hi")
    cfg = make_cfg(
        tmp_path,
        claude={"enabled": True, "autostart": True},
        autostart={"on_start": [user_step.model_dump()]},
    )

    incus = MagicMock()
    applied: list[str] = []

    def fake_apply(_cfg, _incus, _container, step, _repo_dir, **_kwargs):
        applied.append(step.name)

    import jailbee.autostart as autostart_mod

    original = autostart_mod._apply_step
    autostart_mod._apply_step = fake_apply
    try:
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")
    finally:
        autostart_mod._apply_step = original

    assert applied == ["user-step", "claude"]


def test_claude_step_appended_even_with_github_enabled(tmp_path):
    """github-token is no longer injected by run_autostart; claude still
    appends after the user step even when the github integration is on."""
    from jailbee.autostart import AutostartTrigger, run_autostart
    from tests.conftest import make_cfg

    user_step = AutostartStep(name="user-step", run="echo hi")
    cfg = make_cfg(
        tmp_path,
        container_prefix="sampleapp",
        github={"enabled": True, "api_tokens": {"sampleapp": "github_pat_xxx"}},
        claude={"enabled": True, "autostart": True},
        autostart={"on_start": [user_step.model_dump()]},
    )

    incus = MagicMock()
    applied: list[str] = []

    def fake_apply(_cfg, _incus, _container, step, _repo_dir, **_kwargs):
        applied.append(step.name)

    import jailbee.autostart as autostart_mod

    original = autostart_mod._apply_step
    autostart_mod._apply_step = fake_apply
    try:
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")
    finally:
        autostart_mod._apply_step = original

    assert applied == ["user-step", "claude"]


def test_on_create_does_not_get_claude_step(tmp_path):
    """claude.autostart triggers only on_start, not on_create — so the
    first `gie new` reaches it via the explicit ON_START call in
    lifecycle.create_container, not via ON_CREATE."""
    from jailbee.autostart import AutostartTrigger, run_autostart
    from tests.conftest import make_cfg

    user_step = AutostartStep(name="user-step", run="echo hi")
    cfg = make_cfg(
        tmp_path,
        claude={"enabled": True, "autostart": True},
        autostart={"on_create": [user_step.model_dump()]},
    )

    incus = MagicMock()
    applied: list[str] = []

    def fake_apply(_cfg, _incus, _container, step, _repo_dir, **_kwargs):
        applied.append(step.name)

    import jailbee.autostart as autostart_mod

    original = autostart_mod._apply_step
    autostart_mod._apply_step = fake_apply
    try:
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    finally:
        autostart_mod._apply_step = original

    assert applied == ["user-step"]


# --- autostart_in_progress flag ---------------------------------------------


def test_run_autostart_sets_and_clears_progress_flag(mocker):
    """The flag is set before steps run and cleared after they complete."""
    _patch_tmux(mocker)
    step = AutostartStep(name="noop", run="true")
    cfg = _cfg(steps_on_start=[step])
    incus = MagicMock()
    mocker.patch("jailbee.autostart._apply_step")
    mocker.patch("jailbee.autostart._github_token_step", return_value=None)
    mocker.patch("jailbee.autostart._claude_autostart_step", return_value=None)

    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")

    set_calls = [(c.args[1], c.args[2]) for c in incus.config_set.call_args_list]
    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert ("user.jailbee.autostart_in_progress", "1") in set_calls
    assert "user.jailbee.autostart_in_progress" in unset_keys


def test_run_autostart_clears_progress_flag_on_exception(mocker):
    """An exception in a step still clears the flag via try/finally."""
    _patch_tmux(mocker)
    step = AutostartStep(name="boom", run="false")
    cfg = _cfg(steps_on_start=[step])
    incus = MagicMock()
    mocker.patch(
        "jailbee.autostart._apply_step",
        side_effect=RuntimeError("boom"),
    )
    mocker.patch("jailbee.autostart._github_token_step", return_value=None)
    mocker.patch("jailbee.autostart._claude_autostart_step", return_value=None)

    with pytest.raises(RuntimeError):
        run_autostart(cfg, incus, "c1", AutostartTrigger.ON_START, repo_dir="/r")

    unset_keys = [c.args[1] for c in incus.config_unset.call_args_list]
    assert "user.jailbee.autostart_in_progress" in unset_keys


def test_run_autostart_no_steps_does_not_set_progress_flag(mocker):
    """Empty step list returns before setting the flag — nothing to skip."""
    _patch_tmux(mocker)
    cfg = _cfg()  # no steps
    incus = MagicMock()
    run_autostart(cfg, incus, "c1", AutostartTrigger.ON_CREATE, repo_dir="/r")
    assert incus.config_set.call_count == 0
    assert incus.config_unset.call_count == 0
