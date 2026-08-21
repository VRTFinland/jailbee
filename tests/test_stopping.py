"""Tests for the bounded-clean-shutdown stop helper."""

from __future__ import annotations

import pytest

from jailbee.incus import IncusError
from jailbee.stopping import (
    CLEAN_STOP_BUDGET,
    diagnose_stuck_shutdown,
    stop_container,
)

# The exact text incusd returns when the clean-shutdown deadline expires
# (internal/server/instance/drivers/driver_lxc.go), wrapped the way
# `Incus._run` wraps a non-zero exit.
STUCK = IncusError(
    "`incus stop foo --timeout 120` failed (exit 1): Error: Failed shutting "
    'down instance, status is "Running": context deadline exceeded'
)


def test_clean_stop_passes_an_explicit_budget(mocker):
    incus = mocker.MagicMock()
    forced = stop_container(incus, "foo")
    incus.stop.assert_called_once_with("foo", timeout=CLEAN_STOP_BUDGET)
    assert forced is False


def test_force_skips_the_clean_shutdown_entirely(mocker):
    incus = mocker.MagicMock()
    forced = stop_container(incus, "foo", force=True)
    incus.stop.assert_called_once_with("foo", force=True)
    assert forced is True


def test_stuck_shutdown_falls_back_to_force_when_allowed(mocker):
    incus = mocker.MagicMock()
    incus.stop.side_effect = [STUCK, None]
    forced = stop_container(incus, "foo", force_fallback=True)
    assert forced is True
    assert incus.stop.call_args_list[-1] == mocker.call("foo", force=True)


def test_stuck_shutdown_without_fallback_raises_an_actionable_error(mocker):
    incus = mocker.MagicMock()
    incus.stop.side_effect = STUCK
    with pytest.raises(IncusError) as excinfo:
        stop_container(incus, "foo")
    message = str(excinfo.value)
    # The user has to be told what was waited for, and given a command that
    # still works — the old message pointed at `incus info --show-log` for a
    # container the caller had already deleted.
    assert str(CLEAN_STOP_BUDGET) in message
    assert "incus console --show-log foo" in message
    assert "--force" in message
    # No plug pulled without being asked.
    assert mocker.call("foo", force=True) not in incus.stop.call_args_list


def test_other_stop_failures_are_never_forced(mocker):
    """A real error must not be masked by a force fallback."""
    incus = mocker.MagicMock()
    incus.stop.side_effect = IncusError("`incus stop foo` failed (exit 1): Error: not found")
    with pytest.raises(IncusError, match="not found"):
        stop_container(incus, "foo", force_fallback=True)
    assert incus.stop.call_count == 1
    incus.exec.assert_not_called()


def test_stuck_shutdown_is_diagnosed_before_the_plug_is_pulled(mocker):
    """The probes only work while the container is still up — run them first."""
    incus = mocker.MagicMock()
    calls: list[str] = []

    def fake_stop(name, **kwargs):
        if kwargs.get("force"):
            calls.append("force")
            return None
        calls.append("clean")
        raise STUCK

    def fake_exec(name, cmd, **kwargs):
        calls.append("exec")
        return ""

    incus.stop.side_effect = fake_stop
    incus.exec.side_effect = fake_exec
    incus.console_log.return_value = ""
    stop_container(incus, "foo", force_fallback=True)
    assert calls.index("exec") < calls.index("force")


def test_diagnostics_failure_does_not_block_the_force(mocker):
    incus = mocker.MagicMock()
    incus.stop.side_effect = [STUCK, None]
    incus.exec.side_effect = IncusError("exec failed")
    incus.console_log.side_effect = IncusError("no log")
    forced = stop_container(incus, "foo", force_fallback=True)
    assert forced is True


def test_diagnose_reports_the_pending_systemd_job(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = (
        "JOB UNIT                     TYPE  STATE\n123 apt-daily-upgrade.service stop  running\n"
    )
    incus.console_log.return_value = ""
    out = diagnose_stuck_shutdown(incus, "foo")
    assert "apt-daily-upgrade.service" in out


def test_diagnose_reports_uninterruptible_processes(mocker):
    incus = mocker.MagicMock()

    def fake_exec(name, cmd, **kwargs):
        joined = " ".join(cmd)
        if "list-jobs" in joined:
            return "No jobs running.\n"
        return "S 1 systemd\nD 4242 dpkg\nR 9 ps\n"

    incus.exec.side_effect = fake_exec
    incus.console_log.return_value = ""
    out = diagnose_stuck_shutdown(incus, "foo")
    assert "4242 dpkg" in out
    assert "systemd" not in out


def test_diagnose_tails_the_console_log(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    incus.console_log.return_value = "\n".join(f"line {i}" for i in range(100))
    out = diagnose_stuck_shutdown(incus, "foo")
    assert "line 99" in out
    assert "line 0\n" not in out


def test_diagnose_probes_are_time_bounded(mocker):
    """A container too wedged to shut down can be too wedged to exec into."""
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    incus.console_log.return_value = ""
    diagnose_stuck_shutdown(incus, "foo")
    for call in incus.exec.call_args_list:
        assert call.kwargs.get("timeout") is not None


def test_diagnose_returns_empty_when_nothing_is_learned(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = "No jobs running.\n"
    incus.console_log.return_value = ""
    assert diagnose_stuck_shutdown(incus, "foo") == ""
