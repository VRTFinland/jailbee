"""Pilot harness for the Textual dashboard: one call per scripted session.

`drive()` starts a real `DashboardApp` headless against a `FakeStateClient`,
applies ``steps`` one by one, and records the session's view after each — the
same indexing the old `render` call list had (state before the first key,
then after every key that did not quit).
"""

from __future__ import annotations

import asyncio
import io
import itertools
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from jailbee.dashboard.jobs import JobResult, JobRunner
from jailbee.dashboard.tui import app as tapp
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.frame import DashboardView, render_view
from jailbee.db.view_prefs import ViewState
from jailbee.state_service.protocol import Snapshot

_MAX_PADDING = 5  # trailing Ctrl-Cs before a run that will not quit fails


class SyncJobs(JobRunner):
    """`JobRunner` that runs the child through the (mocked) `subprocess.run`.

    The real runner spawns a `Popen` and waits on a thread; patching `Popen`
    is process-wide and breaks every other `subprocess.run`, so the tests
    assert on the one `subprocess.run` mock and get the result on the next
    `poll()`, exactly as the real runner delivers it.
    """

    def start(self, key, label, argv, cwd, on_done):  # type: ignore[no-untyped-def]  # test double
        proc = subprocess.run(argv, check=False, cwd=cwd)
        self._labels[key] = label
        stderr = proc.stderr if isinstance(proc.stderr, str) else ""
        self._finished.append((key, on_done, JobResult(proc.returncode, stderr)))


class FakeStateClient:
    """Stands in for `StateClient`: `latest()` serves the *same* groups list
    every time, so a test that mutates it changes what the next tick sees."""

    def __init__(self, groups, *, git_enabled=False, status=None, fail=None):  # type: ignore[no-untyped-def]
        self.groups = groups
        self.git_enabled = git_enabled
        self._status = status
        self.fail = fail
        self.events: list[tuple[Any, ...]] = []
        self.closed = False

    def wait_first_snapshot(self, timeout):  # type: ignore[no-untyped-def]
        self.events.append(("wait",))
        if self.fail is not None:
            raise self.fail
        return self.latest()

    def latest(self):  # type: ignore[no-untyped-def]
        return Snapshot(1, datetime(2026, 10, 4, tzinfo=UTC), self.git_enabled, self.groups)

    def status(self):  # type: ignore[no-untyped-def]
        return self._status

    def refresh(self):  # type: ignore[no-untyped-def]
        self.events.append(("refresh",))

    def set_active(self, value):  # type: ignore[no-untyped-def]
        self.events.append(("active", value))

    def close(self):  # type: ignore[no-untyped-def]
        self.closed = True


@dataclass(frozen=True)
class Resize:
    width: int
    height: int


Step = str | Resize | Callable[["tapp.DashboardApp"], object]


@dataclass
class Run:
    app: tapp.DashboardApp
    client: FakeStateClient
    trace: list[DashboardView] = field(default_factory=list)
    steps_taken: int = 0
    rc: int | None = None

    @property
    def last(self) -> DashboardView:
        return self.trace[-1]

    def overlays(self) -> list[object]:
        return [view.overlay for view in self.trace]

    def notices(self) -> list[str | None]:
        return [view.notice for view in self.trace]

    def of_type(self, kind: type) -> list[Any]:
        return [view.overlay for view in self.trace if isinstance(view.overlay, kind)]


def _patch_startup(mocker, view_state=None):  # type: ignore[no-untyped-def]
    """Patch everything `open_dashboard` touches except the state client."""
    mocker.patch.object(tsession, "_interactive", return_value=True)
    mocker.patch.object(tsession, "collect_repo_roots", return_value=[Path("/x")])
    mocker.patch("jailbee.db.get_engine", return_value=mocker.Mock())
    mocker.patch.object(tsession, "seed_view_state", return_value=view_state or ViewState())
    mocker.patch.object(tsession, "JobRunner", SyncJobs)


