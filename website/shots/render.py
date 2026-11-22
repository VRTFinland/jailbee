"""Drive the terminal dashboard from a fixture world and write SVG (+ PNG) shots.

No Incus and no state service: the real `DashboardApp` runs headless against the
test suite's `FakeStateClient` (``tests/dashboard_pilot.py``), and everything
that would shell out is answered from the fixture or stubbed at the existing
seams (the quiet CLI runner, the action runner).
"""

from __future__ import annotations

import asyncio
import io
import re
import subprocess
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from rich.console import Console
from rich.terminal_theme import TerminalTheme

from jailbee.dashboard import accounts as da
from jailbee.dashboard import model as dmodel
from jailbee.dashboard import outbox as dob
from jailbee.dashboard.columns import COLUMNS_VERSION
from jailbee.dashboard.runner import RunResult
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from jailbee.db.view_prefs import ViewState
from tests import dashboard_pilot as pilot_support
from website.shots.fixture import World, accounts_listing, outbox_listing

CHROME = Path("/opt/google/chrome/chrome")
DEFAULT_NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
TITLE = "jailbee dashboard"
_FONT_FACE = re.compile(r"@font-face\s*\{.*?\}", re.S)
_FONT_FAMILY = re.compile(r"font-family:[^;]*;")
FONT = '"DejaVu Sans Mono", "Noto Sans Mono", Menlo, Consolas, monospace'


class _Patch:
    """``mocker.patch(target, ...)`` and ``mocker.patch.object(obj, name, ...)``."""

    def __init__(self, stack: ExitStack) -> None:
        self._stack = stack

    def __call__(self, target: str, *args: Any, **kwargs: Any) -> Any:
        return self._stack.enter_context(mock.patch(target, *args, **kwargs))

    def object(self, target: object, attribute: str, *args: Any, **kwargs: Any) -> Any:
        return self._stack.enter_context(mock.patch.object(target, attribute, *args, **kwargs))


class Patches:
    """The slice of pytest-mock's ``mocker`` that `dashboard_pilot` uses, without pytest."""

    def __init__(self) -> None:
        self._stack = ExitStack()
        self.patch = _Patch(self._stack)
        self.Mock = mock.Mock

    def __enter__(self) -> Patches:
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()


def _quiet_cli(world: World) -> Any:
    """Stands in for `accounts.run_cli_quiet`: answers the listings from the fixture."""

    def run(argv: Any, **_kwargs: Any) -> da.CliResult:
        words = list(argv)
        if words[:2] == ["account", "ls"]:
            return da.CliResult(True, "", accounts_listing(world))
        if "outbox" in words and "ls" in words:
            for name in world.outbox:
                if name in words:
                    return da.CliResult(True, "", outbox_listing(world, name))
            return da.CliResult(True, "", outbox_listing(world, ""))
        return da.CliResult(True, "Done.")

    return run


def _menu_keys(world: World, container: str, verb: str) -> list[str]:
    """Keys that choose top-level or submenu leaf ``verb`` of ``container``'s menu."""
    menu = tmenu.open_menu(world.groups, container)
    if menu is None:
        raise ValueError(f"no menu for {container!r}")
    for at, entry in enumerate(tmenu.menu_entries(menu)):
        prefix = ["enter", *["j"] * at, "enter"]
        if hasattr(entry, "actions"):
            for child_at, (_, child_verb) in enumerate(entry.actions):
                if child_verb == verb:
                    return [*prefix, *["j"] * child_at, "enter"]
        elif entry[1] == verb:
            return prefix
    raise ValueError(f"{container!r}'s menu has no {verb!r}")


def _full_name(world: World, name: str) -> str:
    names = [c.name for g in world.groups for c in g.containers]
    matches = [n for n in names if n == name or n.endswith(f"-{name}")]
    if len(matches) != 1:
        raise ValueError(f"{name!r} names {len(matches)} containers")
    return matches[0]


def export_svg(app: Any, title: str = TITLE) -> str:
    """The screen as SVG in the app's own ANSI theme, with no webfont from a CDN."""
    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    update = app.screen._compositor.render_update(
        full=True, screen_stack=app.app._background_screens, simplify=False
    )
    console.print(update)
    theme: TerminalTheme = app.ansi_theme
    svg = console.export_svg(title=title, theme=theme)
    svg = _FONT_FACE.sub("", svg)
    return _FONT_FAMILY.sub(f"font-family: {FONT};", svg)


