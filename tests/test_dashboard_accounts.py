from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from jailbee.dashboard import accounts as da
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import frame as tframe

ROWS = json.dumps(
    [
        {
            "agent": "claude",
            "group": "team",
            "account": "a@x.io#org12345",
            "state": "live",
            "repos": ["alpha"],
            "containers": ["alpha-x"],
        },
        {
            "agent": "claude",
            "group": None,
            "account": "b@x.io~2",
            "state": "parked",
            "repos": [],
            "containers": [],
        },
        {
            "agent": "claude",
            "group": "spare",
            "account": None,
            "state": "empty",
            "repos": [],
            "containers": [],
        },
    ]
)


def test_parse_account_rows_keeps_slot_names_verbatim() -> None:
    rows = da.parse_account_rows(ROWS)
    assert [r.account for r in rows] == ["a@x.io#org12345", "b@x.io~2", None]
    assert rows[0].repos == ("alpha",) and rows[1].group is None


@pytest.mark.parametrize("bad", ["", "{}", "null", "not json", '[{"agent": 1}]'])
def test_parse_account_rows_rejects_garbage_with_a_typed_error(bad: str) -> None:
    with pytest.raises(da.AccountLoadError):
        da.parse_account_rows(bad)


def test_empty_pool_parses_to_no_rows() -> None:
    assert da.parse_account_rows("[]") == ()


def test_missing_lists_default_to_empty() -> None:
    (row,) = da.parse_account_rows('[{"agent": "claude", "state": "parked"}]')
    assert row.repos == () and row.containers == () and row.group is None


def test_account_ls_argv_asks_for_the_parsed_fields() -> None:
    assert da.account_ls_argv() == [
        "account",
        "ls",
        "-o",
        "json",
        "--fields",
        "agent,group,account,state,repos,containers",
    ]


def test_group_ls_argv_asks_the_group_listing_for_the_same_fields() -> None:
    assert da.group_ls_argv() == [
        "account",
        "group",
        "ls",
        "-o",
        "json",
        "--fields",
        "agent,group,account,state,repos,containers",
    ]


def test_argv_builders_keep_special_slot_names_as_single_elements() -> None:
    assert da.use_argv("claude", "team", "b@x.io#org12345~2") == [
        "account",
        "use",
        "b@x.io#org12345~2",
        "-a",
        "claude",
        "-g",
        "team",
    ]
    assert da.park_argv("claude", "team") == ["account", "park", "-a", "claude", "-g", "team"]
    assert da.rm_login_argv("claude", "b@x.io~2") == [
        "account",
        "rm",
        "b@x.io~2",
        "-a",
        "claude",
        "--yes",
    ]
    assert da.group_rm_argv("spare") == ["account", "group", "rm", "spare", "--yes"]
    assert da.group_create_argv("g") == ["account", "group", "create", "g"]
    assert da.repo_group_set_argv("none") == ["account", "group", "set", "none"]
    assert da.repo_group_unset_argv() == ["account", "group", "unset"]
    assert da.container_group_use_argv("team", "alpha-x") == [
        "account",
        "group",
        "use",
        "team",
        "alpha-x",
    ]
    assert da.container_group_reset_argv("alpha-x") == ["account", "group", "reset", "alpha-x"]


def test_group_names_and_parked_for() -> None:
    rows = da.parse_account_rows(ROWS)
    assert da.group_names(rows) == ("spare", "team")
    assert [r.account for r in da.parked_for(rows, "claude")] == ["b@x.io~2"]
    assert da.parked_for(rows, "codex") == ()


def test_actions_depend_on_the_row_kind() -> None:
    rows = da.parse_account_rows(ROWS)
    live, parked, empty = rows
    assert [a for _label, a in da.account_actions(live, rows)] == ["use", "park"]
    assert [a for _label, a in da.account_actions(parked, rows)] == ["use-in", "delete"]
    assert [a for _label, a in da.account_actions(empty, rows)] == ["use", "group-rm"]


def test_actions_omit_use_when_nothing_is_parked_and_group_rm_when_in_use() -> None:
    live = da.AccountRow("claude", "team", "a@x.io", "live", ("alpha",), ())
    assert [a for _label, a in da.account_actions(live, [live])] == ["park"]


def test_live_login_never_offers_group_rm_even_when_unused() -> None:
    live = da.AccountRow("claude", "team", "a@x.io", "live", (), ())
    assert [a for _label, a in da.account_actions(live, [live])] == ["park"]


def test_a_group_row_in_an_unknown_state_is_never_offered_for_removal() -> None:
    """Only an `empty` group is removable; an unforeseen state is not guessed at."""
    odd = da.AccountRow("claude", "team", None, "unknown", (), ())
    assert "group-rm" not in [a for _label, a in da.account_actions(odd, [odd])]


def test_ungrouped_live_row_has_no_actions() -> None:
    own = da.AccountRow("claude", None, "a@x.io", "live", ("beta",), ())
    assert da.account_actions(own, [own]) == ()


