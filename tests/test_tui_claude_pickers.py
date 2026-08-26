"""Tests for the Claude account pickers."""

from __future__ import annotations

from jailbee import tui


def _row(mocker, *, number, label, blocked=None, mine=False):
    row = mocker.MagicMock()
    row.account.number = number
    row.account.label = label
    row.account.email = f"{label}@example.com"
    row.account.five_hour_pct = 10.0
    row.account.seven_day_pct = 20.0
    row.blocked_reason = blocked
    row.mine = mine
    return row


def test_the_use_picker_disables_a_blocked_row_with_its_reason(mocker):
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "2"
    rows = [
        _row(mocker, number=1, label="work"),
        _row(mocker, number=2, label="personal", blocked="held by otherrepo"),
    ]

    tui.pick_claude_account(rows)

    choices = select.call_args.kwargs["choices"]
    assert choices[0].disabled is None
    assert choices[1].disabled == "held by otherrepo"


def test_the_use_picker_returns_the_slot_as_a_string(mocker):
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = "2"

    result = tui.pick_claude_account([_row(mocker, number=2, label="personal")])

    assert result == "2"


def test_the_use_picker_returns_none_on_cancel(mocker):
    select = mocker.patch("questionary.select")
    select.return_value.ask.return_value = None

    assert tui.pick_claude_account([_row(mocker, number=1, label="work")]) is None


def test_the_allow_picker_never_falls_back_to_the_pointed_row(mocker):
    """Unchecking everything must mean "no restriction", not "this one"."""
    checkbox = mocker.patch("jailbee.tui.checkbox", return_value=[])
    rows = [_row(mocker, number=1, label="work"), _row(mocker, number=2, label="personal")]

    result = tui.pick_claude_accounts_multi(rows, checked={"1"})

    assert checkbox.call_args.kwargs["default_to_pointed"] is False
    assert result == []


def test_the_allow_picker_pre_checks_the_current_list(mocker):
    checkbox = mocker.patch("jailbee.tui.checkbox", return_value=["1"])
    rows = [_row(mocker, number=1, label="work"), _row(mocker, number=2, label="personal")]

    tui.pick_claude_accounts_multi(rows, checked={"1"})

    choices = checkbox.call_args.kwargs["choices"]
    assert choices[0].checked is True
    assert choices[1].checked is False


def test_the_allow_picker_returns_none_on_cancel(mocker):
    mocker.patch("jailbee.tui.checkbox", return_value=None)

    assert tui.pick_claude_accounts_multi([], checked=set()) is None
