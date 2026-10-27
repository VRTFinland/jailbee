"""The standalone `jailbee outbox` app: the Outbox box full screen around a local runner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from jailbee.dashboard import accounts as da
from jailbee.dashboard.jobs import JobResult
from jailbee.dashboard.model import RepoTarget
from jailbee.dashboard.tui import outbox_app
from jailbee.dashboard.tui.outbox_app import LocalOutboxRunner, OutboxApp
from jailbee.dashboard.tui.outbox_box import OutboxBox
from tests.dashboard_pilot import screen_text
from tests.test_dashboard_outbox import _container, _listing, _proposal
from tests.test_dashboard_outbox_flow import _plan

TARGET = RepoTarget(Path("/repos/alpha"), Path("/repos/alpha/.jailbee/config.yaml"))
LISTING = _listing(_container(_proposal("pr/a.json"), _proposal("issue/b.json")))
LOADING = "reading the outbox…"
READ_ONLY_LINE = "Read-only over SSH: use explicit `jailbee outbox drop` / `outbox apply` commands."


class FakeJobs:
    """A `JobRunner` stand-in: records starts, delivers scripted results on `poll`."""

    instances: ClassVar[list[FakeJobs]] = []

    def __init__(self) -> None:
        self.started: list[list[str]] = []
        self._pending: list[tuple[Callable[[Any], None], Any]] = []
        FakeJobs.instances.append(self)

    def start(self, key, label, argv, cwd, on_done, *, capture_stdout=False):  # type: ignore[no-untyped-def]
        self.started.append(list(argv))
        stdout = _plan() if "--dry-run" in argv else "published 2 action(s)"
        self._pending.append((on_done, JobResult(0, "", stdout)))

    def call(self, key, label, fn, on_done):  # type: ignore[no-untyped-def]
        """A listing: run now, delivered on the next `poll`."""
        self._pending.append((on_done, fn()))

    def poll(self) -> None:
        pending, self._pending = self._pending, []
        for on_done, result in pending:
            on_done(result)


@pytest.fixture(autouse=True)
def repos(mocker):
    """Every container belongs to the app's repo unless a test says otherwise."""
    owners: dict[str, RepoTarget | None] = {}
    mocker.patch.object(
        outbox_app, "container_repo", side_effect=lambda target, name: owners.get(name, target)
    )
    return owners


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
            await listed(app, pilot)
            result.append(await scenario(pilot))

    asyncio.run(main())
    return result[0]


async def listed(app: OutboxApp, pilot) -> None:  # type: ignore[no-untyped-def]
    """Wait for the background listing (it arrives on the app's poll timer)."""
    for _ in range(100):
        if app.controller.state.busy != LOADING:
            return
        await pilot.pause(0.05)
    raise AssertionError("the outbox listing never arrived")


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
        await listed(app, pilot)
        await pilot.press("a")
        await listed(app, pilot)

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
    assert runner.hand_off(["pr", "push", "alpha-x"]) == 3
    assert seen == [True]
    fg.assert_called_once_with(TARGET, ["pr", "push", "alpha-x"], style="output")


def test_read_only_scope_key_does_nothing(quiet):
    app = make(read_only=True)

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        await pilot.press("a")
        await pilot.pause()
        return screen_text(app)

    text = drive(app, scenario, size=(160, 30))
    assert app.controller.state.abilities.scope is False
    assert len(quiet) == 1 and "--all-repos" not in quiet[0]
    assert "a all repos" not in text


@pytest.mark.parametrize(
    "argv",
    [
        ["outbox", "ls", "--all-repos", "-o", "json"],
        ["outbox", "drop", "alpha-x", "pr/a.json", "--revision", "r1", "--yes"],
        ["outbox", "apply", "alpha-x", "pr/a.json", "--revision", "r1", "-y"],
        ["outbox", "clear", "alpha-x", "--prepare-snapshot"],
        ["pr", "push", "alpha-x"],
    ],
)
def test_read_only_runner_refuses_what_it_does_not_permit(mocker, argv):
    quiet = mocker.patch.object(da, "run_cli_quiet")
    fg = mocker.patch.object(outbox_app, "_run_cli_foreground")
    suspend = mocker.Mock()
    runner = LocalOutboxRunner(TARGET, read_only=True, suspend=suspend)
    runner.jobs = mocker.Mock()
    assert runner.permitted(argv) is False

    result = runner.run_quiet(argv)
    assert result.ok is False and "read-only" in result.message
    with pytest.raises(ValueError, match="read-only"):
        runner.start("k", "label", argv, lambda _r: None)
    assert runner.hand_off(argv) is None

    quiet.assert_not_called()
    runner.jobs.start.assert_not_called()
    suspend.assert_not_called()
    fg.assert_not_called()