def start_session(
    mocker, groups=None, *, view_state=None, git_enabled=False, status=None, fail=None
):  # type: ignore[no-untyped-def]
    """Patch everything `open_dashboard` touches; return its `Startup` and the fake client."""
    _patch_startup(mocker, view_state)
    client = FakeStateClient(
        groups if groups is not None else [], git_enabled=git_enabled, status=status, fail=fail
    )
    mocker.patch.object(tsession, "open_state_client", return_value=client)
    startup = tsession.open_dashboard(None)
    return startup, client


def keys(text: str) -> list[str]:
    """One key press per character — how a typist feeds a prompt."""
    return ["space" if ch == " " else ch for ch in text]


async def _apply(pilot, app: tapp.DashboardApp, step: Step) -> None:  # type: ignore[no-untyped-def]
    if isinstance(step, bytes):
        raise TypeError(f"ported tests press key names, not legacy bytes: {step!r}")
    if isinstance(step, str):
        await pilot.press(step)
    elif isinstance(step, Resize):
        await pilot.resize_terminal(step.width, step.height)
        await pilot.pause()
    else:
        step(app)
        app.refresh_frame()
        await pilot.pause()


def drive(  # type: ignore[no-untyped-def]
    mocker,
    steps: Iterable[Step],
    groups=None,
    *,
    remote=False,
    over_ssh=False,
    ssh_policy=None,
    view_state=None,
    git_enabled=False,
    status=None,
    size=(80, 25),
    scope=None,
    cwd_root=None,
    client: FakeStateClient | None = None,
) -> Run:
    """Run a real `DashboardApp` headless through ``steps``; see the module docstring.

    Steps are key names (``"j"``, ``"enter"``, ``"ctrl+c"``), ``Resize``, or
    callables run with the app between keys (followed by a tick). After the
    steps, Ctrl-C is pressed until the app quits, as the old harness padded
    its input.
    """
    if client is None:
        startup, client = start_session(
            mocker, groups, view_state=view_state, git_enabled=git_enabled, status=status
        )
    else:
        _patch_startup(mocker, view_state)
        mocker.patch.object(tsession, "open_state_client", return_value=client)
        startup = tsession.open_dashboard(None)
    assert not isinstance(startup, int), "startup failed; use tapp.run for startup tests"
    app = tapp.DashboardApp(
        startup,
        incus=mocker.Mock(),
        cwd_root=cwd_root,
        remote=remote,
        over_ssh=over_ssh,
        ssh_policy=ssh_policy,
        scope=scope,
        tick_seconds=None,
    )
    result = Run(app, client)

    async def script() -> None:
        async with app.run_test(size=size) as pilot:
            result.trace.append(app.session.view(app.hover))
            padding = itertools.repeat("ctrl+c", _MAX_PADDING)
            for step in itertools.chain(steps, padding):
                await _apply(pilot, app, step)
                result.steps_taken += 1
                if app.quit_requested:
                    return
                result.trace.append(app.session.view(app.hover))
            raise AssertionError("the dashboard did not quit on Ctrl-C")

    asyncio.run(script())
    result.rc = app.return_value
    return result


def render_text(view: DashboardView, size: tuple[int, int] = (80, 25)) -> str:
    """``view`` as plain text, as `drive` would have drawn it at ``size``."""
    width, height = size
    console = Console(width=width, height=height, file=io.StringIO(), record=True)
    console.print(render_view(view, height=height))
    return console.export_text()


def patch_pause(mocker):  # type: ignore[no-untyped-def]
    """One mock for "press Enter to return", looked up by both dispatch and the session."""
    from jailbee.dashboard import dispatch as ddispatch

    return patch_in(mocker, "_wait_for_return", ddispatch, tsession)


def patch_in(mocker, name: str, *modules: object, **kwargs: Any):  # type: ignore[no-untyped-def]
    """Patch ``name`` in each of ``modules`` with one shared mock."""
    mock = mocker.patch.object(modules[0], name, **kwargs)
    for module in modules[1:]:
        mocker.patch.object(module, name, mock)
    return mock
