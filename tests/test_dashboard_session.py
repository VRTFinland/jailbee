"""DashboardSession without any frontend: the Terminal seam and the view."""

from __future__ import annotations

import dataclasses

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
        self.calls: list[object] = []

    def start(self, key, label, argv, cwd, on_done, *, capture_stdout=False, on_progress=None):  # type: ignore[no-untyped-def]  # test double
        assert capture_stdout
        self.started.append((key, list(argv), on_done))

    def call(self, key, label, fn, on_done):  # type: ignore[no-untyped-def]  # test double
        """A listing: run now, delivered by the next `poll` (only calls are delivered)."""
        value = fn()
        self.calls.append(lambda: on_done(value))

    def poll(self) -> None:
        calls, self.calls = self.calls, []
        for deliver in calls:
            deliver()

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
        return_value=tsession.da.CliResult(
            ok, "done" if ok else "exit 2", stdout or _outbox_json()
        ),
    )


def _open_box(session) -> None:  # type: ignore[no-untyped-def]
    session.handle_key("down")
    session.tick()
    session.handle_key("enter")
    assert isinstance(session.overlay, MenuState)
    session.menu_chosen("outbox browse", None, 0)
    _settle(session)


def _settle(session) -> None:  # type: ignore[no-untyped-def]
    """Tick until the box's background listing has arrived."""
    import time

    deadline = time.monotonic() + 5
    while isinstance(session.overlay, tsession.OutboxState) and session.overlay.busy:
        assert time.monotonic() < deadline, "the outbox listing never arrived"
        time.sleep(0.005)
        session.tick()


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


def test_outbox_box_opens_before_its_listing_arrives(mocker, tmp_path):
    quiet = _quiet(mocker)
    session, _ = bare_session(mocker, [_outbox_group(tmp_path)])
    session.jobs = _HeldJobs()  # type: ignore[assignment]  # test double
    session.handle_key("down")
    session.tick()
    session.handle_key("enter")
    session.menu_chosen("outbox browse", None, 0)

    state = session.overlay
    assert isinstance(state, tsession.OutboxState)
    assert state.busy == "reading the outbox…" and state.containers == ()
    assert len(session.jobs.calls) == 1 and quiet.call_count == 1

    session.tick()  # delivers the listing
    state = session.overlay
    assert isinstance(state, tsession.OutboxState)
    assert state.busy is None and state.containers


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


def _orchestration_session(
    mocker,
    tmp_path,
    *,
    grant=False,
    profiles=("bugfixer",),
    ops=("spawn",),
    mounts=(),
    kinds=(),
    egress=(),
    loose=False,
):
    from jailbee.orchestration.view import GrantView

    info = ci("alpha-one", "alpha")
    if grant:
        info = dataclasses.replace(
            info,
            configured_mounts=tuple(kinds),
            grant=GrantView(limit=2, operations=ops, mounts=mounts, loose=loose, egress=egress),
        )
    group = dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [info], orchestration_profiles=tuple(profiles)
    )
    session, _ = bare_session(mocker, [group])
    return session, mocker.patch.object(session, "run_quiet_cli", return_value=True)


def test_orchestration_picker_without_grant_lists_profiles(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path)
    picker = session.open_orchestration("alpha-one")
    assert picker.purpose == "container-orchestration"
    assert [e.value for e in picker.entries] == ["profile:bugfixer", "defaults"]
    assert [e.label for e in picker.entries] == [
        "Grant profile 'bugfixer'",
        "Grant with defaults",
    ]
    assert picker.detail == ()