def test_scope_after_failed_first_listing_keeps_the_named_container(mocker):
    calls: list[list[str]] = []
    results = [da.CliResult(False, "exited 2", "not json")]

    def run(argv, *, cwd, timeout=60.0):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return results.pop(0) if results else da.CliResult(True, "done", LISTING)

    mocker.patch.object(da, "run_cli_quiet", side_effect=run)
    app = make(container="feature")

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        assert app.controller.state.abilities.scope is False
        await pilot.press("a")  # the box eats it: no scope ability
        await pilot.pause()
        # A scope intent reaching the app anyway (a click, a future key) must not
        # narrow/widen the listing behind the controller's back.
        app.on_outbox_box_intent(OutboxBox.Intent(app.box.key, "scope", None))
        await pilot.press("r")
        await pilot.pause()

    drive(app, scenario)
    assert app.controller.container == "feature"
    assert app.controller.state.all_repos is False
    assert len(calls) >= 2
    for argv in calls[1:]:
        assert argv[:3] == ["outbox", "ls", "feature"] and "--all-repos" not in argv


BETA = RepoTarget(Path("/repos/beta"), Path("/repos/beta/.jailbee/config.yaml"))


def _awaiting_listing() -> str:
    awaiting = _proposal("pr/d.json", state="awaiting-pr", create_scope={"kind": "repo"})
    return _listing(_container(_proposal("pr/a.json")), _container(awaiting, name="beta-one"))


def test_create_pr_in_all_repos_view_is_addressed_at_the_containers_repo(mocker, repos):
    repos["beta-one"] = BETA
    mocker.patch.object(
        da, "run_cli_quiet", return_value=da.CliResult(True, "done", _awaiting_listing())
    )
    handed = []

    def foreground(target, argv, *, style):  # type: ignore[no-untyped-def]
        handed.append((target, argv, target.flags(), target.cwd()))
        return 0

    mocker.patch.object(outbox_app, "_run_cli_foreground", side_effect=foreground)
    app = OutboxApp(TARGET, container=None, all_repos=True, read_only=False)

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        await pilot.press("down", "c")  # the beta-one proposal
        await pilot.pause()

    drive(app, scenario, size=(160, 30))
    assert handed == [
        (BETA, ["pr", "push", "beta-one"], ["--config", "/repos/beta/.jailbee/config.yaml"],
         Path("/repos/beta")),
    ]  # fmt: skip


def test_create_pr_refused_when_the_containers_repo_is_unknown(mocker, repos):
    repos["beta-one"] = None
    mocker.patch.object(
        da, "run_cli_quiet", return_value=da.CliResult(True, "done", _awaiting_listing())
    )
    fg = mocker.patch.object(outbox_app, "_run_cli_foreground")
    app = OutboxApp(TARGET, container=None, all_repos=True, read_only=False)

    async def scenario(pilot):  # type: ignore[no-untyped-def]
        await pilot.press("down", "c")
        await pilot.pause()

    drive(app, scenario, size=(160, 30))
    fg.assert_not_called()
    assert app.controller.state.status[-1] == "beta-one: creating a PR is not available here"
    runner = app.runner
    assert runner.permitted(["pr", "push", "beta-one"]) is False
    assert runner.hand_off(["pr", "push", "beta-one"]) is None


def test_container_repo_is_the_resolved_owner(mocker, tmp_path):
    mocker.stopall()  # the autouse stand-in: test the real lookup
    own = mocker.Mock(repo_root=tmp_path)
    (tmp_path / ".jailbee").mkdir()
    (tmp_path / ".jailbee" / "config.yaml").write_text("{}\n")
    cfg = mocker.Mock()
    load = mocker.patch("jailbee.config.load_config", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    resolve = mocker.patch("jailbee.outbox.commands.resolve_target", return_value=(own, "b-one"))
    assert outbox_app.container_repo(TARGET, "b-one") == RepoTarget(
        tmp_path, tmp_path / ".jailbee" / "config.yaml"
    )
    load.assert_called_once_with(TARGET.config_path)
    assert resolve.call_args.args[0] is cfg and resolve.call_args.args[2] == "b-one"

    from jailbee.outbox.models import OutboxError

    resolve.side_effect = OutboxError("no such container")
    assert outbox_app.container_repo(TARGET, "b-one") is None