async def _run(world: World, out_dir: Path) -> list[Path]:
    written: list[Path] = []
    client = pilot_support.FakeStateClient(world.groups, git_enabled=True)
    with Patches() as mocker:
        mocker.patch.object(tsession, "_now", return_value=(world.now or DEFAULT_NOW))
        mocker.patch.object(da, "run_cli_quiet", side_effect=_quiet_cli(world))
        mocker.patch.object(dob, "read_sent", return_value=())
        app = pilot_support.make_app(mocker, client=client, view_state=_view_state(world))
        # Nothing a fixture does may run a real `jailbee`: actions are swallowed.
        app.action_runner = _no_action
        async with app.run_test(size=world.size) as pilot:
            await pilot.pause()
            # The headless driver leaves Rich's console at its default width, which crops
            # the table's lines; a real terminal resizes it.
            app.console.size = world.size
            # The table lays out for the width the app last saw resized; say it once.
            await pilot_support._apply(pilot, app, pilot_support.Resize(*world.size))
            # `O`: fit the column widths to the width just laid out, as a user would.
            await pilot_support._apply(pilot, app, "O")
            for kind, value in world.steps:
                if kind == "shot":
                    await pilot_support._apply(pilot, app, _redraw)  # a fresh frame
                    path = out_dir / f"{value}.svg"
                    path.write_text(export_svg(app), encoding="utf-8")
                    written.append(path)
                elif kind == "queue":
                    _seed_queue(app, world)
                    await pilot.pause()
                elif kind == "select":
                    row = dmodel.Row("container", _full_name(world, value))
                    await pilot_support._apply(pilot, app, _select(row))
                elif kind == "menu":
                    container = app.session.selected.key if app.session.selected else ""
                    for key in _menu_keys(world, container, value):
                        await pilot_support._apply(pilot, app, key)
                else:
                    for key in [value] if kind == "key" else value:
                        await pilot_support._apply(pilot, app, key)
    return written


def _seed_queue(app: Any, world: World) -> None:
    """Put the fixture's queue entries in the session's queue, as if those actions had run."""
    queue = app.session.queue
    for item in world.queue:
        entry_id = queue.start(f"jailbee {item['command']}", item["target"])
        output = str(item.get("output", ""))
        if item["state"] == "running":
            queue.append(entry_id, output)
        else:
            default = 0 if item["state"] == "done" else 1
            queue.finish(entry_id, int(item.get("status", default)), output)
    app.session._queue_changed()


def _no_action(argv: Any, **_kwargs: Any) -> Any:
    return RunResult(status=0)


def _redraw(app: Any) -> None:
    """A step that only asks for the frame to be drawn again."""
    app.frame.table._strips.clear()
    app.frame.table.refresh(layout=True)


def _view_state(world: World) -> ViewState:
    view = dict(world.view)
    if "columns" in view:
        view["columns"] = tuple(view["columns"])
        view["columns_version"] = COLUMNS_VERSION
    return ViewState(**view)


def _select(row: dmodel.Row) -> Any:
    def step(app: Any) -> None:
        app.session.selected = row

    return step


def render_world(world: World, out_dir: Path) -> list[Path]:
    """Run the fixture's steps; every ``shot`` step writes ``<name>.svg`` in ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_run(world, out_dir))


def svg_size(svg: str) -> tuple[int, int]:
    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    if match is None:
        raise ValueError("SVG has no viewBox")
    return int(float(match.group(1))), int(float(match.group(2)))


def svg_to_png(svg_path: Path, png_path: Path, chrome: Path = CHROME) -> None:
    """Rasterise with headless Chrome (ImageMagick drops the SVG's unfilled strokes)."""
    if not chrome.exists():
        raise RuntimeError(f"{chrome} not found: PNG output needs the host Chrome (or --no-png)")
    width, height = svg_size(svg_path.read_text(encoding="utf-8"))
    png_path.unlink(missing_ok=True)  # a stale PNG must not pass for this run's
    with tempfile.TemporaryDirectory() as profile:
        proc = subprocess.run(
            [
                str(chrome),
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--user-data-dir={profile}",
                "--force-device-scale-factor=1",
                f"--window-size={width},{height}",
                f"--screenshot={png_path}",
                svg_path.resolve().as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if proc.returncode != 0 or not png_path.exists():
        raise RuntimeError(f"chrome could not render {svg_path.name}: {proc.stderr.strip()[-300:]}")
