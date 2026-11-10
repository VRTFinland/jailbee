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


@pytest.mark.parametrize("answer", ["b-full", None])
def test_choose_returns_value_and_none_on_cancel(mocker, answer):
    select = mocker.patch("jailbee.prompting._select_question", return_value=answer, create=True)
    interactive = mocker.patch("jailbee.prompting.is_interactive", side_effect=AssertionError)
    assert prompting.choose("Publish?", [A, B]) == answer
    select.assert_called_once_with("Publish?", [A, B])
    interactive.assert_not_called()


@pytest.mark.parametrize("keys, expected", [("\r", None), ("\x1b", None), ("\x1b[A\r", "b-full")])
def test_choose_real_picker_defaults_to_cancel_and_honors_escape(keys, expected):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        assert prompting.choose("Publish?", [A, B]) == expected


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


def test_select_preselects_the_default_row(mocker):
    seen = _fake_select(mocker, lambda choices: choices[0].value)
    prompting._select("container", [A, B], default="b-full")
    assert seen["kwargs"]["default"].title == B.title


def test_select_without_a_default_preselects_nothing(mocker):
    seen = _fake_select(mocker, lambda choices: choices[0].value)
    prompting._select("container", [A, B])
    assert "default" not in seen["kwargs"]


def test_choose_one_hands_the_default_to_the_terminal_picker(mocker):
    select = mocker.patch("jailbee.prompting._select", return_value="b-full")
    assert choose_one("container", [A, B], is_interactive=_yes, default="b-full") == "b-full"
    assert select.call_args.kwargs["default"] == "b-full"


def test_choose_one_without_default_keeps_the_two_argument_seam(mocker):
    select = mocker.patch("jailbee.prompting._select", return_value="b-full")
    choose_one("container", [A, B], is_interactive=_yes)
    select.assert_called_once_with("container", [A, B])


def test_choose_one_default_is_ignored_off_a_terminal():
    with pytest.raises(MissingValue):
        choose_one("container", [A, B], is_interactive=lambda: False, default="b-full")


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


@pytest.mark.parametrize("completions", [None, ["login"]])
def test_ask_clear_shortcut_exits_with_empty_text(mocker, completions):
    factory = mocker.patch("questionary.autocomplete" if completions else "questionary.text")
    factory.return_value.ask.return_value = "login"
    assert prompting._ask("alias", "login", completions, clear_shortcut=True) == "login"
    kwargs = factory.call_args.kwargs
    assert "Ctrl-R" in factory.call_args.args[0]
    # Ordinary empty submissions must not masquerade as the clear shortcut.
    assert not kwargs["validate"]("")
    assert not kwargs["validate"]("   ")
    assert kwargs["validate"]("login")
    bindings = kwargs["key_bindings"].get_bindings_for_keys(("c-r",))
    assert len(bindings) == 1
    event = mocker.Mock()
    bindings[0].handler(event)
    event.app.exit.assert_called_once_with(result="")


def test_ask_text_clear_shortcut_bypasses_empty_validation(mocker):
    mocker.patch("jailbee.prompting._ask", return_value="")
    assert (
        ask_text(
            "alias",
            validate=lambda text: "empty" if not text else None,
            clear_shortcut=True,
            is_interactive=_yes,
        )
        == ""
    )


def test_ask_text_without_shortcut_rejects_empty_text(mocker):
    mocker.patch("jailbee.prompting._ask", side_effect=["", "login"])
    assert (
        ask_text(
            "alias",
            validate=lambda text: "empty" if not text else None,
            is_interactive=_yes,
        )
        == "login"
    )


