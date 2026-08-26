"""`tui.hint` — advisory output, on stderr, never reinterpreted as markup."""

from __future__ import annotations


def test_hint_writes_to_stderr_not_stdout(capsys) -> None:
    """Hints share a terminal with machine-read output (`jailbee ls`'s table,
    `--format json`), so they must not land on stdout."""
    from jailbee.tui import hint

    hint(["first line", "second line"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "first line" in captured.err
    assert "second line" in captured.err


def test_hint_marks_only_the_first_line(capsys) -> None:
    from jailbee.tui import hint

    hint(["head", "tail"])
    err = capsys.readouterr().err
    assert err.count("⚠") == 1
    assert err.index("⚠") < err.index("head")


def test_hint_does_not_reinterpret_square_brackets(capsys) -> None:
    """A reason may legitimately contain brackets; Rich must not eat them."""
    from jailbee.tui import hint

    hint(["install.sh installs [tool]"])
    assert "[tool]" in capsys.readouterr().err


def test_hint_on_empty_input_prints_nothing(capsys) -> None:
    from jailbee.tui import hint

    hint([])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
