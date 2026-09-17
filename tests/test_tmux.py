"""Tests for tmux session management inside containers."""

from unittest.mock import MagicMock

import pytest

from jailbee import tmux
from jailbee.incus import IncusError
from jailbee.tmux import SENTINEL_DIR, SESSION_NAME, ensure_session


def test_ensure_session_creates_when_missing():
    incus = MagicMock()
    incus.exec.side_effect = [
        IncusError("no session"),  # has-session check fails
        "",  # mkdir succeeds
        "",  # new-session succeeds
        "",  # set-option remain-on-exit
    ]
    ensure_session(incus, "c1")
    assert incus.exec.call_count == 4
    # 2nd call: mkdir
    mkdir_args = incus.exec.call_args_list[1].args[1]
    assert "mkdir" in " ".join(mkdir_args)
    assert SENTINEL_DIR in " ".join(mkdir_args)
    # 3rd call: new-session
    new_args = incus.exec.call_args_list[2].args[1]
    new_cmd = " ".join(new_args)
    assert "new-session" in new_cmd
    assert SESSION_NAME in new_cmd
    assert " -c " not in new_cmd  # no start_dir given
    # 4th call: set-option remain-on-exit
    opt_args = incus.exec.call_args_list[3].args[1]
    assert "set-option" in " ".join(opt_args)
    assert "remain-on-exit" in " ".join(opt_args)


def test_ensure_session_passes_start_dir():
    """When ``start_dir`` is given, new-session gets ``-c <dir>``."""
    incus = MagicMock()
    incus.exec.side_effect = [
        IncusError("no session"),
        "",
        "",
        "",
    ]
    ensure_session(incus, "c1", start_dir="/home/dev/myrepo")
    new_cmd = " ".join(incus.exec.call_args_list[2].args[1])
    assert "new-session" in new_cmd
    assert "-c /home/dev/myrepo" in new_cmd


def test_ensure_session_noop_when_exists():
    incus = MagicMock()
    incus.exec.return_value = ""  # has-session succeeds
    ensure_session(incus, "c1")
    assert incus.exec.call_count == 1
    assert "has-session" in " ".join(incus.exec.call_args.args[1])


def test_ensure_session_tolerates_concurrent_creation():
    """A background `gie new` autostart can create the session between our
    has-session check and the new-session call, so tmux reports
    "duplicate session". If the session exists afterwards, that's benign and
    ensure_session must not raise."""
    incus = MagicMock()
    incus.exec.side_effect = [
        IncusError("no session"),  # has-session: missing
        "",  # mkdir
        IncusError("duplicate session: autostart"),  # new-session lost the race
        "",  # re-check has-session: now exists
    ]
    ensure_session(incus, "c1")
    assert incus.exec.call_count == 4
    # The last call re-checks has-session; no set-option (the winning
    # concurrent caller owns remain-on-exit).
    recheck_args = incus.exec.call_args_list[3].args[1]
    assert "has-session" in " ".join(recheck_args)


def test_ensure_session_reraises_when_new_session_genuinely_fails():
    """If new-session fails and the session still doesn't exist afterwards,
    the original creation error propagates (not a benign race)."""
    incus = MagicMock()
    incus.exec.side_effect = [
        IncusError("no session"),  # has-session: missing
        "",  # mkdir
        IncusError("tmux: command not found"),  # new-session genuinely failed
        IncusError("no session"),  # re-check: still missing
    ]
    with pytest.raises(IncusError, match="command not found"):
        ensure_session(incus, "c1")


def test_ensure_session_propagates_unexpected_errors():
    incus = MagicMock()
    incus.exec.side_effect = [
        IncusError("no session"),
        IncusError("mkdir failed: permission denied"),
    ]
    with pytest.raises(IncusError, match="mkdir failed"):
        ensure_session(incus, "c1")