def test_parked_row_offers_no_use_in_without_groups() -> None:
    parked = da.AccountRow("claude", None, "b@x.io", "parked", (), ())
    assert [a for _label, a in da.account_actions(parked, [parked])] == ["delete"]


def test_run_cli_quiet_reports_the_last_stderr_line_on_failure(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 2
    run.return_value.stdout = ""
    run.return_value.stderr = "warn\n\x1b[31merror: an agent is running; pass --force\x1b[0m\n"
    result = da.run_cli_quiet(["account", "group", "set", "g"], cwd=Path("/r"))
    assert result == da.CliResult(False, "error: an agent is running; pass --force")
    assert run.call_args.args[0] == ["jailbee", "account", "group", "set", "g"]
    assert run.call_args.kwargs["cwd"] == Path("/r")
    assert run.call_args.kwargs["capture_output"] is True


def test_run_cli_quiet_falls_back_to_exit_code(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 3
    run.return_value.stdout = ""
    run.return_value.stderr = ""
    assert da.run_cli_quiet(["x"], cwd=Path("/r")) == da.CliResult(False, "exited 3")


def test_run_cli_quiet_success_timeout_and_oserror(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 0
    run.return_value.stdout = "Switched.\n"
    run.return_value.stderr = ""
    assert da.run_cli_quiet(["account", "park"], cwd=Path("/r")) == da.CliResult(
        True, "Switched.", "Switched.\n"
    )
    run.side_effect = da.subprocess.TimeoutExpired(["jailbee"], 60)
    assert da.run_cli_quiet(["account", "park"], cwd=Path("/r")) == da.CliResult(False, "timed out")
    run.side_effect = FileNotFoundError("jailbee: not found")
    result = da.run_cli_quiet(["account", "park"], cwd=Path("/r"))
    assert not result.ok and "not found" in result.message


def test_account_lines_one_line_per_row_under_a_header() -> None:
    rows = da.parse_account_rows(ROWS)
    header, lines = da.account_lines(rows, 90)
    assert header[0].plain.split() == [
        "GROUP",
        "AGENT",
        "ACCOUNT",
        "STATE",
        "USED",
        "BY",
    ]
    assert len(header) == 2  # the titles and their rule
    assert len(lines) == len(rows)
    assert all(line.cell_len <= 90 for line in (*header, *lines))
    assert "a@x.io#org12345" in lines[0].plain and "alpha, alpha-x" in lines[0].plain
    assert "parked" in lines[1].plain and "team" in lines[0].plain


def test_account_lines_of_no_rows_are_only_the_header() -> None:
    header, lines = da.account_lines((), 80)
    assert lines == () and header and "GROUP" in header[0].plain


@pytest.mark.parametrize("width", [70, 90, 110])
def test_account_lines_keep_the_short_columns_beside_long_values(width: int) -> None:
    """A long login and repo list must squeeze ACCOUNT and USED BY, not GROUP/AGENT/STATE."""
    long_row = da.AccountRow(
        "claude",
        "team",
        "tuomas.airaksinen@gisgro.com#org-3f9a2c71",
        "live",
        ("gisgro-incus-env", "other-repo"),
        ("gisgro-incus-env-help", "other-main"),
    )
    header, lines = da.account_lines((long_row,), width)
    assert all(name in header[0].plain for name in ("GROUP", "AGENT", "STATE", "USED BY"))
    assert "team" in lines[0].plain and "claude" in lines[0].plain and "live" in lines[0].plain
    assert "tuomas" in lines[0].plain


def test_account_lines_keep_short_columns_when_a_login_is_long() -> None:
    rows = (da.AccountRow("claude", "team", "x" * 200, "live", (), ()),)
    _header, lines = da.account_lines(rows, 80)
    assert lines[0].plain.startswith("team") and "live" in lines[0].plain


def test_account_lines_are_markup_safe_and_hold_one_line_each() -> None:
    weird = da.AccountRow("claude", "g[/x]", "[bold]a@x.io", "live", ("r[1]",), ())
    _header, lines = da.account_lines((weird, weird), 100)
    assert len(lines) == 2
    assert "[bold]a@x.io" in lines[0].plain and "g[/x]" in lines[0].plain


def test_account_lines_hold_to_a_tiny_width() -> None:
    rows = da.parse_account_rows(ROWS)
    header, lines = da.account_lines(rows, 5)  # floored at 20 cells
    assert len(lines) == len(rows) and len(header) == 2
    assert all(line.cell_len <= 20 for line in (*header, *lines))


def test_run_cli_quiet_decodes_leniently(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 0
    run.return_value.stdout = "ok\n"
    run.return_value.stderr = ""
    da.run_cli_quiet(["account", "park"], cwd=Path("/r"))
    assert run.call_args.kwargs["errors"] == "replace"


def test_run_cli_quiet_turns_a_decode_error_into_a_failed_result(mocker: MockerFixture) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    result = da.run_cli_quiet(["account", "park"], cwd=Path("/r"))
    assert result.ok is False and result.message


# Real output of the CLI in a scratch HOME, piped (so Rich wraps at 80 columns).
# The refusal is the source text of `_refuse_if_agent_running` wrapped the same
# way: reproducing it needs a running container.
_CREATE_OK = (
    "✓ Created group `alpha` for claude → \n"
    "/tmp/scratch/d/jailbee/claude-credentials/alpha\n"
    "It holds no login yet. `jailbee account group set alpha` moves this repo into \n"
    "it, `jailbee account group use alpha <container>` moves one container, and \n"
    "`jailbee account use -g alpha <account>` activates a stored login into it.\n"
)
_INVALID_NAME = (
    "✗ invalid credential group name 'Bad Name': must match * — lowercase letters, \n"
    "digits and hyphens, starting with a letter or digit.\n"
)
_REFUSAL = (
    "✗ An agent is running in repo-feat-x. Swapping the credential group under a live \n"
    "session can overwrite the target group's login with this one's on the next token \n"
    "refresh, and that login cannot be recovered.\n"
    "Close it in that container and run this again, or pass --force if you are sure.\n"
)


def _old_last_line(text: str) -> str:
    return [ln.strip() for ln in text.splitlines() if ln.strip()][-1]


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr", "expected"),
    [
        (0, _CREATE_OK, "", "✓ Created group `alpha` for claude"),
        (0, "✓ Removed group `alpha`\n", "", "✓ Removed group `alpha`"),
        (
            0,
            "Nothing to park: this holder has no stored login.\n",
            "",
            "Nothing to park: this holder has no stored login.",
        ),
        (
            2,
            "",
            _INVALID_NAME,
            "✗ invalid credential group name 'Bad Name': must match * — "
            "lowercase letters, digits and hyphens, starting with a letter or digit.",
        ),
        (
            2,
            "",
            _REFUSAL,
            "✗ An agent is running in repo-feat-x. Swapping the credential group under "
            "a live session can overwrite the target group's login with this one's on the "
            "next token refresh, and that login cannot be recovered. Close it in that "
            "container and run this again, or pass --force if you are sure.",
        ),
    ],
    ids=["create-ok", "rm-ok", "no-marker", "invalid-name", "refused"],
)
def test_run_cli_quiet_message_is_the_verdict_not_the_last_line(
    mocker: MockerFixture, rc: int, stdout: str, stderr: str, expected: str
) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = rc
    run.return_value.stdout = stdout
    run.return_value.stderr = stderr
    result = da.run_cli_quiet(["account", "group", "create", "x"], cwd=Path("/r"))
    assert result.message == expected
    assert "\n" not in result.message
    assert result.stdout == (stdout if rc == 0 else "")
    if len((stdout or stderr).splitlines()) > 1 and expected.startswith(("✓", "✗")):
        # the pre-fix rule (last non-empty line) got these wrong
        assert _old_last_line(stdout or stderr) != expected


def test_verdict_marker_beats_an_earlier_warning_and_stops_at_the_next_verdict(
    mocker: MockerFixture,
) -> None:
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 2
    run.return_value.stdout = ""
    run.return_value.stderr = "⚠ something odd\n✗ first\nmore\n✗ second\n"
    assert da.run_cli_quiet(["x"], cwd=Path("/r")).message == "✗ first more"


def _frame(tmp_path: Path, notice: str, width: int = 100) -> list[str]:
    group = dmodel.RepoGroup("alpha", "/repos/alpha", tmp_path / "a.yaml", [])
    from tests.dashboard_pilot import paint, view_of

    return paint(view_of([group], notice=notice), size=(width, 200))


def _flat(lines: list[str]) -> str:
    """The frame's text with borders and line breaks collapsed to single spaces."""
    return " ".join(" ".join(ln.strip(" │╭╮╰╯─") for ln in lines).split())


def test_a_long_refusal_is_shown_whole_below_the_table_with_its_remedy(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """The refusal's remedy (`pass --force`) sits past any border-width cut."""
    run = mocker.patch.object(da.subprocess, "run")
    run.return_value.returncode = 2
    run.return_value.stdout = ""
    run.return_value.stderr = _REFUSAL
    message = da.run_cli_quiet(["account", "group", "set", "beta"], cwd=Path("/r")).message
    assert len(message) > 200

    lines = _frame(tmp_path, message)

    assert "pass --force if you are sure." in _flat(lines)
    assert "pass --force" not in lines[-1]  # not squeezed into the bottom border
    assert "…" not in "\n".join(lines)


def test_a_short_notice_stays_on_the_bottom_border(tmp_path: Path) -> None:
    lines = _frame(tmp_path, "✓ Set group beta")
    assert "✓ Set group beta" in lines[-1]
    assert sum("✓ Set group beta" in ln for ln in lines) == 1


def test_a_long_notice_with_markup_characters_renders_literally(tmp_path: Path) -> None:
    notice = "✗ bad [/x] value [bold]not bold[/bold] " + "and more words " * 10
    subtitle, inline = tframe.notice_parts(notice)
    assert subtitle is None and inline is not None
    assert inline.plain == notice and not inline.spans
