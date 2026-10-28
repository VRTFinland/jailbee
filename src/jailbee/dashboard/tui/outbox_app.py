"""The standalone `jailbee outbox` app: the Outbox box full screen.

The same `OutboxBox` the dashboard opens for one container, here for every
container of the current repo (or of every registered repo, `a`). The
`OutboxController` decides what to run; `LocalOutboxRunner` runs it the way the
dashboard does — quiet `jailbee outbox …` children addressed with
``--config``, long ones as detached `JobRunner` jobs polled by the app's timer,
terminal hand-offs with the app suspended.

Over SSH (`cli_outbox.browser_read_only`) the runner permits nothing but
``outbox ls``: every mutation key is off and a line says which explicit
commands to use instead.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.message import Message
from textual.widgets import Static

from jailbee.dashboard import accounts as da
from jailbee.dashboard.dispatch import _run_cli_foreground
from jailbee.dashboard.jobs import JobResult, JobRunner
from jailbee.dashboard.model import RepoTarget
from jailbee.dashboard.outbox import OutboxState
from jailbee.dashboard.outbox_flow import OutboxController
from jailbee.dashboard.tui import scrollbar
from jailbee.dashboard.tui.native import OverlayBox
from jailbee.dashboard.tui.outbox_box import OutboxBox
from jailbee.remote_ssh.router import RouteError

POLL_SECONDS = 0.25
READ_ONLY_LINE = "Read-only over SSH: use explicit `jailbee outbox drop` / `outbox apply` commands."
_LOADING = "reading the outbox…"


def container_repo(target: RepoTarget, name: str) -> RepoTarget | None:
    """The repo that owns container ``name``, seen from ``target``; None when unknown.

    The `outbox` children find a container's own repo themselves
    (`outbox.commands.resolve_target`); `pr` and `submodule pr` act on the repo
    they are addressed at, so a hand-off of one must be addressed at the
    container's repo, not the app's (they differ in the all-repos view).
    """
    from jailbee.config import ConfigError, load_config, load_repo_config
    from jailbee.incus import Incus, IncusError
    from jailbee.outbox.commands import resolve_target
    from jailbee.outbox.models import OutboxError, OutboxExecutionError
    from jailbee.paths import repo_config_path
    from jailbee.remote_ssh.repo_scope import RepoScopeError

    try:
        path = target.config_path
        cfg = load_config(path) if path is not None else load_repo_config(target.repo_root)
        own, _ = resolve_target(cfg, Incus(), name)
    except (ConfigError, IncusError, OutboxError, OutboxExecutionError, RepoScopeError, OSError):
        return None
    return RepoTarget(own.repo_root, repo_config_path(own.repo_root))


def _handed_container(argv: Sequence[str]) -> str | None:
    """The container of a `pr push NAME` / `submodule pr push NAME PATH` hand-off, else None."""
    words = list(argv)
    if words[:2] == ["pr", "push"] and len(words) > 2:
        return words[2]
    if words[:3] == ["submodule", "pr", "push"] and len(words) > 3:
        return words[3]
    return None


class LocalOutboxRunner:
    """`OutboxRunner` for the standalone app: children of `jailbee` against ``target``."""

    def __init__(
        self, target: RepoTarget, *, read_only: bool, suspend: Callable[[Callable[[], int]], int]
    ) -> None:
        self.target = target
        self.read_only = read_only
        self.suspend = suspend
        self.jobs = JobRunner()
        # Container -> its repo, for PR hand-offs; only successes are kept.
        self._repos: dict[str, RepoTarget] = {}

    def _repo_of(self, name: str) -> RepoTarget | None:
        repo = self._repos.get(name)
        if repo is None:
            repo = container_repo(self.target, name)
            if repo is not None:
                self._repos[name] = repo
        return repo

    def _full(self, argv: Sequence[str]) -> list[str]:
        return [*argv, *self.target.flags()]

    def permitted(self, argv: Sequence[str]) -> bool:
        """Anything when writable; read-only (over SSH): only a listing of this repo.

        `--all-repos` is refused read-only too: the SSH session's repository
        exclusions are enforced by the router, which these children bypass.
        """
        if not self.read_only:
            name = _handed_container(argv)
            return name is None or self._repo_of(name) is not None
        return list(argv[:2]) == ["outbox", "ls"] and "--all-repos" not in argv

    def _refusal(self, argv: Sequence[str]) -> str | None:
        if self.permitted(argv):
            return None
        if not self.read_only:
            return f"the repository of '{_handed_container(argv)}' is unknown"
        return f"not available read-only over SSH: jailbee {' '.join(argv[:2])}"

    def run_quiet(self, argv: Sequence[str]) -> da.CliResult:
        refusal = self._refusal(argv)
        if refusal is not None:
            return da.CliResult(False, refusal)
        return da.run_cli_quiet(self._full(argv), cwd=self.target.cwd())

    def run_quiet_later(
        self, key: str, label: str, argv: Sequence[str], on_done: Callable[[da.CliResult], None]
    ) -> None:
        self.jobs.call(key, label, lambda: self.run_quiet(argv), on_done)

    def start(
        self, key: str, label: str, argv: Sequence[str], on_done: Callable[[JobResult], None]
    ) -> None:
        """A detached job; OSError/ValueError (no cwd, running, refused) reach the controller."""
        refusal = self._refusal(argv)
        if refusal is not None:
            raise ValueError(refusal)
        self.jobs.start(
            key, label, ["jailbee", *self._full(argv)], self.target.cwd(), on_done,
            capture_stdout=True,
        )  # fmt: skip

    def hand_off(self, argv: Sequence[str]) -> int | None:
        """``argv`` on the real terminal; None when it could not run (or is refused)."""
        if self._refusal(argv) is not None:
            return None
        name = _handed_container(argv)
        repo = self.target if name is None else self._repo_of(name)
        if repo is None:
            return None
        try:
            return self.suspend(lambda: _run_cli_foreground(repo, list(argv), style="output"))
        except (RouteError, OSError):
            return None

    def hand_off_diagnostic(self, argv: Sequence[str]) -> tuple[int | None, str | None]:
        import tempfile
        from pathlib import Path

        if self._refusal(argv) is not None:
            return None, None
        name = _handed_container(argv)
        repo = self.target if name is None else self._repo_of(name)
        if repo is None:
            return None, None
        with tempfile.TemporaryDirectory(prefix="jailbee-outbox-") as tmp:
            path = Path(tmp) / "diagnostic"
            try:
                rc = self.suspend(
                    lambda: _run_cli_foreground(
                        repo, list(argv), style="output", diagnostic_path=path
                    )
                )
            except (RouteError, OSError):
                return None, None
            from jailbee.dashboard.dispatch import read_diagnostic_text

            return rc, read_diagnostic_text(path)

    def changed(self) -> None:
        """Nothing else shows the outbox here."""


class OutboxApp(App[int], inherit_bindings=False):
    """One full-screen `OutboxBox`; keys are routed to it one at a time.

    ``inherit_bindings=False`` drops Textual's own bindings: every key goes
    through `on_event`, so Ctrl-C cancels a question instead of quitting.
    """

    CSS = """
    Screen { background: ansi_default; }
    #outbox-read-only {
        height: 1; width: 1fr; background: ansi_default; color: ansi_default;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    """
    ENABLE_COMMAND_PALETTE = False
    ALLOW_SELECT = False

    def __init__(
        self, target: RepoTarget, *, container: str | None, all_repos: bool, read_only: bool
    ) -> None:
        super().__init__(ansi_color=True)
        scrollbar.install()
        self.target = target
        self.read_only = read_only
        # A named container and --all-repos exclude each other (`outbox ls`):
        # the wide scope lists every repo, the narrow one the named container again.
        self._container = container
        self.runner = LocalOutboxRunner(target, read_only=read_only, suspend=self._suspend)
        self.controller = OutboxController(
            self.runner,
            title=container or target.repo_root.name,
            container=None if all_repos else container,
            standalone=True,
            on_change=self._changed,
        )
        self.controller.state = replace(self.controller.state, all_repos=all_repos, busy=_LOADING)

    # -- plumbing -----------------------------------------------------------

    def _suspend(self, fn: Callable[[], int]) -> int:
        driver = self._driver
        suspended: AbstractContextManager[None] = (
            self.suspend() if driver is not None and driver.can_suspend else nullcontext()
        )
        with suspended:
            return fn()

    @property
    def box(self) -> OutboxBox:
        return self.query_one(OutboxBox)

    def _changed(self, state: OutboxState) -> None:
        if self.is_running and self.box.is_mounted:
            self.box.show(state)

    def _poll(self) -> None:
        self.runner.jobs.poll()

    # -- Textual ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        if self.read_only:
            yield Static(Text(READ_ONLY_LINE, style="yellow"), id="outbox-read-only", markup=False)
        yield OutboxBox(self.controller.state, mouse_enabled=lambda: True, standalone=True)

    def on_mount(self) -> None:
        self.set_interval(POLL_SECONDS, self._poll)
        self.call_after_refresh(self._load)

    def _load(self) -> None:
        self.controller.state = replace(self.controller.state, busy=None)
        self.controller.load_later()

    async def on_event(self, event: events.Event) -> None:
        # As in `DashboardApp`: keys are routed one at a time, never through focus.
        if isinstance(event, events.Key) and not event.is_forwarded:
            self.app_focus = True
            await self._route_key(event)
        else:
            await super().on_event(event)

    async def _route_key(self, event: events.Key) -> None:
        outcome = await self.box.handle_key(event)
        if isinstance(outcome, Message):
            getattr(self, outcome.handler_name)(outcome)
        elif not outcome and event.key in ("ctrl+c", "ctrl+q"):
            self.exit(0)

    def on_outbox_box_intent(self, message: OutboxBox.Intent) -> None:
        message.stop()
        controller = self.controller
        if message.name == "cancel" and controller.state.question is None:
            self.exit(0)
            return
        if (
            message.name == "scope"
            and self._container is not None
            and controller.state.abilities.scope
        ):
            controller.container = self._container if controller.state.all_repos else None
        controller.intent(message.name, message.row, message.value)

    def on_overlay_box_cancelled(self, message: OverlayBox.Cancelled) -> None:
        message.stop()
        self.exit(0)

    def on_overlay_box_changed(self, message: OverlayBox.Changed) -> None:
        message.stop()  # full screen: nothing to lay out again


def run_outbox_app(
    target: RepoTarget, *, container: str | None, all_repos: bool, read_only: bool
) -> int:
    """Run the app on the terminal; its exit status."""
    app = OutboxApp(target, container=container, all_repos=all_repos, read_only=read_only)
    return app.run() or 0
