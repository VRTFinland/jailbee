"""Interactive retry for git operations that talk to a remote.

A remote git operation can fail for reasons that do not repeat: an
authentication round-trip that never completed, a dropped connection. When that
happens partway through a command that has already done expensive or interactive
work, aborting discards it. `jailbee pr` is the motivating case — it generates the
PR title, body and head branch name with the container's Claude CLI and has the
branch name confirmed, all before it ever pushes, and all of it lives only in
process memory.

`with_remote_retry` wraps the single failing command so the user can re-run it in
place. Declining reproduces the old behaviour exactly: the original exception
propagates untouched.

Design rules this module obeys:
  - It NEVER prints. All output belongs to the injected `confirm` callback, so a
    caller that supplies its own is in full control of the terminal.
  - The retry offer is TTY-gated, so background workers (`background.py`) and
    the unit suite never block on a prompt.
  - No module state: `confirm` is a parameter with a default, not a hook that
    callers mutate.
  - Messages are generic. The cause is whatever git reported; jailbee does not
    speculate about the user's hardware.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from jailbee.tui import error


def _stdin_is_interactive() -> bool:
    """Return True if stdin is a TTY (and JAILBEE_NONINTERACTIVE is unset)."""
    return sys.stdin.isatty() and not os.environ.get("JAILBEE_NONINTERACTIVE")


def _ask(label: str) -> bool:
    """Ask `Retry <label>?` on a TTY; False off-TTY without prompting."""
    if not _stdin_is_interactive():
        return False
    return input(f"Retry {label}? [y/N]: ").strip().lower() in ("y", "yes")


def confirm_retry(label: str, exc: Exception) -> bool:
    """Report `exc`, then ask whether to retry `label`.

    The default for `with_remote_retry`. Use it when the failing command
    captured its own stderr, so nothing has reached the terminal yet and a bare
    prompt would give the user no reason for the question.

    Several jailbee exception types (e.g. `git.GitFetchError`) capture the
    subprocess's stderr in a `.stderr` attribute specifically so callers can
    report the real diagnostic without re-running the command. `str(exc)` on
    those types is often just a generic "command failed in <path>" message, so
    prefer a non-empty `.stderr` when present; fall back to `str(exc)`
    otherwise. This is read generically via `getattr` rather than importing
    any git-specific exception, keeping `retry.py` free of git knowledge.
    """
    detail = getattr(exc, "stderr", "") or ""
    detail = detail.strip() if isinstance(detail, str) else ""
    error(f"{label[:1].upper()}{label[1:]} failed: {detail or exc}")
    return _ask(label)


def confirm_retry_quiet(label: str, exc: Exception) -> bool:
    """Ask whether to retry `label` without reporting `exc`.

    For commands whose output is inherited by the parent process (notably
    `git.push_to_origin`): git has already printed the failure to the terminal
    directly above the prompt, and the caller prints its own error below if the
    user declines, so a third copy would be noise. `exc` is accepted to keep the
    signature interchangeable with `confirm_retry`.
    """
    return _ask(label)


def with_remote_retry[T](
    op: Callable[[], T],
    *,
    label: str,
    catch: type[Exception] | tuple[type[Exception], ...],
    not_retryable: tuple[type[Exception], ...] = (),
    confirm: Callable[[str, Exception], bool] = confirm_retry,
) -> T:
    """Run `op()`, offering the user a retry when it fails.

    On an exception matching `catch` but not `not_retryable`, calls
    `confirm(label, exc)`. True re-runs `op()`; False re-raises the original
    exception unchanged. The loop is unbounded — the user drives it, and every
    failed attempt asks again. Anything in `not_retryable`, or outside `catch`,
    propagates immediately.

    `label` is a lowercase gerund phrase ("pushing 'x' to origin", "fetching
    origin/main") so it reads correctly both capitalised at the start of a
    report line and inline in the question.

    `op` must be safe to run more than once: wrap the single remote command, not
    the surrounding work.
    """
    while True:
        try:
            return op()
        except Exception as exc:
            # One `except Exception` block plus explicit isinstance tests
            # (rather than `except catch as exc: ... except not_retryable:`)
            # keeps the catch/not_retryable precedence visible in one place.
            if not isinstance(exc, catch) or isinstance(exc, not_retryable):
                raise
            if not confirm(label, exc):
                raise
