"""DashboardSession without any frontend: the Terminal seam and the view."""

from __future__ import annotations

import pytest

from jailbee import memory_limit
from jailbee.dashboard import columns as dcolumns
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.overlays import Picker, PickerEntry, TextPrompt, validate_answer
from jailbee.dashboard.system import SystemStatus
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import MenuState
from jailbee.sysstat import MemoryStatus
from tests.dashboard_fixtures import ci, wide_group
from tests.dashboard_pilot import bare_session, make_app


def test_keys_move_and_open_a_menu_without_any_terminal(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    session, _ = bare_session(mocker, [group])
    assert session.selected == dmodel.Row("repo", "alpha")
    session.handle_key("down")
    session.tick()
    assert session.selected == dmodel.Row("container", "alpha-one")
    session.handle_key("enter")
    assert isinstance(session.overlay, MenuState)


def test_quit_is_returned_not_raised(mocker, tmp_path):
    session, _ = bare_session(mocker, [])
    assert session.handle_key("quit") == "quit"
    assert session.handle_key("interrupt") == "quit"


def test_the_view_carries_the_render_arguments_and_whole_seconds(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    session, _ = bare_session(mocker, [group])
    view = session.view()
    assert view.groups == [group] and view.now.microsecond == 0
    assert session.view() == session.view()


def test_a_dispatch_goes_through_the_terminal_hand_off(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch("jailbee.dashboard.dispatch._wait_for_return")
    session, terminal = bare_session(mocker, [group])
    session.handle_key("down")
    session.tick()
    assert session.open_menu_for_letter("t")  # tmux: a foreground dispatch
    session.menu_chosen("tmux", None, 0)
    assert len(terminal.handed) == 1
    child.assert_called_once()


def test_the_offset_clamp_uses_the_tables_own_width(mocker, tmp_path):
    clamp = mocker.spy(tsession, "clamp_column_offset")
    session, terminal = bare_session(mocker, [wide_group(tmp_path)])
    terminal.table_width = 40
    session.scroll_columns(1)
    assert clamp.call_args.kwargs["available"] == 40


def test_the_session_module_imports_without_textual():
    import subprocess
    import sys

    code = "import sys, jailbee.dashboard.tui.session; sys.exit('textual' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def _optimized(session):  # type: ignore[no-untyped-def]
    return dcolumns.optimize_column_widths(
        session.groups, now=tsession._now(), enabled=session.enabled, folded=session.folded
    )


def test_column_widths_are_optimized_at_startup(mocker, tmp_path):
    session, _ = bare_session(mocker, [wide_group(tmp_path)])
    assert session.column_widths is not None
    assert session.column_widths == _optimized(session)


def test_the_first_new_snapshot_reoptimizes_once(mocker, tmp_path):
    session, _ = bare_session(mocker, [wide_group(tmp_path)])
    client = session.client
    longer = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-" + "x" * 40, "alpha")])
    client.groups = [longer]
    client.seq = 2
    session.tick()
    assert session.column_widths == _optimized(session)
    first = session.column_widths
    client.groups = [dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("a", "alpha")])]
    client.seq = 3
    session.tick()
    assert session.column_widths == first


def test_a_manual_optimize_cancels_the_pending_reoptimize(mocker, tmp_path):
    session, _ = bare_session(mocker, [wide_group(tmp_path)])
    session.handle_key("optimize")
    manual = session.column_widths
    client = session.client
    client.groups = [dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("a" * 50, "alpha")])]
    client.seq = 2
    session.tick()
    assert session.column_widths == manual


def test_the_app_starts_with_optimized_widths(mocker, tmp_path):
    app = make_app(mocker, [wide_group(tmp_path)], auto_optimize=True)
    assert app.session.column_widths == _optimized(app.session)