def test_orchestration_picker_with_grant_reapplies_and_revokes(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    picker = session.open_orchestration("alpha-one")
    assert [e.value for e in picker.entries][-2:] == ["profile:bugfixer", "revoke"]
    assert picker.entries[-2].label == "Re-apply profile 'bugfixer'"
    # card lines, markup stripped (the picker draws plain text)
    assert picker.detail[0].startswith("orchestrator · children 0/2")
    assert all("[dim]" not in line for line in picker.detail)


@pytest.mark.parametrize(
    ("value", "argv"),
    [
        ("profile:bugfixer", ["orchestrate", "grant", "--profile", "bugfixer", "--", "alpha-one"]),
        ("defaults", ["orchestrate", "grant", "--", "alpha-one"]),
    ],
)
def test_orchestration_grant_runs_cli(mocker, tmp_path, value, argv):
    session, quiet = _orchestration_session(mocker, tmp_path)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", value))
    assert quiet.call_args.args[1] == argv


def test_orchestration_revoke_asks_first(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    confirm = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("Revoke…", "revoke")
    )
    assert confirm.purpose == "container-orchestration-revoke"
    assert [e.label for e in confirm.entries] == ["No", "Yes, revoke"]
    quiet.assert_not_called()
    session.submit_picker(confirm, PickerEntry("No", "no"))
    quiet.assert_not_called()
    session.submit_picker(confirm, PickerEntry("Yes, revoke", "yes"))
    assert quiet.call_args.args[1] == ["orchestrate", "revoke", "--", "alpha-one"]


def test_orchestration_picker_refused_for_a_child(mocker, tmp_path):
    from jailbee.orchestration.view import ChildView

    session, _ = _orchestration_session(mocker, tmp_path)
    group = session.groups[0]
    group.containers[0] = dataclasses.replace(
        group.containers[0], child=ChildView("alpha-o", "running", (), False, ())
    )
    assert session.open_orchestration("alpha-one") is None
    assert "not available" in session.notice


def _edit(session, quiet, value):
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", value))
    return quiet.call_args.args[1]


def test_orchestration_edit_entries_labels(mocker, tmp_path):
    session, _ = _orchestration_session(
        mocker,
        tmp_path,
        grant=True,
        ops=("spawn", "restart"),
        mounts=("data",),
        kinds=("data", "docs"),
        egress=("pypi.org",),
    )
    picker = session.open_orchestration("alpha-one")
    assert [(e.label, e.value) for e in picker.entries] == [
        ("Children limit: 2…", "limit"),
        ("Allow import: off", "op:import"),
        ("Allow restart: on", "op:restart"),
        ("Allow stop: off", "op:stop"),
        ("Allow destroy: off", "op:destroy"),
        ("Children may be loose: no", "loose"),
        ("Grant mount data: on", "mount:data"),
        ("Grant mount docs: off", "mount:docs"),
        ("Egress for children: pypi.org…", "egress"),
        ("Default runner: inherit (agent default)…", "runner"),
        ("Re-apply profile 'bugfixer'", "profile:bugfixer"),
        ("Revoke…", "revoke"),
    ]


def test_orchestration_mount_kinds_fall_back_to_repo(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    session.groups[0].optional_mounts = ("docs",)
    values = [e.value for e in session.open_orchestration("alpha-one").entries]
    assert "mount:docs" in values


def test_toggle_destroy_on(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    assert _edit(session, quiet, "op:destroy") == [
        "orchestrate",
        "grant",
        "--allow",
        "destroy",
        "--",
        "alpha-one",
    ]


def test_toggle_restart_off(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True, ops=("spawn", "restart"))
    assert _edit(session, quiet, "op:restart") == [
        "orchestrate",
        "grant",
        "--deny",
        "restart",
        "--",
        "alpha-one",
    ]


def test_loose_toggle(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    assert _edit(session, quiet, "loose") == ["orchestrate", "grant", "--loose", "--", "alpha-one"]


def test_mount_toggle_sends_full_list(mocker, tmp_path):
    session, quiet = _orchestration_session(
        mocker, tmp_path, grant=True, mounts=("data",), kinds=("data", "docs")
    )
    assert _edit(session, quiet, "mount:docs") == [
        "orchestrate",
        "grant",
        "--mount",
        "data",
        "--mount",
        "docs",
        "--",
        "alpha-one",
    ]


def test_turning_off_last_mount_clears(mocker, tmp_path):
    session, quiet = _orchestration_session(
        mocker, tmp_path, grant=True, mounts=("data",), kinds=("data",)
    )
    assert _edit(session, quiet, "mount:data") == [
        "orchestrate",
        "grant",
        "--clear-mounts",
        "--",
        "alpha-one",
    ]


def test_limit_prompt_validates_and_runs(mocker, tmp_path):
    from jailbee.dashboard.overlays import validate_answer

    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    prompt = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "limit")
    )
    assert prompt.purpose == "container-orchestration-limit"
    assert prompt.initial == "2"
    for bad in ("0", "65", "x", "", "-1"):
        assert validate_answer(prompt, bad)
    assert validate_answer(prompt, "5") is None
    assert validate_answer(prompt, " 64 ") is None
    session.submit_prompt(prompt, " 5 ")
    assert quiet.call_args.args[1] == ["orchestrate", "grant", "--children", "5", "--", "alpha-one"]


def test_egress_prompt_parses_list_and_prefills(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True, egress=("a.org", "b.org"))
    prompt = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "egress")
    )
    assert prompt.purpose == "container-orchestration-egress"
    assert prompt.initial == "a.org, b.org"
    session.submit_prompt(prompt, "pypi.org, *.npmjs.org")
    assert quiet.call_args.args[1] == [
        "orchestrate",
        "grant",
        "--egress",
        "pypi.org",
        "--egress",
        "*.npmjs.org",
        "--",
        "alpha-one",
    ]