def test_kill_window_calls_tmux():
    from jailbee.tmux import kill_window

    incus = MagicMock()
    incus.exec.return_value = ""
    kill_window(incus, "c1", "frontend")
    args = " ".join(incus.exec.call_args.args[1])
    assert "kill-window" in args
    assert "autostart:frontend" in args


def test_kill_window_ignores_missing_window():
    from jailbee.tmux import kill_window

    incus = MagicMock()
    incus.exec.side_effect = IncusError("can't find window: frontend")
    # Should not raise
    kill_window(incus, "c1", "frontend")


def test_select_window_returns_true_on_success():
    from jailbee.tmux import select_window

    incus = MagicMock()
    incus.exec.return_value = ""
    assert select_window(incus, "c1", "claude") is True
    args = " ".join(incus.exec.call_args.args[1])
    assert "select-window" in args
    assert "autostart:claude" in args


def test_select_window_returns_false_on_missing():
    from jailbee.tmux import select_window

    incus = MagicMock()
    incus.exec.side_effect = IncusError("can't find window: claude")
    assert select_window(incus, "c1", "claude") is False


def test_sanitize_window_name():
    from jailbee.tmux import _sanitize_window_name

    assert _sanitize_window_name("frontend") == "frontend"
    assert _sanitize_window_name("dev:env") == "dev_env"
    assert _sanitize_window_name("a.b.c") == "a_b_c"
    assert _sanitize_window_name("step 1") == "step_1"
    assert _sanitize_window_name("ok_-name") == "ok_-name"


def test_run_step_background_calls_new_window_and_returns():
    from jailbee.tmux import run_step

    incus = MagicMock()
    # kill-window OK, new-window OK, probe wait-for times out (= still alive)
    incus.exec.side_effect = ["", "", IncusError("exit 124: timeout")]
    run_step(
        incus,
        "c1",
        name="frontend",
        command="pnpm dev",
        env={"NODE_ENV": "dev"},
        cwd="/home/dev/repo/frontend",
        background=True,
        timeout=600,
    )
    # Three calls: kill-window, new-window, probe wait-for (timed out)
    assert incus.exec.call_count == 3
    new_window_args = " ".join(incus.exec.call_args_list[1].args[1])
    assert "new-window" in new_window_args
    assert "-t autostart:" in new_window_args
    assert "-n frontend" in new_window_args
    # env propagation via -e
    assert "-e NODE_ENV=dev" in new_window_args
    # cwd cd
    assert "cd /home/dev/repo/frontend" in new_window_args
    assert "pnpm dev" in new_window_args
    # Login shell so PATH includes ~/.local/share/pnpm etc.
    assert "bash -lc" in new_window_args
    # EXIT trap for early-death detection
    assert "trap" in new_window_args
    # Probe wait-for at index 2
    probe_args = " ".join(incus.exec.call_args_list[2].args[1])
    assert "timeout" in probe_args
    assert "tmux wait-for" in probe_args


def test_run_step_background_sanitizes_window_name():
    from jailbee.tmux import run_step

    incus = MagicMock()
    incus.exec.side_effect = ["", "", IncusError("timeout")]
    run_step(
        incus,
        "c1",
        name="dev:env",
        command="echo hi",
        env={},
        cwd="/r",
        background=True,
        timeout=60,
    )
    new_window_args = " ".join(incus.exec.call_args_list[1].args[1])
    assert "-n dev_env" in new_window_args
    assert "-t autostart:" in new_window_args


def test_run_step_background_died_early_raises():
    """If the EXIT trap fires within the probe window, run_step raises."""
    from jailbee.tmux import run_step

    incus = MagicMock()
    # kill OK, new-window OK, probe wait-for returns 0 → signal received → died
    incus.exec.side_effect = ["", "", ""]
    with pytest.raises(RuntimeError, match="died within"):
        run_step(
            incus,
            "c1",
            name="bg",
            command="false",
            env={},
            cwd="/r",
            background=True,
            timeout=600,
        )


