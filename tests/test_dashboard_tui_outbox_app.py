"""The standalone `jailbee outbox` app: the Outbox box full screen around a local runner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from jailbee.dashboard import accounts as da
from jailbee.dashboard.jobs import JobResult
from jailbee.dashboard.model import RepoTarget
from jailbee.dashboard.tui import outbox_app
from jailbee.dashboard.tui.outbox_app import LocalOutboxRunner, OutboxApp
from tests.dashboard_pilot import screen_text
from tests.test_dashboard_outbox import _container, _listing, _proposal
from tests.test_dashboard_outbox_flow import _plan

TARGET = RepoTarget(Path("/repos/alpha"), Path("/repos/alpha/.jailbee/config.yaml"))
LISTING = _listing(_container(_proposal("pr/a.json"), _proposal("issue/b.json")))
READ_ONLY_LINE = "Read-only over SSH: use explicit `jailbee outbox drop` / `outbox apply` commands."


class FakeJobs:
    """A `JobRunner` stand-in: records starts, delivers scripted results on `poll`."""

    instances: list[FakeJobs] = []

    def __init__(self) -> None:
        self.started: list[list[str]] = []
        self._pending: list[tuple[Callable[[JobResult], None], JobResult]] = []
        FakeJobs.instances.append(self)

    def start(self, key, label, argv, cwd, on_done, *, capture_stdout=False):  # type: ignore[no-untyped-def]
        self.started.append(list(argv))
        stdout = _plan() if "--dry-run" in argv else "published 2 action(s)"
        self._pending.append((on_done, JobResult(0, "", stdout)))

    def poll(self) -> None:
        pending, self._pending = self._pending, []
        for on_done, result in pending:
            on_done(result)


@pytest.fixture
def quiet(mocker):
    calls: list[list[str]] = []

    def run(argv, *, cwd, timeout=60.0):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return da.CliResult(True, "done", LISTING)

    mocker.patch.object(da, "run_cli_quiet", side_effect=run)
    return calls


@pytest.fixture
def jobs(mocker):
    FakeJobs.instances = []
    mocker.patch.object(outbox_app, "JobRunner", FakeJobs)
    return FakeJobs.instances


def drive(app: OutboxApp, scenario, size=(100, 30)):  # type: ignore[no-untyped-def]
    result = []

    async def main() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.pause()
            result.append(await scenario(pilot))

    asyncio.run(main())
    return result[0]


def make(read_only: bool = False, container: str | None = None) -> OutboxApp:
    return OutboxApp(TARGET, container=container, all_repos=False, read_only=read_only)


def test_app_lists_and_quits(quiet):
    app = make()

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        text = screen_text(app)
        await pilot.press("q")
        await pilot.pause()
        return text

    text = drive(app, scenario)
    assert "PR  o/r#42" in text and "ISS o/r#42" in text
    assert app.return_value == 0
    assert quiet[0] == [
        "outbox", "ls", "-o", "json", "--config", "/repos/alpha/.jailbee/config.yaml",
    ]  # fmt: skip
    assert READ_ONLY_LINE not in text


def test_read_only_hides_mutations(quiet, jobs):
    app = make(read_only=True)

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        for key in ("d", "p", "X"):
            await pilot.press(key)
            await pilot.pause()
        return screen_text(app)

    text = drive(app, scenario)
    assert all(argv[1] == "ls" for argv in quiet) and len(quiet) == 1
    assert all(not j.started for j in jobs)
    assert READ_ONLY_LINE in text
    assert "d delete" not in text and "p publish" not in text and "X clear" not in text
    assert "q quit" in text
    ab = app.controller.state.abilities
    assert not (ab.delete or ab.publish or ab.publish_hand_off or ab.create_pr or ab.clear)


def test_runner_permits_only_ls_when_read_only():
    runner = LocalOutboxRunner(TARGET, read_only=True, suspend=lambda fn: fn())
    assert runner.permitted(["outbox", "ls", "-o", "json"])
    assert not runner.permitted(["outbox", "drop", "x", "pr/a.json", "--revision", "r"])
    writable = LocalOutboxRunner(TARGET, read_only=False, suspend=lambda fn: fn())
    assert writable.permitted(["outbox", "apply", "x", "pr/a.json", "-y"])


def test_scope_toggle_reloads_with_all_repos(quiet):
    app = make()

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        await pilot.press("a")
        await pilot.pause()
        return screen_text(app)

    text = drive(app, scenario, size=(160, 30))
    assert "--all-repos" not in quiet[0]
    assert "--all-repos" in quiet[-1]
    assert "a all repos" in text


def test_scope_toggle_with_a_named_container_lists_every_repo(quiet):
    app = make(container="feature")

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()

    drive(app, scenario)
    assert quiet[0][:3] == ["outbox", "ls", "feature"]
    assert quiet[1][:2] == ["outbox", "ls"] and "feature" not in quiet[1]
    assert "--all-repos" in quiet[1]
    assert quiet[2][:3] == ["outbox", "ls", "feature"] and "--all-repos" not in quiet[2]


def test_publish_job_polled_and_status_shown(quiet, jobs):
    app = make()

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        await pilot.press("p")
        await pilot.pause(0.4)  # the poll interval delivers the plan
        asked = app.controller.state.question
        await pilot.press("up", "up", "enter")  # Cancel is the default; Publish is first
        await pilot.pause(0.4)
        return asked, screen_text(app)

    asked, text = drive(app, scenario)
    assert asked is not None and asked.purpose == "publish"
    (runner_jobs,) = jobs
    plan_argv, apply_argv = runner_jobs.started
    assert plan_argv[0] == "jailbee" and "--dry-run" in plan_argv
    assert "--config" in plan_argv
    assert apply_argv[:4] == ["jailbee", "outbox", "apply", "alpha-x"]
    assert "-y" in apply_argv and "--dry-run" not in apply_argv
    assert "published 2 action(s)" in text
    assert app.controller.state.question is None


def test_hand_off_runs_in_the_foreground_through_suspend(mocker):
    fg = mocker.patch.object(outbox_app, "_run_cli_foreground", return_value=3)
    seen = []

    def suspend(fn):  # type: ignore[no-untyped-def]
        seen.append(True)
        return fn()

    runner = LocalOutboxRunner(TARGET, read_only=False, suspend=suspend)
    assert runner.hand_off(["pr", "alpha-x"]) == 3
    assert seen == [True]
    fg.assert_called_once_with(TARGET, ["pr", "alpha-x"], style="output")