def test_ask_text_passes_completions_through(mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    ask = mocker.patch("jailbee.prompting._ask", return_value="main")
    assert (
        prompting.ask_text("base branch", validate=lambda _t: None, completions=["main"]) == "main"
    )
    ask.assert_called_once_with("base branch", None, ["main"])


# --- the backend seam and the full question-kind API -------------------------

C = Option("c-full", "c  (title)", "c")


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.delenv("JAILBEE_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("jailbee.prompting.is_interactive", lambda: True)


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr("jailbee.prompting.is_interactive", lambda: False)


def test_choose_many_returns_the_ticked_values(mocker, tty):
    pick = mocker.patch("jailbee.prompting._select_many", return_value=["a-full", "c-full"])
    assert prompting.choose_many("containers", [A, B, C]) == ["a-full", "c-full"]
    assert pick.call_args.args[0] == "containers"


def test_choose_many_empty_selection_is_an_answer_not_a_cancel(mocker, tty):
    mocker.patch("jailbee.prompting._select_many", return_value=[])
    assert prompting.choose_many("containers", [A, B]) == []


def test_choose_many_cancel_raises_cancelled(mocker, tty):
    mocker.patch("jailbee.prompting._select_many", return_value=None)
    with pytest.raises(Cancelled):
        prompting.choose_many("containers", [A, B])


def test_choose_many_off_a_tty_names_the_candidates(no_tty):
    with pytest.raises(MissingValue) as exc:
        prompting.choose_many("containers", [A, B])
    assert exc.value.exit_code == 2
    assert "a, b" in exc.value.format_message()


def test_choose_many_with_one_option_still_asks(mocker, tty):
    pick = mocker.patch("jailbee.prompting._select_many", return_value=["a-full"])
    assert prompting.choose_many("containers", [A]) == ["a-full"]
    pick.assert_called_once()


def test_choose_many_no_options_is_missing_with_the_reason(tty):
    with pytest.raises(MissingValue, match="nothing here"):
        prompting.choose_many("containers", [], empty_reason="nothing here")


def test_secret_returns_the_typed_text(mocker, tty):
    mocker.patch("jailbee.prompting._ask_secret", return_value="hunter2")
    assert prompting.ask_secret("token") == "hunter2"


def test_secret_cancel_raises_cancelled(mocker, tty):
    mocker.patch("jailbee.prompting._ask_secret", return_value=None)
    with pytest.raises(Cancelled):
        prompting.ask_secret("token")


def test_secret_off_a_tty_is_missing_and_never_asks(mocker, no_tty):
    ask = mocker.patch("jailbee.prompting._ask_secret")
    with pytest.raises(MissingValue, match="token"):
        prompting.ask_secret("token", alternative="--token")
    ask.assert_not_called()


def test_ask_confirm_returns_the_answer_and_passes_default(mocker, tty):
    ask = mocker.patch("jailbee.prompting._confirm", return_value=True)
    assert prompting.ask_confirm("Go?", default=False) is True
    assert ask.call_args.args == ("Go?", False)


def test_ask_confirm_cancel_raises_cancelled(mocker, tty):
    mocker.patch("jailbee.prompting._confirm", return_value=None)
    with pytest.raises(Cancelled):
        prompting.ask_confirm("Go?")


def test_ask_confirm_off_a_tty_is_missing_not_a_silent_default(mocker, no_tty):
    ask = mocker.patch("jailbee.prompting._confirm")
    with pytest.raises(MissingValue) as exc:
        prompting.ask_confirm("Destroy it?", alternative="--yes")
    assert exc.value.exit_code == 2
    assert "--yes" in exc.value.format_message()
    ask.assert_not_called()


def test_destructive_confirm_defaults_to_no_even_if_asked_for_yes(mocker, tty):
    ask = mocker.patch("jailbee.prompting._confirm", return_value=False)
    prompting.ask_confirm("Destroy it?", default=True, destructive=True)
    assert ask.call_args.args == ("Destroy it?", False)


def test_noninteractive_env_beats_a_tty_for_every_kind(mocker, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    for name in ("_confirm", "_select", "_select_many", "_ask", "_ask_secret"):
        mocker.patch(f"jailbee.prompting.{name}", side_effect=AssertionError(name))
    calls = [
        lambda: prompting.ask_confirm("Go?"),
        lambda: choose_one("x", [A, B]),
        lambda: prompting.choose_many("x", [A, B]),
        lambda: ask_text("x", validate=lambda s: None),
        lambda: prompting.ask_secret("x"),
    ]
    for call in calls:
        with pytest.raises(MissingValue):
            call()


def test_selection_follows_is_interactive_at_call_time(mocker, monkeypatch):
    monkeypatch.delenv("JAILBEE_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("jailbee.prompting.is_interactive", lambda: True)
    mocker.patch("jailbee.prompting._confirm", return_value=True)
    assert prompting.ask_confirm("Go?") is True
    monkeypatch.setattr("jailbee.prompting.is_interactive", lambda: False)
    with pytest.raises(MissingValue):
        prompting.ask_confirm("Go?")


def test_context_is_accepted_by_every_kind(mocker, tty):
    ctx = prompting.PromptContext(repo="r", container="c1")
    mocker.patch("jailbee.prompting._confirm", return_value=True)
    mocker.patch("jailbee.prompting._select", return_value="a-full")
    mocker.patch("jailbee.prompting._select_many", return_value=[])
    mocker.patch("jailbee.prompting._ask", return_value="x")
    mocker.patch("jailbee.prompting._ask_secret", return_value="s")
    assert prompting.ask_confirm("Go?", context=ctx) is True
    assert choose_one("x", [A, B], context=ctx) == "a-full"
    assert prompting.choose_many("x", [A, B], context=ctx) == []
    assert ask_text("x", validate=lambda s: None, context=ctx) == "x"
    assert prompting.ask_secret("x", context=ctx) == "s"


def test_a_backend_receives_the_context_and_a_custom_one_can_be_selected(monkeypatch):
    seen = {}

    class Fake:
        def confirm(self, question, *, default, destructive, context, alternative):
            seen["ctx"] = context
            return True

    monkeypatch.setattr("jailbee.prompting.select_backend", lambda: Fake())
    ctx = prompting.PromptContext(container="c1")
    assert prompting.ask_confirm("Go?", context=ctx) is True
    assert seen["ctx"] == ctx


def test_option_description_is_optional():
    assert Option("v", "t", "l").description == ""
    assert Option("v", "t", "l", "d").description == "d"


def test_default_secret_prompt_hides_input_and_maps_cancel(mocker):
    pw = mocker.patch("questionary.password", return_value=mocker.Mock(ask=lambda: None))
    assert prompting._ask_secret("API token") is None
    assert pw.call_args.args[0] == "API token:"
    mocker.patch("questionary.password", return_value=mocker.Mock(ask=lambda: "s3"))
    assert prompting._ask_secret("token") == "s3"


def test_default_multi_picker_returns_values_and_none_on_cancel(mocker):
    seen = {}

    def fake(message, choices, **kw):
        seen["choices"] = choices
        return ["a-full"]

    mocker.patch("jailbee.tui.checkbox", side_effect=fake)
    assert prompting._select_many("things", [A, B]) == ["a-full"]
    assert [c.value for c in seen["choices"]] == ["a-full", "b-full"]
    mocker.patch("jailbee.tui.checkbox", return_value=None)
    assert prompting._select_many("things", [A, B]) is None


def test_explicit_question_is_the_terminal_header_and_the_default_row_stays(mocker, tty):
    headers = []

    def select(message, choices, **kwargs):
        headers.append(message)
        assert "default" not in kwargs  # the first row is highlighted, not Cancel
        return mocker.Mock(ask=lambda: choices[0].value)

    mocker.patch("questionary.select", side_effect=select)
    got = choose_one(
        "pending PR description",
        [A, B],
        question="More than one PR description is pending. Which one?",
    )
    assert got == "a-full"
    assert headers == ["More than one PR description is pending. Which one?"]


def test_without_a_question_the_header_is_select_noun(mocker, tty):
    headers = []

    def select(message, choices, **kwargs):
        headers.append(message)
        return mocker.Mock(ask=lambda: choices[0].value)

    mocker.patch("questionary.select", side_effect=select)
    choose_one("container", [A, B])
    assert headers == ["Select container:"]


def test_explicit_question_is_the_terminal_header_for_choose_many(mocker, tty):
    pick = mocker.patch("jailbee.tui.checkbox", return_value=[])

    prompting.choose_many("labels", [A, B], question="Which labels?")

    assert pick.call_args.args[0] == "Which labels?"
