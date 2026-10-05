"""Tests for jailbee.prompting — the missing-value policy."""

from __future__ import annotations

import pytest
import typer
from typer._click.exceptions import ClickException
from typer.testing import CliRunner

from jailbee import prompting
from jailbee.prompting import Cancelled, MissingValue, Option, ask_text, choose_one
from tests.conftest import panel_text

A = Option("a-full", "a  (title)", "a")
B = Option("b-full", "b  (title)", "b")


def _yes() -> bool:
    return True


def _no() -> bool:
    return False


def test_missing_value_from_a_typer_command_exits_2_with_the_message():
    demo = typer.Typer()

    @demo.command()
    def go() -> None:
        raise MissingValue("tag", candidates=["s1", "s2"])

    @demo.command()
    def other() -> None:  # a second command keeps Typer in group mode, like jailbee
        pass

    result = CliRunner().invoke(demo, ["go"])
    assert result.exit_code == 2
    assert "Candidates: s1, s2" in panel_text(result.stderr)
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_is_interactive_needs_a_tty(mocker, monkeypatch):
    monkeypatch.delenv("JAILBEE_NONINTERACTIVE", raising=False)
    mocker.patch("jailbee.prompting.sys.stdin.isatty", return_value=False)
    assert prompting.is_interactive() is False


def test_is_interactive_honours_the_env_override_on_a_tty(mocker, monkeypatch):
    mocker.patch("jailbee.prompting.sys.stdin.isatty", return_value=True)
    monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    assert prompting.is_interactive() is False
    monkeypatch.delenv("JAILBEE_NONINTERACTIVE")
    assert prompting.is_interactive() is True


def test_exceptions_are_click_exceptions_with_the_policy_exit_codes():
    assert issubclass(MissingValue, ClickException)
    assert not issubclass(MissingValue, ValueError)
    assert MissingValue("tag").exit_code == 2
    assert Cancelled().exit_code == 1
    assert Cancelled().message == "cancelled"


def test_no_options_is_missing_with_the_reason():
    with pytest.raises(MissingValue) as exc:
        choose_one("snapshot", [], empty_reason="no snapshots in feat-x", is_interactive=_yes)
    assert exc.value.message == "no snapshots in feat-x"


def test_one_option_is_taken_with_a_stderr_line(capsys):
    picker = pytest.fail  # must not run
    assert choose_one("container", [A], picker=picker, is_interactive=_yes) == "a-full"
    out, err = capsys.readouterr()
    assert out == ""
    assert "Using container a" in err


def test_one_option_destructive_still_asks():
    seen: list[list[str]] = []

    def picker(opts):
        seen.append([o.label for o in opts])
        return opts[0].value

    assert choose_one("tag", [A], destructive=True, picker=picker, is_interactive=_yes) == "a-full"
    assert seen == [["a"]]


def test_one_option_destructive_off_a_tty_is_missing():
    with pytest.raises(MissingValue) as exc:
        choose_one("tag", [A], destructive=True, is_interactive=_no)
    assert exc.value.candidates == ("a",)


def test_many_options_off_a_tty_name_the_candidates():
    with pytest.raises(MissingValue) as exc:
        choose_one("container", [A, B], alternative="--all", is_interactive=_no)
    msg = exc.value.message
    assert "missing container" in msg
    assert "--all" in msg
    assert msg.endswith("Candidates: a, b")


def test_many_options_on_a_tty_use_the_picker():
    assert (
        choose_one("container", [A, B], picker=lambda o: o[1].value, is_interactive=_yes)
        == "b-full"
    )


def test_picker_returning_none_is_cancelled():
    with pytest.raises(Cancelled):
        choose_one("container", [A, B], picker=lambda o: None, is_interactive=_yes)


def test_default_picker_is_the_select_seam(mocker):
    sel = mocker.patch("jailbee.prompting._select", return_value="b-full")
    assert choose_one("container", [A, B], is_interactive=_yes) == "b-full"
    assert sel.call_args.args[0] == "container"


