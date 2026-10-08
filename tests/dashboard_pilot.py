"""Pilot harness for the Textual dashboard: one call per scripted session.

`drive()` starts a real `DashboardApp` headless against a `FakeStateClient`,
applies ``steps`` one by one, and records the session's view after each — the
same indexing the old `render` call list had (state before the first key,
then after every key that did not quit).
"""

from __future__ import annotations

import asyncio
import itertools
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from textual import _wait as textual_wait
from textual import events
from textual.app import App, ComposeResult

from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.hit import Hit
from jailbee.dashboard.jobs import JobResult, JobRunner
from jailbee.dashboard.tui import app as tapp
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.frame import DashboardView
from jailbee.db.view_prefs import ViewState
from jailbee.global_config import DashboardConfig, GlobalConfig
from jailbee.state_service.protocol import Snapshot

_MAX_PADDING = 5  # trailing Ctrl-Cs before a run that will not quit fails
_MAX_SETTLE_PAUSES = 20  # pauses before a resize whose layout keeps changing fails
# Pilot's wait_for_idle sleeps this long per poll (20 ms by default, twice per
# key press); the session is synchronous, so a poll of 1 ms is as deterministic.
_SLEEP_GRANULARITY = 0.001


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


@dataclass(frozen=True)
class Click:
    hit: Hit
    times: int = 1
    button: int = 1


@dataclass(frozen=True)
class Wheel:
    step: int
    shift: bool = False
    horizontal: bool = False
    at: str = "#fleet"
    ctrl: bool = False


@dataclass(frozen=True)
class Paste:
    """A bracketed paste: the terminal's one chunk, delivered as Textual's `events.Paste`."""

    text: str


Step = str | Resize | Click | Wheel | Paste | Callable[["tapp.DashboardApp"], object]


def hit_offset(app: tapp.DashboardApp, hit: Hit) -> tuple[int, int]:
    """The first screen cell whose style carries ``hit``."""
    width, height = app.size
    for y in range(height):
        for x in range(width):
            if Hit.of(app.screen.get_style_at(x, y).meta) == hit:
                return x, y
    raise AssertionError(f"{hit} is not on screen")


@dataclass
class Run:
    app: tapp.DashboardApp
    client: FakeStateClient
    trace: list[DashboardView] = field(default_factory=list)
    screens: list[str] = field(default_factory=list)
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


def _patch_startup(mocker, view_state=None, mouse=True, jobs=SyncJobs):  # type: ignore[no-untyped-def]
    """Patch everything `open_dashboard` touches except the state client."""
    mocker.patch.object(
        tsession,
        "global_config_or_defaults",
        return_value=GlobalConfig(dashboard=DashboardConfig(mouse=mouse)),
    )
    mocker.patch.object(tsession, "_interactive", return_value=True)
    mocker.patch.object(tsession, "collect_repo_roots", return_value=[Path("/x")])
    mocker.patch("jailbee.db.get_engine", return_value=mocker.Mock())
    mocker.patch.object(tsession, "seed_view_state", return_value=view_state or ViewState())
    mocker.patch.object(tsession, "JobRunner", jobs)


def start_session(
    mocker,
    groups=None,
    *,
    view_state=None,
    git_enabled=False,
    status=None,
    fail=None,
    mouse=True,
    jobs=SyncJobs,
):  # type: ignore[no-untyped-def]
    """Patch everything `open_dashboard` touches; return its `Startup` and the fake client."""
    _patch_startup(mocker, view_state, mouse, jobs)
    client = FakeStateClient(
        groups if groups is not None else [], git_enabled=git_enabled, status=status, fail=fail
    )
    mocker.patch.object(tsession, "open_state_client", return_value=client)
    startup = tsession.open_dashboard(None)
    return startup, client


def keys(text: str) -> list[str]:
    """One key press per character — how a typist feeds a prompt."""
    return ["space" if ch == " " else ch for ch in text]