def test_run_step_background_alive_after_probe_returns():
    """If the probe times out (process still alive), run_step returns OK."""
    from jailbee.tmux import run_step

    incus = MagicMock()
    incus.exec.side_effect = ["", "", IncusError("exit 124")]
    run_step(
        incus,
        "c1",
        name="bg",
        command="sleep 999",
        env={},
        cwd="/r",
        background=True,
        timeout=600,
    )
    assert incus.exec.call_count == 3


def test_run_step_sync_success_reads_exit_code():
    from jailbee.tmux import run_step

    incus = MagicMock()
    # Sequence: kill-window, new-window, wait-for, cat exit file, rm exit file
    incus.exec.side_effect = ["", "", "", "0\n", ""]
    run_step(
        incus,
        "c1",
        name="dev_env",
        command="make dev-env",
        env={"FOO": "bar"},
        cwd="/r",
        background=False,
        timeout=300,
    )
    assert incus.exec.call_count == 5
    new_window_args = " ".join(incus.exec.call_args_list[1].args[1])
    assert "new-window" in new_window_args
    assert "wait-for -S" in new_window_args
    assert ".jailbee/step_dev_env_" in new_window_args
    assert ".exit" in new_window_args
    # wait-for call uses timeout
    wait_args = " ".join(incus.exec.call_args_list[2].args[1])
    assert "timeout 300 tmux wait-for" in wait_args
    # cat reads the sentinel
    cat_args = " ".join(incus.exec.call_args_list[3].args[1])
    assert "cat" in cat_args and ".exit" in cat_args
    # rm cleans up
    rm_args = " ".join(incus.exec.call_args_list[4].args[1])
    assert "rm -f" in rm_args


def test_run_step_strips_trailing_newline_from_command():
    """A `run: |` block scalar always ends in a newline, and
    `AutostartStep.run` never strips it. The sync path appends
    `; rc=$?; …` to the command, so an unstripped newline would put that
    continuation on a fresh line — a bash syntax error, which means no
    sentinel is written and the caller blocks on `tmux wait-for` for the
    whole timeout before reporting a bogus failure.
    """
    from jailbee.tmux import run_step

    incus = MagicMock()
    incus.exec.side_effect = ["", "", "", "0\n", ""]
    run_step(
        incus,
        "c1",
        name="multi",
        command="echo one\necho two\n",
        env={},
        cwd="/r",
        background=False,
        timeout=60,
    )
    new_window_args = " ".join(incus.exec.call_args_list[1].args[1])
    assert "echo two\n;" not in new_window_args
    assert "echo two; rc=$?" in new_window_args


def test_run_step_background_strips_trailing_newline_from_command():
    """Same for the background path, which appends nothing today but
    composes the command into a larger `bash -lc` line all the same."""
    from jailbee.tmux import run_step

    incus = MagicMock()
    incus.exec.side_effect = ["", "", IncusError("still alive")]
    run_step(
        incus,
        "c1",
        name="bg",
        command="sleep 1\n",
        env={},
        cwd="/r",
        background=True,
        timeout=60,
    )
    new_window_args = " ".join(incus.exec.call_args_list[1].args[1])
    assert not new_window_args.rstrip("'").endswith("\n")
    assert "&& sleep 1" in new_window_args


def test_run_step_sync_nonzero_exit_raises():
    from jailbee.tmux import TmuxStepError, run_step

    incus = MagicMock()
    incus.exec.side_effect = ["", "", "", "2\n", ""]
    with pytest.raises(TmuxStepError, match="exit code 2") as exc_info:
        run_step(
            incus,
            "c1",
            name="step",
            command="false",
            env={},
            cwd="/r",
            background=False,
            timeout=60,
        )
    err = exc_info.value
    assert err.step_name == "step"
    assert err.reason == "exit"
    assert err.exit_code == 2