def test_default_predicate_is_looked_up_at_call_time(mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    with pytest.raises(MissingValue):
        choose_one("container", [A, B])


def test_ask_text_off_a_tty_is_missing():
    with pytest.raises(MissingValue) as exc:
        ask_text("port", validate=lambda s: None, is_interactive=_no)
    assert exc.value.candidates == ()
    assert "missing port" in exc.value.message


def test_ask_text_re_asks_until_valid(mocker, capsys):
    mocker.patch("jailbee.prompting._ask", side_effect=["x", "8080"])
    got = ask_text(
        "port", validate=lambda s: None if s.isdigit() else "not a number", is_interactive=_yes
    )
    assert got == "8080"
    assert "not a number" in capsys.readouterr().err


def test_ask_text_ctrl_c_is_cancelled(mocker):
    mocker.patch("jailbee.prompting._ask", return_value=None)
    with pytest.raises(Cancelled):
        ask_text("port", validate=lambda s: None, is_interactive=_yes)


def _fake_select(mocker, pick):
    """Patch questionary.select; `pick(choices)` is what .ask() answers."""
    seen: dict[str, object] = {}

    def select(message, choices, **kwargs):
        seen["choices"] = choices
        seen["kwargs"] = kwargs
        return mocker.Mock(ask=lambda: pick(choices))

    mocker.patch("questionary.select", side_effect=select)
    return seen


def test_select_cancel_row_returns_none_and_choose_one_cancels(mocker):
    seen = _fake_select(mocker, lambda choices: choices[-1].value)
    assert prompting._select("container", [A, B]) is None
    cancel = seen["choices"][-1]
    assert cancel.title == "cancel"
    assert cancel.value is not None  # questionary answers value=None with the title
    with pytest.raises(Cancelled):
        choose_one("container", [A, B], is_interactive=_yes)


def test_select_returns_the_chosen_options_value(mocker):
    _fake_select(mocker, lambda choices: choices[1].value)
    assert prompting._select("container", [A, B]) == "b-full"


def test_select_escape_is_none(mocker):
    _fake_select(mocker, lambda choices: None)
    assert prompting._select("container", [A, B]) is None


def test_select_with_many_options_does_not_use_shortcuts(mocker):
    options = [Option(i, f"t{i}", f"l{i}") for i in range(40)]
    seen = _fake_select(mocker, lambda choices: choices[0].value)
    assert prompting._select("thing", options) == 0
    assert len(seen["choices"]) == 41
    assert seen["kwargs"]["use_shortcuts"] is False


def test_select_real_questionary_accepts_36_options_without_shortcuts():
    import questionary

    choices = [questionary.Choice(title=str(i), value=i) for i in range(37)]
    questionary.select("x", choices=choices, use_shortcuts=False)  # must not raise


def test_ask_returns_the_typed_text_or_none(mocker):
    text = mocker.patch("questionary.text")
    text.return_value.ask.return_value = "hello"
    assert prompting._ask("port", None) == "hello"
    text.return_value.ask.return_value = None
    assert prompting._ask("port", None) is None


def test_ask_keeps_acronyms_in_the_prompt(mocker):
    text = mocker.patch("questionary.text")
    text.return_value.ask.return_value = "x"
    prompting._ask("GitHub URL", None)
    assert text.call_args.args[0] == "GitHub URL:"


def test_ask_text_off_a_tty_names_the_flag_and_says_type():
    with pytest.raises(MissingValue) as exc:
        ask_text("GitHub URL", validate=lambda s: None, alternative="--url", is_interactive=_no)
    assert exc.value.message == (
        "missing GitHub URL; pass it explicitly or use --url, or run in a terminal to type it"
    )


def test_ask_text_without_an_alternative_still_says_type():
    with pytest.raises(MissingValue) as exc:
        ask_text("port", validate=lambda s: None, is_interactive=_no)
    assert exc.value.message == "missing port; pass it explicitly, or run in a terminal to type it"


def test_stdout_is_terminal_follows_stdout(mocker):
    mocker.patch("jailbee.prompting.sys.stdout.isatty", return_value=False)
    assert prompting.stdout_is_terminal() is False
    mocker.patch("jailbee.prompting.sys.stdout.isatty", return_value=True)
    assert prompting.stdout_is_terminal() is True


def test_confirm_returns_the_answer(mocker):
    ask = mocker.patch("jailbee.prompting._confirm", return_value=False)
    assert prompting.confirm("Move it?", default=True) is False
    ask.assert_called_once_with("Move it?", True)


def test_confirm_passes_the_default_through(mocker):
    ask = mocker.patch("jailbee.prompting._confirm", return_value=True)
    assert prompting.confirm("Move it?", default=False) is True
    ask.assert_called_once_with("Move it?", False)


def test_confirm_cancel_raises_cancelled(mocker):
    mocker.patch("jailbee.prompting._confirm", return_value=None)
    with pytest.raises(Cancelled):
        prompting.confirm("Move it?")


def test_ask_with_completions_uses_autocomplete(mocker):
    auto = mocker.patch("questionary.autocomplete")
    auto.return_value.ask.return_value = "develop"
    assert prompting._ask("base branch", None, ["main", "develop"]) == "develop"
    assert auto.call_args.args[0] == "Base branch:"
    assert auto.call_args.kwargs["choices"] == ["main", "develop"]
    assert auto.call_args.kwargs["match_middle"] is True


def test_ask_with_no_completions_falls_back_to_plain_text(mocker):
    """questionary.autocomplete refuses an empty choice list."""
    auto = mocker.patch("questionary.autocomplete")
    text = mocker.patch("questionary.text")
    text.return_value.ask.return_value = "x"
    assert prompting._ask("base branch", None, []) == "x"
    auto.assert_not_called()


def test_ask_text_passes_completions_through(mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    ask = mocker.patch("jailbee.prompting._ask", return_value="main")
    assert (
        prompting.ask_text("base branch", validate=lambda _t: None, completions=["main"]) == "main"
    )
    ask.assert_called_once_with("base branch", None, ["main"])