def _in_flight(app: tapp.DashboardApp) -> bool:
    """Whether a resize, layout, message or after-refresh callback is still pending."""
    screen = app.screen
    return bool(
        app._resize_event is not None
        or screen._layout_required
        or screen._callbacks
        or any(
            node.message_queue_size or node._next_callbacks
            for node in (app, *screen.walk_children(with_self=True))
        )
    )


async def _settle_resize(pilot, app: tapp.DashboardApp) -> None:  # type: ignore[no-untyped-def]
    """Lay the resized screen out before the next step, as a person sees it.

    Textual 8.2.8 debounces a resize: `App._on_resize` only arms a 1/120 s
    timer whose `_check_resize` hands the Resize to the screen, and Pilot's
    pause waits for messages, never timers. With the 1 ms idle poll the next
    key could reach the session before the table was laid out at the new
    width. Hand the Resize over now (the timer then finds nothing to do), then
    pause until nothing is in flight: the layout, the widgets' Resize
    messages, and the reclamps they trigger.
    """
    app._check_resize()
    for _ in range(_MAX_SETTLE_PAUSES):
        await pilot.pause()
        if not _in_flight(app):
            return
    raise AssertionError("the resized layout did not settle")


async def _apply(pilot, app: tapp.DashboardApp, step: Step) -> None:  # type: ignore[no-untyped-def]
    if isinstance(step, bytes):
        raise TypeError(f"ported tests press key names, not legacy bytes: {step!r}")
    if isinstance(step, str):
        await pilot.press(step)
        await pilot.pause()
    elif isinstance(step, Resize):
        await pilot.resize_terminal(step.width, step.height)
        await _settle_resize(pilot, app)
    elif isinstance(step, Paste):
        app.post_message(events.Paste(step.text))
        await pilot.pause()
    elif isinstance(step, Click):
        await pilot.click(offset=hit_offset(app, step.hit), times=step.times, button=step.button)
    elif isinstance(step, Wheel):
        if step.horizontal:
            event = events.MouseScrollRight if step.step > 0 else events.MouseScrollLeft
        else:
            event = events.MouseScrollDown if step.step > 0 else events.MouseScrollUp
        # Pilot has no public wheel helper in Textual 8.2.8.
        await pilot._post_mouse_events(
            [event], widget=step.at, offset=(1, 1), shift=step.shift, control=step.ctrl
        )
        await pilot.pause()
    else:
        step(app)
        app.refresh_frame()
        await pilot.pause()


def make_app(  # type: ignore[no-untyped-def]
    mocker,
    groups=None,
    *,
    remote=False,
    over_ssh=False,
    ssh_policy=None,
    view_state=None,
    git_enabled=False,
    status=None,
    scope=None,
    cwd_root=None,
    client: FakeStateClient | None = None,
    mouse: bool = True,
    jobs: type[JobRunner] = SyncJobs,
) -> tapp.DashboardApp:
    """Build the real app with the same isolated startup for pilots and snapshots."""
    # Read at call time by `wait_for_idle(0)`, which `pilot.press`/`pause` use.
    mocker.patch.object(textual_wait, "SLEEP_GRANULARITY", _SLEEP_GRANULARITY)
    if client is None:
        startup, client = start_session(
            mocker,
            groups,
            view_state=view_state,
            git_enabled=git_enabled,
            status=status,
            mouse=mouse,
            jobs=jobs,
        )
    else:
        _patch_startup(mocker, view_state, mouse, jobs)
        mocker.patch.object(tsession, "open_state_client", return_value=client)
        startup = tsession.open_dashboard(None)
    assert not isinstance(startup, int), "startup failed; use tapp.run for startup tests"
    return tapp.DashboardApp(
        startup,
        incus=mocker.Mock(),
        cwd_root=cwd_root,
        remote=remote,
        over_ssh=over_ssh,
        ssh_policy=ssh_policy,
        scope=scope,
        tick_seconds=None,
    )


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
    mouse: bool = True,
    jobs: type[JobRunner] = SyncJobs,
    screens: bool = False,
) -> Run:
    """Run a real `DashboardApp` headless through ``steps``; see the module docstring.

    Steps are key names (``"j"``, ``"enter"``, ``"ctrl+c"``), ``Resize``, or
    callables run with the app between keys (followed by a tick). After the
    steps, Ctrl-C is pressed until the app quits, as the old harness padded
    its input.
    """
    app = make_app(
        mocker,
        groups,
        remote=remote,
        over_ssh=over_ssh,
        ssh_policy=ssh_policy,
        view_state=view_state,
        git_enabled=git_enabled,
        status=status,
        scope=scope,
        cwd_root=cwd_root,
        client=client,
        mouse=mouse,
        jobs=jobs,
    )
    client = app.session.client
    result = Run(app, client)

    async def script() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            result.trace.append(app.session.view(app.hover))
            if screens:
                result.screens.append(screen_text(app))
            padding = itertools.repeat("ctrl+c", _MAX_PADDING)
            for step in itertools.chain(steps, padding):
                await _apply(pilot, app, step)
                result.steps_taken += 1
                if app.quit_requested:
                    return
                result.trace.append(app.session.view(app.hover))
                if screens:
                    result.screens.append(screen_text(app))
            raise AssertionError("the dashboard did not quit on Ctrl-C")

    asyncio.run(script())
    result.rc = app.return_value
    return result