def _memory_session(mocker, tmp_path, usage=None):
    info = ci("alpha-one", "alpha")
    info.memory_limit, info.memory_override, info.memory_usage = "24GiB", True, usage
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [info], memory_default="16GiB")
    session, _ = bare_session(mocker, [group])
    session.incus.config_show.return_value = "profiles: [base]"
    session.incus.profile_show.return_value = "config: {limits.memory: 16GiB}"
    return session, mocker.patch.object(session, "run_quiet_cli", return_value=True)


@pytest.mark.parametrize("host_total", [None, 20 * 1024**3])
def test_memory_picker_shared_rows(mocker, tmp_path, host_total):
    session, _ = _memory_session(mocker, tmp_path)
    session.system = (
        None
        if host_total is None
        else SystemStatus(
            memory=MemoryStatus(total=host_total, available=0, cache=0), pools=(), root=None
        )
    )
    session.selected = dmodel.Row("container", "alpha-one")
    session.handle_key("enter")
    session.menu_chosen("mem", None, 0)
    picker = session.overlay
    assert isinstance(picker, Picker) and picker.purpose == "container-memory"
    options = memory_limit.size_options(
        current="24GiB", override=True, default="16GiB", host_total=host_total
    )
    assert [(e.label, e.value) for e in picker.entries] == [
        (o.label + (" *" if o.current else ""), o.value) for o in options
    ]


@pytest.mark.parametrize("size", ["8GiB", "default"])
def test_memory_selection(mocker, tmp_path, size):
    session, quiet = _memory_session(mocker, tmp_path)
    assert (
        session.submit_picker(session.open_memory_picker("alpha-one"), PickerEntry(size, size))
        is None
    )
    assert quiet.call_args.args[1] == ["mem", "--", "alpha-one", size]


def test_memory_other(mocker, tmp_path):
    session, quiet = _memory_session(mocker, tmp_path)
    prompt = session.submit_picker(
        session.open_memory_picker("alpha-one"), PickerEntry("Other", memory_limit.OTHER_VALUE)
    )
    assert isinstance(prompt, TextPrompt) and prompt.purpose == "container-memory-other"
    assert validate_answer(prompt, "garbage") == memory_limit.validate_size("garbage")
    assert validate_answer(prompt, "12GiB") is None
    session.submit_prompt(prompt, " 12GiB ")
    assert quiet.call_args.args[1] == ["mem", "--", "alpha-one", "12GiB"]


@pytest.mark.parametrize("size", ["8GiB", "default"])
@pytest.mark.parametrize("answer", ["yes", "no"])
def test_memory_below_usage(mocker, tmp_path, size, answer):
    session, quiet = _memory_session(mocker, tmp_path, 20 * 1024**3)
    confirm = session.submit_picker(
        session.open_memory_picker("alpha-one"), PickerEntry(size, size)
    )
    assert isinstance(confirm, Picker) and confirm.purpose == "container-memory-confirm"
    quiet.assert_not_called()
    session.submit_picker(confirm, PickerEntry(answer, answer))
    if answer == "yes":
        assert quiet.call_args.args[1] == [
            "mem",
            "--confirmed-target",
            "16GiB" if size == "default" else size,
            "--force",
            "--",
            "alpha-one",
            size,
        ]
    else:
        quiet.assert_not_called()
        assert session.notice == "Cancelled"


def test_memory_default_uses_actual_profile_and_rechecks_vanished_target(mocker, tmp_path):
    session, quiet = _memory_session(mocker, tmp_path, 12 * 1024**3)
    session.groups[0].memory_default = "32GiB"
    session.incus.profile_show.return_value = "config: {limits.memory: 8GiB}"
    picker = session.open_memory_picker("alpha-one")
    confirm = session.submit_picker(picker, PickerEntry("Default", "default"))
    assert isinstance(confirm, Picker)
    assert "8GiB" in confirm.title
    session.groups[0].containers.clear()
    session.submit_picker(confirm, PickerEntry("Yes", "yes"))
    quiet.assert_not_called()