def test_empty_egress_answer_clears(mocker, tmp_path):
    from jailbee.dashboard.overlays import validate_answer

    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    prompt = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "egress")
    )
    assert validate_answer(prompt, "  ") is None
    session.submit_prompt(prompt, "  ")
    assert quiet.call_args.args[1] == ["orchestrate", "grant", "--clear-egress", "--", "alpha-one"]


def test_edit_reopens_picker(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    shown = session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    assert session.overlay.purpose == "container-orchestration"
    assert shown is session.overlay


def test_prompt_edit_reopens_picker(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    prompt = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "limit")
    )
    shown = session.submit_prompt(prompt, "3")
    assert shown.purpose == "container-orchestration"


def test_stale_picker_does_not_run_after_row_loses_eligibility(mocker, tmp_path):
    from jailbee.orchestration.view import ChildView

    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    picker = session.open_orchestration("alpha-one")
    group = session.groups[0]
    group.containers[0] = dataclasses.replace(
        group.containers[0], child=ChildView("alpha-o", "running", (), False, ())
    )
    assert session.submit_picker(picker, PickerEntry("", "loose")) is None
    quiet.assert_not_called()
    assert "not available" in session.notice


def test_revoke_hidden_when_policy_forbids(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    mocker.patch("jailbee.dashboard.tui.session.permitted", return_value=False)
    # offered() uses actions' own permitted import, so only the revoke entry is gated here
    picker = session.open_orchestration("alpha-one")
    assert "revoke" not in [e.value for e in picker.entries]


def _run_edits(session, quiet, *values):
    """Choose each value in turn from the picker the session itself reopened."""
    argvs = []
    picker = session.open_orchestration("alpha-one")
    for value in values:
        picker = session.submit_picker(picker, PickerEntry("", value))
        argvs.append(quiet.call_args.args[1])
    return argvs


def test_consecutive_mount_toggles_use_the_updated_grant(mocker, tmp_path):
    session, quiet = _orchestration_session(
        mocker, tmp_path, grant=True, mounts=("data",), kinds=("data", "docs")
    )
    first, second = _run_edits(session, quiet, "mount:docs", "mount:data")
    assert first[2:6] == ["--mount", "data", "--mount", "docs"]
    # docs stays: the second edit must not be computed from the stale (data,)
    assert second == ["orchestrate", "grant", "--mount", "docs", "--", "alpha-one"]


def test_consecutive_op_and_loose_toggles_flip_direction(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    argvs = _run_edits(session, quiet, "op:destroy", "op:destroy", "loose", "loose")
    assert [a[2] for a in argvs] == ["--allow", "--deny", "--loose", "--no-loose"]


def test_failed_edit_does_not_patch_the_grant(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    quiet.return_value = False
    for _ in range(2):  # the picker is closed after a failure, so open it afresh each time
        session.submit_picker(
            session.open_orchestration("alpha-one"), PickerEntry("", "op:destroy")
        )
        assert quiet.call_args.args[1][2] == "--allow"


def test_tick_rebuilds_the_orchestration_picker_and_keeps_cursor(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    reopened = session.overlay
    labels = {e.value: e.label for e in reopened.entries}
    assert labels["loose"] == "Children may be loose: yes"  # the local patch
    # the snapshot (loose still off) wins once the patch is gone; the picker follows it
    session._grant_patches.clear()
    session.tick()
    labels = {e.value: e.label for e in session.overlay.entries}
    assert labels["loose"] == "Children may be loose: no"
    assert session.overlay.start_index == reopened.start_index > 0


def test_snapshot_that_is_neither_base_nor_patch_keeps_the_patch(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    info = session.client.groups[0].containers[0]
    # a gather racing a later edit: it matches neither the old grant nor the patch
    session.client.groups[0].containers[0] = dataclasses.replace(
        info, grant=dataclasses.replace(info.grant, limit=7)
    )
    session.tick()
    assert "alpha-one" in session._grant_patches
    assert session.groups[0].containers[0].grant.loose is True
    # the next edit still computes from the patched grant
    session.submit_picker(session.overlay, PickerEntry("", "loose"))
    assert quiet.call_args.args[1][2] == "--no-loose"


def test_snapshot_equal_to_the_patch_drops_it(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    info = session.client.groups[0].containers[0]
    session.client.groups[0].containers[0] = dataclasses.replace(
        info, grant=dataclasses.replace(info.grant, loose=True)
    )
    session.tick()
    assert session._grant_patches == {}
    assert session.groups[0].containers[0].grant.loose is True


def test_failed_edit_closes_the_picker_so_the_notice_shows(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    quiet.return_value = False
    session.overlay = session.open_orchestration("alpha-one")
    assert session.submit_picker(session.overlay, PickerEntry("", "loose")) is None
    assert session.overlay is None
    assert session._grant_patches == {}


def test_failed_text_edit_closes_the_picker(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    quiet.return_value = False
    prompt = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "limit")
    )
    session.overlay = prompt
    assert session.submit_prompt(prompt, "3") is None
    assert session.overlay is None
    assert session._grant_patches == {}


def test_failed_revoke_does_not_patch(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    quiet.return_value = False
    confirm = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("Revoke…", "revoke")
    )
    session.submit_picker(confirm, PickerEntry("Yes, revoke", "yes"))
    assert session._grant_patches == {}
    assert session.groups[0].containers[0].grant is not None


def test_stale_snapshot_keeps_the_patch_for_the_next_edit(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    session.tick()  # the snapshot still shows the pre-edit grant
    labels = {e.value: e.label for e in session.overlay.entries}
    assert labels["loose"] == "Children may be loose: yes"
    session.submit_picker(session.overlay, PickerEntry("", "loose"))
    assert quiet.call_args.args[1][2] == "--no-loose"


def test_patch_expires(mocker, tmp_path):
    import jailbee.dashboard.tui.session as tsession

    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    clock = mocker.patch.object(tsession.time, "monotonic", return_value=1000.0)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    clock.return_value = 1000.0 + tsession.GRANT_PATCH_SECONDS - 1
    session.tick()
    assert session.groups[0].containers[0].grant.loose is True
    clock.return_value = 1000.0 + tsession.GRANT_PATCH_SECONDS + 1
    session.tick()
    assert session.groups[0].containers[0].grant.loose is False
    assert session._grant_patches == {}


def test_patch_survives_a_filtered_out_container(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    session.hidden_repos = frozenset({"alpha"})  # filtered out of session.groups
    session.tick()
    assert session.groups == []
    assert "alpha-one" in session._grant_patches
    session.tick()  # a second tick must not prune it either
    assert "alpha-one" in session._grant_patches


def test_revoke_after_edit_leaves_no_grant(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    confirm = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("Revoke…", "revoke")
    )
    session.submit_picker(confirm, PickerEntry("Yes, revoke", "yes"))
    assert session.groups[0].containers[0].grant is None
    session.tick()  # a stale snapshot still has the grant
    assert session.groups[0].containers[0].grant is None
    # then the real (revoked) snapshot arrives and the patch goes
    session.client.groups[0].containers[0] = dataclasses.replace(
        session.client.groups[0].containers[0], grant=None
    )
    session.tick()
    assert session._grant_patches == {}


def test_reapply_profile_after_edit_drops_the_patch(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    session.submit_picker(session.open_orchestration("alpha-one"), PickerEntry("", "loose"))
    session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "profile:bugfixer")
    )
    assert session._grant_patches == {}
    session.tick()
    assert session.groups[0].containers[0].grant.loose is False  # the snapshot, not the old patch


def test_patched_grant_is_normalised_like_the_cli(mocker, tmp_path):
    from jailbee.orchestration.grant import Grant, Grantable

    session, quiet = _orchestration_session(
        mocker,
        tmp_path,
        grant=True,
        ops=("spawn", "message"),
        mounts=("docs",),
        kinds=("data", "docs"),
    )
    _run_edits(session, quiet, "mount:data", "op:restart", "op:import")
    patched = session.groups[0].containers[0].grant
    real = Grant(
        operations=("spawn", "message", "restart", "import"),
        grantable=Grantable(mounts=("docs", "data", "data")),
    )
    assert patched.operations == real.operations
    assert patched.mounts == real.grantable.mounts
    from jailbee.dashboard.tui.session import _normalised

    ops = _normalised(dataclasses.replace(patched, egress=("b", "a", "a"))).egress
    assert ops == Grantable(egress=("b", "a", "a")).egress


def test_limit_one_is_valid(mocker, tmp_path):
    from jailbee.dashboard.overlays import validate_answer

    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    prompt = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "limit")
    )
    assert validate_answer(prompt, "1") is None


def test_loose_on_toggles_to_no_loose(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True, loose=True)
    assert _edit(session, quiet, "loose") == [
        "orchestrate",
        "grant",
        "--no-loose",
        "--",
        "alpha-one",
    ]


def test_mount_off_keeps_the_others(mocker, tmp_path):
    session, quiet = _orchestration_session(
        mocker, tmp_path, grant=True, mounts=("data", "docs"), kinds=("data", "docs")
    )
    assert _edit(session, quiet, "mount:docs") == [
        "orchestrate",
        "grant",
        "--mount",
        "data",
        "--",
        "alpha-one",
    ]


def test_text_edit_rechecks_grant_and_eligibility(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    prompt = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "limit")
    )
    group = session.groups[0]
    group.containers[0] = dataclasses.replace(group.containers[0], grant=None)
    assert session.submit_prompt(prompt, "3") is None
    quiet.assert_not_called()
    assert "not available" in session.notice


def _ssh_orchestration_session(mocker, tmp_path, *, level, grant_kw=None, kinds=("data",)):
    from jailbee.config.models_remote import RemoteSSHConfig

    session, _ = _orchestration_session(mocker, tmp_path, grant=True, kinds=kinds)
    group = session.groups[0]
    info = group.containers[0]
    if grant_kw:
        info = dataclasses.replace(info, grant=dataclasses.replace(info.grant, **grant_kw))
        group.containers[0] = info
    session.over_ssh = True
    session.ssh_policy = RemoteSSHConfig(orchestration=level)
    return session


def test_menu_over_ssh_setting_off_shows_only_revoke(mocker, tmp_path):
    session = _ssh_orchestration_session(mocker, tmp_path, level="off")
    assert [e.value for e in session.open_orchestration("alpha-one").entries] == ["revoke"]


def test_menu_over_ssh_ssh_level_hides_widening_toggles(mocker, tmp_path):
    session = _ssh_orchestration_session(mocker, tmp_path, level="ssh")
    values = [e.value for e in session.open_orchestration("alpha-one").entries]
    assert "loose" not in values
    assert "mount:data" not in values
    assert {"limit", "op:destroy", "egress"} <= set(values)


def test_menu_over_ssh_offers_turning_a_wide_field_off(mocker, tmp_path):
    session = _ssh_orchestration_session(
        mocker, tmp_path, level="ssh", grant_kw={"loose": True, "mounts": ("data",)}
    )
    values = [e.value for e in session.open_orchestration("alpha-one").entries]
    assert "loose" in values
    assert "mount:data" in values


def test_menu_locally_unchanged(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True, kinds=("data",))
    assert [e.value for e in session.open_orchestration("alpha-one").entries] == [
        "limit",
        "op:import",
        "op:restart",
        "op:stop",
        "op:destroy",
        "loose",
        "mount:data",
        "egress",
        "runner",
        "profile:bugfixer",
        "revoke",
    ]


def test_default_runner_picker_and_submit(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    group = session.groups[0]
    group.default_runner = "claude haiku"
    group.default_runner_source = "repo"
    group.runner_profiles = ("grok",)
    picker = session.open_orchestration("alpha-one")
    assert ("Default runner: inherit (claude haiku from repo)…", "runner") in [
        (e.label, e.value) for e in picker.entries
    ]
    runner = session.submit_picker(picker, PickerEntry("", "runner"))
    assert "claude-jb:grok:sonnet" in [e.value for e in runner.entries]
    assert "inherit" in [e.value for e in runner.entries]
    result = session.submit_picker(runner, PickerEntry("", "claude::opus"))
    assert quiet.call_args.args[1] == [
        "orchestrate",
        "grant",
        "--runner-agent",
        "claude",
        "--runner-model",
        "opus",
        "--",
        "alpha-one",
    ]
    assert result.entries[result.start_index].value == "runner"
    assert session.groups[0].containers[0].grant.default_runner == "claude opus"
    runner = session.submit_picker(result, PickerEntry("", "runner"))
    session.submit_picker(runner, PickerEntry("", "inherit"))
    assert quiet.call_args.args[1] == ["orchestrate", "grant", "--clear-runner", "--", "alpha-one"]
    assert session.groups[0].containers[0].grant.default_runner is None


def test_runner_picker_escape_returns_to_orchestration(mocker, tmp_path):
    session, _ = _orchestration_session(mocker, tmp_path, grant=True)
    picker = session.open_orchestration("alpha-one")
    session.overlay = session.submit_picker(picker, PickerEntry("", "runner"))
    session.overlay_cancel()
    assert session.overlay.purpose == "container-orchestration"


def test_runner_picker_failure_does_not_patch(mocker, tmp_path):
    session, quiet = _orchestration_session(mocker, tmp_path, grant=True)
    quiet.return_value = False
    picker = session.submit_picker(
        session.open_orchestration("alpha-one"), PickerEntry("", "runner")
    )
    assert session.submit_picker(picker, PickerEntry("", "claude::opus")) is None
    assert session.groups[0].containers[0].grant.default_runner is None


def test_runner_entry_absent_if_grant_forbidden(mocker, tmp_path):
    session = _ssh_orchestration_session(mocker, tmp_path, level="off")
    assert "runner" not in [e.value for e in session.open_orchestration("alpha-one").entries]
