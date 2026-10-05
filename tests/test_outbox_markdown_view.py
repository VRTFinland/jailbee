from __future__ import annotations

import io
import re

from rich.text import Text

from jailbee.outbox.markdown_view import AnsiLine, print_lines, render_markdown


def plain(lines: list[str]) -> list[str]:
    """What a terminal would show: the lines with their style codes interpreted away."""
    return [Text.from_ansi(line).plain for line in lines]


LONG = "word " * 40


def test_non_terminal_returns_text_verbatim(monkeypatch) -> None:
    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: None)
    assert render_markdown("# T\n\n" + LONG, indent="  ") == ["  # T", "  ", "  " + LONG]


def test_paragraph_wraps_to_width() -> None:
    lines = plain(render_markdown(LONG, width=40))
    assert len(lines) > 1
    assert all(len(line) <= 40 for line in lines)


def test_indent_counts_against_width() -> None:
    lines = plain(render_markdown(LONG, indent="    ", width=40))
    assert all(line.startswith("    ") and len(line) <= 40 for line in lines)


def test_list_continuation_is_indented() -> None:
    lines = plain(render_markdown("- " + LONG, width=40))
    assert lines[0].lstrip().startswith("•")
    assert all(line.startswith("   ") for line in lines[1:])


def test_code_block_content_is_kept() -> None:
    assert any("abc def" in line for line in plain(render_markdown("```\nabc def\n```", width=40)))


def test_styled_output_is_marked_and_coloured(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("NO_COLOR", raising=False)
    lines = render_markdown("# T\n\n**bold** `code`", width=40)
    assert all(isinstance(line, AnsiLine) for line in lines if line)
    joined = "\n".join(lines)
    assert "\x1b[1m" in joined  # bold
    assert re.search(r"\x1b\[[\d;]*\b(3[0-7]|38)\b", joined)  # a foreground colour, not just bold


def test_no_hyperlink_escapes_and_url_stays_visible() -> None:
    lines = render_markdown("[x](https://e.com)", width=60)
    assert not any("\x1b]8" in line for line in lines)
    assert "https://e.com" in "\n".join(plain(lines))


def test_escapes_in_the_body_never_reach_the_output() -> None:
    lines = render_markdown("hi \x1b]8;;http://evil\x07there \x1b[31mred", width=60)
    out = "\n".join(plain(lines))
    assert "\x1b" not in out and "\x07" not in out
    assert "evil" in out  # only the control characters are dropped, not the text
    assert "\x1b]8" not in "\n".join(lines)


def test_empty_text_and_no_leading_or_trailing_blank_lines() -> None:
    assert render_markdown("", width=40) == []
    lines = render_markdown("- a\n\n- b", width=40)
    assert lines[0] and lines[-1]


def _capture_console(monkeypatch) -> io.StringIO:
    from rich.console import Console

    buffer = io.StringIO()
    monkeypatch.setattr(
        "jailbee.tui.console",
        Console(file=buffer, force_terminal=True, color_system="standard", width=80),
    )
    return buffer


def test_print_lines_prints_styled_lines_with_their_styling(monkeypatch) -> None:
    buffer = _capture_console(monkeypatch)
    print_lines([AnsiLine("\x1b[1mstyled\x1b[0m")])
    assert "\x1b[1mstyled" in buffer.getvalue()


def test_print_lines_strips_escapes_and_markup_from_plain_lines(monkeypatch) -> None:
    buffer = _capture_console(monkeypatch)
    print_lines(["plain \x1b[31mred\x1b[0m [bold]tag[/bold]"])
    out = buffer.getvalue()
    assert "\x1b[31m" not in out and "\x1b[1m" not in out
    assert "[bold]tag[/bold]" in out


def test_print_lines_does_not_trust_a_demoted_styled_line(monkeypatch) -> None:
    buffer = _capture_console(monkeypatch)
    print_lines([f"{AnsiLine(chr(27) + '[31mx')}"])  # an f-string gives back a plain str
    assert "\x1b[31m" not in buffer.getvalue()


def test_html_is_shown_as_text_not_dropped() -> None:
    out = "\n".join(
        plain(render_markdown("a\n\n<div>\nsecret http://evil\n</div>\n\nb x <i>y</i>", width=60))
    )
    assert "<div>" in out and "http://evil" in out and "<i>y</i>" in out


def test_image_url_is_shown() -> None:
    out = "\n".join(plain(render_markdown("![logo](http://track/p.png)", width=60)))
    assert "logo" in out and "http://track/p.png" in out


def test_image_keeps_its_place_in_the_line() -> None:
    assert plain(render_markdown("before ![y](http://t/y) after", width=60)) == [
        "before ![y](http://t/y) after"
    ]


def test_image_in_a_table_cell_stays_in_the_cell() -> None:
    out = "\n".join(plain(render_markdown("| a |\n|---|\n| ![i](http://t/i) |", width=60)))
    assert out.index("![i](http://t/i)") > out.index("──")


def test_entities_cannot_reintroduce_format_or_control_characters() -> None:
    lines = render_markdown("a &#x202e;b &#x200b;c &#x7;d &#27;[31me", width=60)
    out = "\n".join(plain(lines))
    assert not any(ch in out for ch in "\u202e\u200b\x07")
    assert "\x1b" not in out
    assert "\x1b[31m" not in "\n".join(lines)


def test_empty_body_renders_nothing_with_or_without_a_terminal(monkeypatch) -> None:
    assert render_markdown("", width=40) == []
    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: None)
    assert render_markdown("") == []


