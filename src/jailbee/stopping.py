"""Stopping a container without staring at a ten-minute silent wait.

`incus stop` with no `--timeout` looks like "stop it", but incusd reads the
CLI's default `-1` as **600 seconds** of clean-shutdown budget
(``cmd/incusd/instance_state.go``: ``req.Timeout < 0`` → ``600``). A
container whose init never finishes shutting down therefore blocks jailbee
for ten unannounced minutes and then fails with::

    Failed shutting down instance, status is "Running": context deadline exceeded

which is how a `jailbee base build` lost a complete, successful provisioning
run at the very last step before publishing.

So every jailbee stop goes through here:

* an explicit, much shorter budget, with the elapsed time on screen;
* on expiry, a diagnosis taken *while the container is still up* — the
  pending systemd job, processes stuck in uninterruptible sleep, and the tail
  of the console log, which is where ``A stop job is running for …`` appears;
* for containers jailbee is about to discard anyway, a forced stop instead of
  a failed command; for the user's own containers, an error that names a
  command that still works.
"""

from __future__ import annotations

from jailbee.incus import Incus, IncusError
from jailbee.tui import status_with_elapsed, warn, warn_plain

# Seconds of clean shutdown before jailbee stops waiting. Two minutes is well
# past a healthy container (a second or two) and past the 90s systemd spends
# on a single unit's default TimeoutStopSec, while staying inside a user's
# patience for a command they are watching.
CLEAN_STOP_BUDGET = 120

# incusd's wording when the budget expires and the container is still up
# (``internal/server/instance/drivers/driver_lxc.go``). Matched rather than
# assumed: every *other* stop failure — no such instance, already stopped,
# storage error — is a real error and must never be papered over with a
# forced stop.
_STUCK_MARKER = "Failed shutting down instance"

# A container too wedged to shut down may be too wedged to exec into, so each
# probe gets a short leash of its own.
_PROBE_TIMEOUT = 15

# The console log holds a whole boot; only the end of it is about the
# shutdown that just failed.
_CONSOLE_TAIL_LINES = 25


def _pending_systemd_jobs(incus: Incus, name: str) -> str:
    """Jobs systemd is still waiting on — the direct answer, when it works."""
    out = incus.exec(
        name,
        ["systemctl", "list-jobs", "--no-pager", "--no-legend"],
        timeout=_PROBE_TIMEOUT,
    ).strip()
    # `--no-legend` still prints "No jobs running." when there are none.
    if not out or out.startswith("No jobs"):
        return ""
    return out


def _uninterruptible_processes(incus: Incus, name: str) -> str:
    """Processes in D state — unkillable, and so unstoppable by systemd too.

    The other half of the answer: when systemd has no pending job but the
    container still will not go down, something is blocked in the kernel
    (storage stall, a hung mount) and no shutdown timeout can help.
    """
    out = incus.exec(
        name,
        ["ps", "-eo", "state=,pid=,comm="],
        timeout=_PROBE_TIMEOUT,
    )
    stuck = [
        line.strip().removeprefix("D").strip()
        for line in out.splitlines()
        if line.strip().startswith("D")
    ]
    return "\n".join(stuck)


def _console_tail(incus: Incus, name: str) -> str:
    """The end of the guest console — where "A stop job is running for …" is."""
    out = incus.console_log(name, timeout=_PROBE_TIMEOUT).rstrip()
    if not out:
        return ""
    return "\n".join(out.splitlines()[-_CONSOLE_TAIL_LINES:])


def diagnose_stuck_shutdown(incus: Incus, name: str) -> str:
    """Ask a container that refuses to stop why, while it can still answer.

    Every probe is best-effort: this runs on a path that has already failed,
    and a probe that fails too must not replace the failure the caller is
    reporting. Returns "" when nothing was learned.
    """
    sections: list[tuple[str, str]] = []
    probes = (
        ("systemd jobs still pending", _pending_systemd_jobs),
        ("processes in uninterruptible sleep (D)", _uninterruptible_processes),
        (f"last {_CONSOLE_TAIL_LINES} console lines", _console_tail),
    )
    for title, probe in probes:
        try:
            body = probe(incus, name)
        except IncusError:
            continue
        if body:
            sections.append((title, body))
    return "\n".join(
        f"{title}:\n" + "\n".join(f"  {line}" for line in body.splitlines())
        for title, body in sections
    )


def stop_container(
    incus: Incus,
    name: str,
    *,
    force: bool = False,
    budget: int = CLEAN_STOP_BUDGET,
    force_fallback: bool = False,
    label: str | None = None,
    show_progress: bool = True,
) -> bool:
    """Stop ``name`` within ``budget`` seconds. Returns True if force was used.

    ``force`` skips the clean shutdown outright (the user asked for a power
    cut). ``force_fallback`` pulls the plug only after the clean shutdown ran
    out of budget — correct for containers the caller is about to publish and
    delete, wrong for a container holding the user's work.

    ``show_progress`` draws the elapsed-time spinner; callers that already own
    a Rich Live display (the dashboard, the registry's ``on_step`` reporting)
    must pass False, since Rich allows only one live display at a time.
    """
    what = label or name

    if force:
        incus.stop(name, force=True)
        return True

    try:
        if show_progress:
            with status_with_elapsed(f"stopping {what} (clean shutdown, up to {budget}s)"):
                incus.stop(name, timeout=budget)
        else:
            incus.stop(name, timeout=budget)
    except IncusError as exc:
        if _STUCK_MARKER not in str(exc):
            raise
        warn(f"{what} did not shut down cleanly within {budget}s")
        details = diagnose_stuck_shutdown(incus, name)
        if details:
            warn_plain(f"what is holding it up:\n{details}")
        if not force_fallback:
            raise IncusError(
                f"{what} is still running after a {budget}s clean-shutdown request. "
                f"Inspect it with `incus console --show-log {name}`, then stop it "
                f"with --force once you know why — a forced stop is a power cut, "
                f"so anything unsaved inside the container is lost. "
                f"Incus reported: {exc}"
            ) from exc
        warn(f"Pulling the plug on {what} — it is disposable, so nothing is lost")
        incus.stop(name, force=True)
        return True
    return False
