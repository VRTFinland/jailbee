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
        assert quiet.call_args.args[1] == ["mem", "--confirmed-target", "16GiB" if size == "default" else size, "--force", "--", "alpha-one", size]
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
    confirm = session.change_memory('alpha-one', 'default')
    assert isinstance(confirm, Picker)
    session.incus.profile_show.return_value = 'config: {limits.memory: 8GiB}'
    session.submit_picker(confirm, PickerEntry('Yes', 'yes'))
    quiet.assert_not_called()
    assert 'changed' in session.notice


def test_memory_default_refuses_uncapped_target(mocker, tmp_path):
    session, quiet = _memory_session(mocker, tmp_path)
    session.incus.profile_show.return_value = 'config: {}'
    assert session.change_memory('alpha-one', 'default') is None
    quiet.assert_not_called()
    assert 'apply' in session.notice
