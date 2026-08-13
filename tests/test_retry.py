"""Tests for retry.with_remote_retry — interactive retry for remote git ops."""

from __future__ import annotations

import pytest

# --- with_remote_retry ----------------------------------------------------


def test_returns_value_without_asking_when_op_succeeds(mocker):
    from jailbee.retry import with_remote_retry

    confirm = mocker.Mock(return_value=True)
    op = mocker.Mock(return_value="ok")

    result = with_remote_retry(op, label="pushing to origin", catch=RuntimeError, confirm=confirm)

    assert result == "ok"
    op.assert_called_once_with()
    confirm.assert_not_called()


def test_reruns_op_when_confirm_accepts(mocker):
    from jailbee.retry import with_remote_retry

    confirm = mocker.Mock(return_value=True)
    op = mocker.Mock(side_effect=[RuntimeError("boom"), "ok"])

    result = with_remote_retry(op, label="pushing to origin", catch=RuntimeError, confirm=confirm)

    assert result == "ok"
    assert op.call_count == 2
    label, exc = confirm.call_args.args
    assert label == "pushing to origin"
    assert isinstance(exc, RuntimeError)


def test_keeps_asking_until_confirm_declines(mocker):
    """The loop is unbounded — the user drives it, not a retry counter."""
    from jailbee.retry import with_remote_retry

    confirm = mocker.Mock(side_effect=[True, True, False])
    op = mocker.Mock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        with_remote_retry(op, label="pushing", catch=RuntimeError, confirm=confirm)

    assert op.call_count == 3
    assert confirm.call_count == 3


def test_reraises_the_original_exception_object_when_declined(mocker):
    from jailbee.retry import with_remote_retry

    original = RuntimeError("boom")
    confirm = mocker.Mock(return_value=False)
    op = mocker.Mock(side_effect=original)

    with pytest.raises(RuntimeError) as excinfo:
        with_remote_retry(op, label="pushing", catch=RuntimeError, confirm=confirm)

    assert excinfo.value is original
    op.assert_called_once_with()


def test_not_retryable_propagates_without_asking(mocker):
    from jailbee.retry import with_remote_retry

    class DeterministicError(RuntimeError):
        """A subclass of the caught type that a retry cannot fix."""

    confirm = mocker.Mock(return_value=True)
    op = mocker.Mock(side_effect=DeterministicError("diverged"))

    with pytest.raises(DeterministicError):
        with_remote_retry(
            op,
            label="fetching",
            catch=RuntimeError,
            not_retryable=(DeterministicError,),
            confirm=confirm,
        )

    op.assert_called_once_with()
    confirm.assert_not_called()


def test_exceptions_outside_catch_propagate_without_asking(mocker):
    from jailbee.retry import with_remote_retry

    confirm = mocker.Mock(return_value=True)
    op = mocker.Mock(side_effect=ValueError("unrelated"))

    with pytest.raises(ValueError, match="unrelated"):
        with_remote_retry(op, label="pushing", catch=RuntimeError, confirm=confirm)

    confirm.assert_not_called()


def test_catch_accepts_a_tuple_of_types(mocker):
    from jailbee.retry import with_remote_retry

    confirm = mocker.Mock(return_value=True)
    op = mocker.Mock(side_effect=[KeyError("k"), "ok"])

    result = with_remote_retry(
        op, label="fetching", catch=(RuntimeError, KeyError), confirm=confirm
    )

    assert result == "ok"
    assert confirm.call_count == 1


# --- the default confirm callbacks ----------------------------------------


def test_confirm_retry_is_false_off_tty_and_does_not_prompt(mocker):
    from jailbee import retry

    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=False)
    prompt = mocker.patch("builtins.input")

    assert retry.confirm_retry("pushing", RuntimeError("boom")) is False
    prompt.assert_not_called()


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("y", True),
        ("Y", True),
        ("yes", True),
        (" YES ", True),
        ("", False),
        ("n", False),
        ("nope", False),
    ],
)
def test_confirm_retry_reads_the_answer(mocker, answer, expected):
    from jailbee import retry

    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value=answer)

    assert retry.confirm_retry("pushing", RuntimeError("boom")) is expected


def test_confirm_retry_reports_the_failure_with_a_capitalised_label(mocker):
    from jailbee import retry

    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="n")
    reported = mocker.patch("jailbee.retry.error")

    retry.confirm_retry("fetching origin/main", RuntimeError("connection refused"))

    reported.assert_called_once_with("Fetching origin/main failed: connection refused")


def test_confirm_retry_reports_stderr_when_the_exception_carries_one(mocker):
    """`.stderr` is the real diagnostic captured at the raise site; prefer it
    over the exception's generic `str()` (e.g. GitFetchError's "failed in
    <path>")."""
    from jailbee import retry

    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="n")
    reported = mocker.patch("jailbee.retry.error")

    class FakeFetchError(RuntimeError):
        def __init__(self, message, *, stderr=""):
            super().__init__(message)
            self.stderr = stderr

    exc = FakeFetchError("git fetch origin dev failed in /repo", stderr="fatal: connection refused")

    retry.confirm_retry("fetching origin/dev", exc)

    reported.assert_called_once_with("Fetching origin/dev failed: fatal: connection refused")


@pytest.mark.parametrize("blank_stderr", ["", "   ", "\n"])
def test_confirm_retry_falls_back_to_str_when_stderr_is_blank(mocker, blank_stderr):
    from jailbee import retry

    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="n")
    reported = mocker.patch("jailbee.retry.error")

    class FakeFetchError(RuntimeError):
        def __init__(self, message, *, stderr=""):
            super().__init__(message)
            self.stderr = stderr

    exc = FakeFetchError("git fetch origin dev failed in /repo", stderr=blank_stderr)

    retry.confirm_retry("fetching origin/dev", exc)

    reported.assert_called_once_with(
        "Fetching origin/dev failed: git fetch origin dev failed in /repo"
    )


def test_confirm_retry_falls_back_to_str_when_stderr_is_not_a_string(mocker):
    """A non-string `.stderr` must not crash confirm_retry."""
    from jailbee import retry

    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="n")
    reported = mocker.patch("jailbee.retry.error")

    exc = RuntimeError("connection refused")
    exc.stderr = 12345  # not a string

    retry.confirm_retry("fetching origin/dev", exc)

    reported.assert_called_once_with("Fetching origin/dev failed: connection refused")


def test_confirm_retry_quiet_prompts_without_reporting(mocker):
    from jailbee import retry

    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    prompt = mocker.patch("builtins.input", return_value="y")
    reported = mocker.patch("jailbee.retry.error")

    assert retry.confirm_retry_quiet("pushing 'x' to origin", RuntimeError("exit 128")) is True

    reported.assert_not_called()
    assert "pushing 'x' to origin" in prompt.call_args.args[0]


def test_confirm_retry_quiet_is_false_off_tty_and_does_not_prompt(mocker):
    from jailbee import retry

    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=False)
    prompt = mocker.patch("builtins.input")

    assert retry.confirm_retry_quiet("pushing", RuntimeError("boom")) is False
    prompt.assert_not_called()


def test_stdin_is_interactive_respects_env_and_tty(monkeypatch):
    from jailbee.retry import _stdin_is_interactive

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.delenv("JAILBEE_NONINTERACTIVE", raising=False)
    assert _stdin_is_interactive() is True

    monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    assert _stdin_is_interactive() is False

    monkeypatch.delenv("JAILBEE_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _stdin_is_interactive() is False