def test_run_step_sync_timeout_raises():
    from jailbee.tmux import run_step

    # wait-for fails (timeout returned non-zero, propagated as IncusError)
    incus = MagicMock()

    def _exec(_container, cmd, **kwargs):
        joined = " ".join(cmd)
        if "wait-for" in joined and "-S" not in joined:
            raise IncusError("`incus exec` failed (exit 124): timeout")
        return ""

    incus.exec.side_effect = _exec
    with pytest.raises(RuntimeError, match="timed out"):
        run_step(
            incus,
            "c1",
            name="step",
            command="sleep 999",
            env={},
            cwd="/r",
            background=False,
            timeout=1,
        )
    # After timeout, send-keys C-c should have been called to interrupt
    send_keys_called = any("send-keys" in " ".join(c.args[1]) for c in incus.exec.call_args_list)
    assert send_keys_called


def test_run_step_sync_missing_sentinel_raises():
    from jailbee.tmux import run_step

    # wait-for succeeds but cat returns empty (no exit file)
    incus = MagicMock()
    incus.exec.side_effect = ["", "", "", "", ""]
    with pytest.raises(RuntimeError, match="exit code missing"):
        run_step(
            incus,
            "c1",
            name="step",
            command="true",
            env={},
            cwd="/r",
            background=False,
            timeout=60,
        )


def test_run_step_sync_uses_unique_signal_per_call():
    from jailbee.tmux import run_step

    incus = MagicMock()
    incus.exec.side_effect = ["", "", "", "0\n", ""]
    run_step(
        incus,
        "c1",
        name="step",
        command="true",
        env={},
        cwd="/r",
        background=False,
        timeout=60,
    )
    sig1 = _extract_sentinel(incus.exec.call_args_list[1])
    incus.reset_mock()
    incus.exec.side_effect = ["", "", "", "0\n", ""]
    run_step(
        incus,
        "c1",
        name="step",
        command="true",
        env={},
        cwd="/r",
        background=False,
        timeout=60,
    )
    sig2 = _extract_sentinel(incus.exec.call_args_list[1])
    assert sig1 != sig2


def _extract_sentinel(call) -> str:
    """Pull the sentinel path out of a new-window call."""
    import re

    joined = " ".join(call.args[1])
    m = re.search(r"step_[A-Za-z0-9_-]+_\d+_\d+\.exit", joined)
    assert m is not None, joined
    return m.group(0)


def test_launch_step_creates_window_and_returns_handle(mocker):
    incus = mocker.Mock()
    incus.exec.return_value = ""
    handle = tmux.launch_step(
        incus,
        "c1",
        name="uv-sync",
        command="uv sync",
        env={"A": "b"},
        cwd="/home/dev/repo",
        background=False,
        timeout=300,
    )
    assert handle.name == "uv-sync"
    assert handle.window == "uv-sync"
    assert handle.sentinel.startswith(tmux.SENTINEL_DIR)
    assert handle.background is False
    joined = " ".join(" ".join(c.args[1]) for c in incus.exec.call_args_list)
    assert "tmux new-window" in joined
    # No blocking wait: launching must return immediately.
    assert "tmux wait-for" not in joined.replace("wait-for -S", "")


def test_poll_steps_returns_finished_exit_codes_and_clears_sentinels(mocker):
    incus = mocker.Mock()
    incus.exec.return_value = ""
    h1 = tmux.launch_step(
        incus, "c1", name="a", command="true", env={}, cwd="/r", background=False, timeout=300
    )
    h2 = tmux.launch_step(
        incus, "c1", name="b", command="true", env={}, cwd="/r", background=False, timeout=300
    )
    incus.exec.reset_mock()
    incus.exec.return_value = f"{h1.sentinel} 0\n"

    done = tmux.poll_steps(incus, "c1", [h1, h2])

    assert done == {"a": 0}
    joined = " ".join(" ".join(c.args[1]) for c in incus.exec.call_args_list)
    assert "rm -f" in joined