def test_pr_show_lines_renders_bodies_and_leaves_headings_alone(monkeypatch) -> None:
    from jailbee.pr_outbox import parse_manifest
    from tests.test_pr_outbox import _manifest_text

    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: 40)
    manifest = parse_manifest(
        "001-x.json",
        _manifest_text(actions=[{"type": "comment", "body": LONG}]),
        {},
    )
    from jailbee.pr_outbox import show_lines

    lines = show_lines(manifest)
    body = plain(lines[3:])
    assert len(body) > 1 and all(len(line) <= 40 for line in body)
    assert "COMMENT (general)" in lines[2]


def test_non_terminal_stdout_is_not_a_terminal_width(monkeypatch) -> None:
    from jailbee.outbox import markdown_view

    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    assert markdown_view._terminal_width() is None


def test_issue_prose_wraps_bodies_but_not_titles(monkeypatch) -> None:
    from jailbee import issue_outbox

    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: 40)
    body = plain(issue_outbox._prose("body", LONG, markdown=True))
    before = issue_outbox._prose("body before", LONG)
    title = issue_outbox._prose("title", LONG)
    assert len(body) > 3 and all(len(line) <= 40 for line in body)
    assert before == ["  body before:", f"    {LONG}"]  # an edit's before/after stay verbatim
    assert title == ["  title:", f"    {LONG}"]


def test_render_width_follows_the_color_choice(monkeypatch) -> None:
    from jailbee.outbox import markdown_view

    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: 70)
    assert markdown_view.render_width(None) == 70
    assert markdown_view.render_width(True) == 70
    assert markdown_view.render_width(False) is None
    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: None)
    assert markdown_view.render_width(None) is None
    assert markdown_view.render_width(False) is None


def test_forced_render_width_is_the_terminal_stderr_is_on(monkeypatch) -> None:
    import os

    from jailbee.outbox import markdown_view

    monkeypatch.setattr("jailbee.outbox.markdown_view._terminal_width", lambda: None)
    monkeypatch.setattr(
        "jailbee.outbox.markdown_view.os.get_terminal_size",
        lambda fd: os.terminal_size((55, 20)),
    )
    assert markdown_view.render_width(True) == 55

    def no_terminal(fd: int) -> os.terminal_size:
        raise OSError

    monkeypatch.setattr("jailbee.outbox.markdown_view.os.get_terminal_size", no_terminal)
    monkeypatch.setenv("COLUMNS", "66")
    assert markdown_view.render_width(True) == 66


def test_print_lines_color_keeps_styling_off_a_terminal(monkeypatch) -> None:
    from rich.console import Console

    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "xterm-256color")
    buffer = io.StringIO()
    monkeypatch.setattr("jailbee.tui.console", Console(file=buffer, force_terminal=False))
    print_lines([AnsiLine("\x1b[1mstyled\x1b[0m"), "plain \x1b[31mred"])
    assert "\x1b[1m" not in buffer.getvalue()
    print_lines([AnsiLine("\x1b[1mstyled\x1b[0m"), "plain \x1b[31mred"], color=True)
    out = buffer.getvalue()
    assert "\x1b[1mstyled" in out
    assert "\x1b[31m" not in out