class BareClient:
    """A state client for a session with no frontend at all."""

    def __init__(self, groups):  # type: ignore[no-untyped-def]
        self.groups = groups
        self.events: list[tuple[Any, ...]] = []

    def latest(self):  # type: ignore[no-untyped-def]
        return Snapshot(1, datetime(2026, 10, 7, tzinfo=UTC), False, self.groups)

    def status(self):  # type: ignore[no-untyped-def]
        return None

    def refresh(self):  # type: ignore[no-untyped-def]
        self.events.append(("refresh",))

    def set_active(self, value):  # type: ignore[no-untyped-def]
        self.events.append(("active", value))


class BareTerminal:
    table_width = 116

    def __init__(self) -> None:
        self.handed: list[object] = []

    def hand_off(self, fn):  # type: ignore[no-untyped-def]
        self.handed.append(fn)
        return fn()


def bare_session(mocker, groups, **kw):  # type: ignore[no-untyped-def]
    """A `DashboardSession` over a `BareClient` and `BareTerminal`, ticked once."""
    mocker.patch.object(tsession, "save_view_state")
    startup = tsession.Startup(mocker.Mock(), BareClient(groups), ViewState(), None)  # type: ignore[arg-type]  # duck-typed client
    terminal = BareTerminal()
    session = tsession.DashboardSession(
        startup, incus=mocker.Mock(), cwd_root=None, terminal=terminal, **kw
    )
    session.tick()
    return session, terminal


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


def container_egress_keys(group: dmodel.RepoGroup, **menu_kwargs: Any) -> list[str]:
    """Keys that open the first container's Egress panel from the dashboard.

    ``menu_kwargs`` (``remote``/``over_ssh``/``ssh_policy``) must match the
    ``drive()`` call: the menu an SSH session sees has other entries.
    """
    menu = tmenu.open_menu([group], group.containers[0].name, **menu_kwargs)
    assert menu is not None
    root = tmenu._menu_entries(menu)
    network_index = next(
        i
        for i, item in enumerate(root)
        if isinstance(item, dmenus.MenuGroup) and item.label == "Network →"
    )
    network = root[network_index]
    assert isinstance(network, dmenus.MenuGroup)
    egress_index = next(i for i, (_, verb) in enumerate(network.actions) if verb == "net egress ls")
    return ["j", "enter", *["j"] * network_index, "enter", *["j"] * egress_index, "enter"]