def test_memory_invalid_submit_and_escape_never_dispatch(mocker, tmp_path):
    session, quiet = _memory_session(mocker, tmp_path)
    prompt = session.submit_picker(
        session.open_memory_picker("alpha-one"), PickerEntry("Other", "other")
    )
    session.submit_prompt(prompt, "invalid")
    assert "positive Incus memory size" in session.notice
    session.overlay = prompt
    session.overlay_cancel()
    assert session.overlay is None
    quiet.assert_not_called()


def test_memory_ssh_denial_never_opens_picker(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    session, quiet = _memory_session(mocker, tmp_path)
    session.over_ssh = True
    session.ssh_policy = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["shell"])
    )
    assert session.open_memory_picker("alpha-one") is None
    quiet.assert_not_called()


def test_memory_confirmation_cannot_force_different_inherited_target(mocker, tmp_path):
    session, quiet = _memory_session(mocker, tmp_path, 20 * 1024**3)
    confirm = session.change_memory("alpha-one", "default")
    assert isinstance(confirm, Picker)
    session.incus.profile_show.return_value = "config: {limits.memory: 8GiB}"
    session.submit_picker(confirm, PickerEntry("Yes", "yes"))
    quiet.assert_not_called()
    assert "changed" in session.notice


def test_memory_default_refuses_uncapped_target(mocker, tmp_path):
    session, quiet = _memory_session(mocker, tmp_path)
    session.incus.profile_show.return_value = "config: {}"
    assert session.change_memory("alpha-one", "default") is None
    quiet.assert_not_called()
    assert "apply" in session.notice


# --- The Outbox box -----------------------------------------------------------


class _HeldJobs:
    """A job runner whose jobs finish only when the test says so."""

    def __init__(self) -> None:
        self.started: list[tuple[str, list[str], object]] = []

    def start(self, key, label, argv, cwd, on_done, *, capture_stdout=False):  # type: ignore[no-untyped-def]  # test double
        assert capture_stdout
        self.started.append((key, list(argv), on_done))

    def poll(self) -> None:
        pass

    def busy(self, key: str) -> bool:
        return False

    def running(self) -> dict[str, str]:
        return {}


def _outbox_group(tmp_path):  # type: ignore[no-untyped-def]
    from tests.dashboard_fixtures import cfg_group

    return cfg_group(tmp_path, (ci("alpha-x", "alpha"),))


def _outbox_json(**container: object) -> str:
    from tests.test_dashboard_outbox import _action, _container, _listing, _proposal

    return _listing(_container(_proposal(actions=[_action(0)]), **container))


def _quiet(mocker, stdout: str | None = None, ok: bool = True):  # type: ignore[no-untyped-def]
    return mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=tsession.da.CliResult(ok, "done" if ok else "exit 2", stdout or _outbox_json()),
    )


def _open_box(session) -> None:  # type: ignore[no-untyped-def]
    session.handle_key("down")
    session.tick()
    session.handle_key("enter")
    assert isinstance(session.overlay, MenuState)
    session.menu_chosen("outbox browse", None, 0)


_PLAN = (
    '{"schema": 1, "proposal": "pr/a.json", "revision": "r1", "actions": [{"index": 0, '
    '"kind": "comment", "target": "o/r#42", "will": "publish"}], "warnings": [], "notes": [], '
    '"login": "me", "total": 1, "foreign": false, "draftable": false, "refusals": []}'
)


def test_outbox_menu_opens_box(mocker, tmp_path):
    group = _outbox_group(tmp_path)
    quiet = _quiet(mocker)
    session, _ = bare_session(mocker, [group])

    _open_box(session)

    assert isinstance(session.overlay, tsession.OutboxState)
    assert session.overlay.scope_name == "alpha-x"
    quiet.assert_called_once_with(
        ["outbox", "ls", "alpha-x", "-o", "json", "--config", str(group.config_path)],
        cwd=tmp_path,
    )