def test_poll_steps_ignores_background_handles(mocker):
    """A background handle is skipped by the ``not h.background`` guard on
    its own — even when it carries a (hypothetically) non-empty sentinel,
    which would otherwise make it look pending. Built directly rather than
    via `launch_step` (which always hands back `sentinel=""` for background
    steps) so this guard, not the empty-sentinel guard, is what's under
    test."""
    incus = mocker.Mock()
    handle = tmux.StepHandle(
        name="srv",
        window="srv",
        sentinel=f"{SENTINEL_DIR}/srv.exit",
        background=True,
        deadline=0.0,
    )
    assert tmux.poll_steps(incus, "c1", [handle]) == {}
    incus.exec.assert_not_called()


def test_launch_step_background_raises_when_the_step_dies_immediately(mocker):
    """Same early-death probe `run_step` has — a background step that exits
    inside the probe window is a config error, not a running service."""
    incus = mocker.Mock()
    incus.exec.return_value = ""  # probe wait-for returns → signal received → died
    with pytest.raises(tmux.TmuxStepError) as e:
        tmux.launch_step(
            incus,
            "c1",
            name="srv",
            command="false",
            env={},
            cwd="/r",
            background=True,
            timeout=300,
        )
    assert e.value.reason == "died_early"


def test_launch_step_background_returns_a_handle_when_the_step_survives_the_probe(mocker):
    """The other half of `launch_step`'s background branch: when the probe
    times out (still alive) rather than returning, `launch_step` must not
    raise — it hands back a fire-and-forget handle with an empty sentinel
    (`poll_steps` relies on that emptiness, alongside `background=True`, to
    skip it) and the window it created carries the early-death EXIT trap."""
    incus = mocker.Mock()
    # kill-window OK, new-window OK, probe wait-for times out (= still alive)
    incus.exec.side_effect = ["", "", IncusError("exit 124: timeout")]
    handle = tmux.launch_step(
        incus,
        "c1",
        name="srv",
        command="sleep 9",
        env={},
        cwd="/r",
        background=True,
        timeout=300,
    )
    assert handle.name == "srv"
    assert handle.window == "srv"
    assert handle.background is True
    assert handle.sentinel == ""
    new_window_args = " ".join(incus.exec.call_args_list[1].args[1])
    assert "trap" in new_window_args
    assert "tmux wait-for -S" in new_window_args


def test_new_windows_are_detached_so_they_do_not_steal_focus(mocker):
    """`tmux new-window` makes the new window current unless `-d` is given.

    An autostart run creating windows while someone is attached with
    `jailbee tmux` would otherwise yank their view to every step in turn.
    """
    incus = mocker.Mock()
    incus.exec.side_effect = ["", "", IncusError("exit 124: still alive")]
    tmux.run_step(
        incus,
        "c1",
        name="frontend",
        command="pnpm dev",
        env={},
        cwd="/r",
        background=True,
        timeout=60,
    )
    new_window_args = " ".join(incus.exec.call_args_list[1].args[1])
    assert "new-window -d " in new_window_args


def test_sync_new_window_is_detached(mocker):
    incus = mocker.Mock()
    incus.exec.side_effect = ["", "", "", "0\n", ""]
    tmux.run_step(
        incus,
        "c1",
        name="build",
        command="make",
        env={},
        cwd="/r",
        background=False,
        timeout=60,
    )
    assert "new-window -d " in " ".join(incus.exec.call_args_list[1].args[1])


def test_launch_step_new_window_is_detached(mocker):
    incus = mocker.Mock()
    incus.exec.return_value = ""
    tmux.launch_step(
        incus,
        "c1",
        name="uv-sync",
        command="uv sync",
        env={},
        cwd="/r",
        background=False,
        timeout=60,
    )
    assert "new-window -d " in " ".join(incus.exec.call_args_list[1].args[1])