def repo_menu_keys(group: dmodel.RepoGroup, verb: str, **menu_kwargs: Any) -> list[str]:
    """Keys that choose repo-menu ``verb`` from the first row (the repo header).

    Finds a top-level leaf or one inside a submenu, so no test counts entries.
    ``menu_kwargs`` (``ssh_policy``/``over_ssh``) must match the ``drive()`` call.
    """
    menu = tmenu.open_repo_menu([group], group.prefix, frozenset(), **menu_kwargs)
    assert menu is not None
    for i, item in enumerate(menu.actions):
        if isinstance(item, dmenus.MenuGroup):
            leaves = [leaf_verb for _label, leaf_verb in item.actions]
            if verb in leaves:
                return ["enter", *["j"] * i, "enter", *["j"] * leaves.index(verb), "enter"]
        elif item[1] == verb:
            return ["enter", *["j"] * i, "enter"]
    raise AssertionError(f"{verb!r} is not in the repo menu")


def container_menu_keys(group: dmodel.RepoGroup, verb: str, **menu_kwargs: Any) -> list[str]:
    """Keys that choose top-level container-menu leaf ``verb`` for the first container.

    ``menu_kwargs`` (``remote``/``over_ssh``/``ssh_policy``) must match the ``drive()`` call.
    """
    menu = tmenu.open_menu([group], group.containers[0].name, **menu_kwargs)
    assert menu is not None
    entries = list(tmenu._menu_entries(menu))
    at = next(
        i
        for i, entry in enumerate(entries)
        if not isinstance(entry, dmenus.MenuGroup) and entry[1] == verb
    )
    return ["j", "enter", *["j"] * at, "enter"]


# repo header → Enter (menu) → past New container…, New from PR… → Enter
OPEN_REPO_GROUP_PICKER = ["enter", "j", "j", "enter"]

CREDENTIAL_GROUP_LEAF = ("Credential group…", "credential-group")


def open_container_group_picker(group: dmodel.RepoGroup, **menu_kwargs: Any) -> list[str]:
    """Keys that open the first container's credential-group picker.

    ``menu_kwargs`` must match the ``drive()`` call, as in ``container_egress_keys``.
    """
    menu = tmenu.open_menu([group], group.containers[0].name, **menu_kwargs)
    assert menu is not None
    at = list(tmenu._menu_entries(menu)).index(CREDENTIAL_GROUP_LEAF)
    return ["j", "enter", *["j"] * at, "enter"]


FROZEN_NOW = datetime(2026, 10, 8, 12, 0, 5, tzinfo=UTC)


def view_of(groups, **overrides: Any) -> DashboardView:  # type: ignore[no-untyped-def]  # test utility
    base: dict[str, Any] = dict(
        groups=groups,
        selected=None,
        now=FROZEN_NOW,
        git_enabled=True,
        enabled=None,
        overlay=None,
        notice=None,
        folded=frozenset(),
        column_offset=0,
        hidden_by_preferences=False,
        show_details=False,
        column_widths=None,
        shown_columns=None,
        hover=None,
    )
    base.update(overrides)
    return DashboardView(**base)


def screen_text(app: App) -> str:
    # Textual 8.2.8's only whole-screen export lives on the private compositor.
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


class _FrameHost(App[None]):
    CSS = "Screen { background: ansi_default; }"

    def __init__(self, view: DashboardView) -> None:
        super().__init__(ansi_color=True)
        self.view = view

    def compose(self) -> ComposeResult:
        from jailbee.dashboard.tui.widgets import DashboardFrame

        yield DashboardFrame(id="frame")

    def on_mount(self) -> None:
        from jailbee.dashboard.tui.widgets import DashboardFrame

        self.query_one(DashboardFrame).show(self.view)


def paint(view: DashboardView, size: tuple[int, int] = (80, 25)) -> list[str]:
    """Draw via real widgets without a corrective second show after layout."""
    lines: list[str] = []

    async def main() -> None:
        app = _FrameHost(view)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            lines.extend(screen_text(app).splitlines())

    asyncio.run(main())
    while lines and not lines[-1].strip():
        lines.pop()
    return lines