def test_open_outbox_unavailable_shows_box_error(mocker, tmp_path):
    from jailbee.dashboard import outbox as dob

    _quiet(mocker, _outbox_json(available=False, error="container is stopped"), ok=False)
    session, _ = bare_session(mocker, [_outbox_group(tmp_path)])

    _open_box(session)

    state = session.overlay
    assert isinstance(state, tsession.OutboxState)
    header = dob.visible_rows(state, 100)[0]
    assert header.role == "header" and "container is stopped" in header.text.plain
    assert state.abilities.delete is False
    assert state.abilities.publish is False


def test_open_outbox_route_error_is_a_notice(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    quiet = _quiet(mocker)
    policy = RemoteSSHConfig.model_validate({"commands": {"mode": "allowlist", "allow": ["shell"]}})
    session, _ = bare_session(
        mocker, [_outbox_group(tmp_path)], over_ssh=True, remote=True, ssh_policy=policy
    )

    assert session.open_outbox("alpha-x") is None

    assert session.notice is not None and session.notice.startswith("could not list the outbox")
    quiet.assert_not_called()


def test_outbox_runner_checks_policy_before_spawn(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig
    from jailbee.dashboard.tui.outbox_wiring import SessionOutboxRunner

    run = mocker.patch.object(
        tsession.da.subprocess,
        "run",
        return_value=mocker.Mock(returncode=0, stdout=_outbox_json(), stderr=""),
    )
    policy = RemoteSSHConfig.model_validate(
        {"commands": {"mode": "allowlist", "allow": ["outbox browse", "outbox ls"]}}
    )
    session, _ = bare_session(
        mocker, [_outbox_group(tmp_path)], over_ssh=True, remote=True, ssh_policy=policy
    )
    session.jobs = _HeldJobs()  # type: ignore[assignment]  # test double

    _open_box(session)
    assert isinstance(session.overlay, tsession.OutboxState)
    assert session.overlay.abilities.delete is False
    assert run.call_count == 1  # the listing

    runner = SessionOutboxRunner(session, "alpha-x")
    drop = ["outbox", "drop", "alpha-x", "pr/a.json", "--yes", "--revision", "r1"]
    result = runner.run_quiet(drop)
    assert not result.ok
    with pytest.raises(ValueError):
        runner.start("k", "label", drop, lambda _result: None)
    assert run.call_count == 1  # neither reached a child
    assert session.jobs.started == []


def test_outbox_publish_over_ssh_hands_off_without_yes(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    _quiet(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch("jailbee.dashboard.dispatch._wait_for_return")
    session, terminal = bare_session(
        mocker, [_outbox_group(tmp_path)], over_ssh=True, remote=True, ssh_policy=RemoteSSHConfig()
    )
    session.jobs = _HeldJobs()  # type: ignore[assignment]  # test double
    _open_box(session)
    state = session.overlay
    assert isinstance(state, tsession.OutboxState)
    assert state.abilities.publish is False and state.abilities.publish_hand_off is True

    session.outbox_intent("publish", ("alpha-x", "pr/a.json", None, None))

    assert len(terminal.handed) == 1
    child.assert_called_once()
    argv = child.call_args.args[0]
    assert argv == ["jailbee", "outbox", "apply", "alpha-x", "pr/a.json", "--revision", "r1"]
    assert session.jobs.started == []
    assert isinstance(session.overlay, tsession.OutboxState)  # back in the box


def test_late_job_result_after_close_is_a_notice(mocker, tmp_path):
    from jailbee.dashboard.jobs import JobResult

    _quiet(mocker)
    session, _ = bare_session(mocker, [_outbox_group(tmp_path)])
    jobs = _HeldJobs()
    session.jobs = jobs  # type: ignore[assignment]  # test double
    _open_box(session)
    key = ("alpha-x", "pr/a.json", None, None)

    session.outbox_intent("publish", key)
    _, plan_argv, planned = jobs.started[-1]
    assert "--dry-run" in plan_argv
    planned(JobResult(0, "", _PLAN))  # type: ignore[operator]  # the held callback
    state = session.overlay
    assert isinstance(state, tsession.OutboxState) and state.question is not None
    session.outbox_intent("answer", None, "publish")
    _, apply_argv, published = jobs.started[-1]
    assert "-y" in apply_argv

    session.overlay_cancel()
    menu = session.overlay
    assert isinstance(menu, MenuState)
    published(JobResult(0, "", "Published 1 action\n"))  # type: ignore[operator]  # the held callback

    assert session.overlay is menu  # not reopened
    assert session.notice == "Published 1 action"


def test_late_job_result_does_not_overwrite_a_reopened_box(mocker, tmp_path):
    from jailbee.dashboard.jobs import JobResult

    _quiet(mocker)
    session, _ = bare_session(mocker, [_outbox_group(tmp_path)])
    jobs = _HeldJobs()
    session.jobs = jobs  # type: ignore[assignment]  # test double
    _open_box(session)
    session.outbox_intent("publish", ("alpha-x", "pr/a.json", None, None))
    _, _, planned = jobs.started[-1]

    session.overlay_cancel()
    session.menu_chosen("outbox browse", None, 0)
    reopened = session.overlay
    assert isinstance(reopened, tsession.OutboxState)
    planned(JobResult(0, "", _PLAN))  # type: ignore[operator]  # the held callback

    assert session.overlay is reopened
    assert reopened.question is None


def test_outbox_mutation_refreshes_client(mocker, tmp_path):
    quiet = _quiet(mocker)
    session, _ = bare_session(mocker, [_outbox_group(tmp_path)])
    _open_box(session)
    session.client.events.clear()
    key = ("alpha-x", "pr/a.json", None, None)

    session.outbox_intent("delete", key)
    state = session.overlay
    assert isinstance(state, tsession.OutboxState) and state.question is not None
    assert session.client.events == []
    session.outbox_intent("answer", None, "yes")

    assert any(c.args[0][:2] == ["outbox", "drop"] for c in quiet.call_args_list)
    assert ("refresh",) in session.client.events


def test_tick_never_reloads_an_open_question(mocker, tmp_path):
    quiet = _quiet(mocker)
    session, _ = bare_session(mocker, [_outbox_group(tmp_path)])
    _open_box(session)
    session.outbox_intent("delete", ("alpha-x", "pr/a.json", None, None))
    calls = quiet.call_count

    session.tick()
    session.tick()

    state = session.overlay
    assert isinstance(state, tsession.OutboxState) and state.question is not None
    assert quiet.call_count == calls


def test_outbox_cancel_without_question_goes_back_to_the_menu(mocker, tmp_path):
    _quiet(mocker)
    session, _ = bare_session(mocker, [_outbox_group(tmp_path)])
    _open_box(session)

    session.outbox_intent("cancel", None)

    assert isinstance(session.overlay, MenuState)


def test_old_outbox_pickers_are_gone():
    from pathlib import Path

    from jailbee.dashboard import outbox as dob

    source = Path(tsession.__file__).read_text()
    for purpose in ("container-outbox", "container-outbox-proposal", "container-outbox-confirm"):
        assert f'"{purpose}"' not in source
    assert "submit_outbox_picker" not in source
    for name in (
        "outbox_picker",
        "proposal_picker",
        "outbox_delete_confirm_picker",
        "ProposalRow",
        "OutboxListing",
        "legacy_parse_outbox_listing",
        "legacy_outbox_ls_argv",
        "legacy_outbox_apply_argv",
        "legacy_outbox_drop_argv",
        "outbox_browse_argv",
        "outbox_show_argv",
    ):
        assert not hasattr(dob, name), name
