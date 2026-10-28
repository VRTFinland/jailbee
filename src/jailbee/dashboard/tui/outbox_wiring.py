"""The terminal dashboard's `OutboxRunner`: the Outbox box's children, run for one container.

`jailbee.dashboard.outbox_flow.OutboxController` decides what to run; this
runs it the way every other dashboard child runs: addressed at the container's
repo (``--config``, never over SSH), checked against the remote-SSH policy
before anything starts, quiet children through `run_cli_quiet`, long ones as
detached `JobRunner` jobs, and terminal hand-offs through
`DashboardSession.run_dashboard_command`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from jailbee.dashboard import accounts as da
from jailbee.dashboard import actions as dact
from jailbee.dashboard.commands import check_dashboard_command, permitted
from jailbee.dashboard.jobs import JobResult
from jailbee.remote_ssh.router import RouteError

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoTarget
    from jailbee.dashboard.tui.session import DashboardSession


class SessionOutboxRunner:
    """`OutboxRunner` for ``container``'s box in ``session``."""

    def __init__(self, session: DashboardSession, container: str) -> None:
        self.session = session
        self.container = container

    def _addressed(self, argv: Sequence[str]) -> tuple[RepoTarget, list[str]]:
        """``argv`` pointed at the container's repo and checked; RouteError when it may not run."""
        session = self.session
        repo = session.repo_for(self.container, "container")
        if repo is None:
            raise RouteError(f"'{self.container}' is gone")
        full = dact.addressed(argv, repo.flags(), over_ssh=session.over_ssh)
        check_dashboard_command(full, session.ssh_policy, over_ssh=session.over_ssh)
        return repo, full

    def refusal(self, argv: Sequence[str]) -> str | None:
        """Why ``argv`` may not run for this container, or None."""
        try:
            self._addressed(argv)
        except RouteError as exc:
            return str(exc)
        return None

    def permitted(self, argv: Sequence[str]) -> bool:
        return permitted(argv, self.session.ssh_policy, over_ssh=self.session.over_ssh)

    def run_quiet(self, argv: Sequence[str]) -> da.CliResult:
        try:
            repo, full = self._addressed(argv)
        except RouteError as exc:
            return da.CliResult(False, str(exc))
        return da.run_cli_quiet(full, cwd=repo.cwd())

    def run_quiet_later(
        self, key: str, label: str, argv: Sequence[str], on_done: Callable[[da.CliResult], None]
    ) -> None:
        self.session.jobs.call(key, label, lambda: self.run_quiet(argv), on_done)

    def start(
        self, key: str, label: str, argv: Sequence[str], on_done: Callable[[JobResult], None]
    ) -> None:
        """Start a detached job; a refused or unaddressable argv raises ValueError, unstarted.

        The controller turns the ValueError (as an OSError from a vanished cwd,
        or a job already running under ``key``) into a status line.
        """
        try:
            repo, full = self._addressed(argv)
        except RouteError as exc:
            raise ValueError(str(exc)) from None
        self.session.jobs.start(
            key, label, ["jailbee", *full], repo.cwd(), on_done, capture_stdout=True
        )

    def hand_off(self, argv: Sequence[str]) -> int | None:
        return self.session.run_dashboard_command(self.container, "container", list(argv))

    def hand_off_diagnostic(self, argv: Sequence[str]) -> tuple[int | None, str | None]:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="jailbee-outbox-") as tmp:
            path = Path(tmp) / "diagnostic"
            rc = self.session.run_dashboard_command(
                self.container, "container", list(argv), diagnostic_path=path
            )
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            return rc, text or None

    def changed(self) -> None:
        self.session.client.refresh()
