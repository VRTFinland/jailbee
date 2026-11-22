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


def _links(lines: list[str]) -> list[str]:
    return [m for line in lines for m in re.findall(r"\x1b\]8;[^;]*;([^\x1b]+)\x1b\\", line)]


def test_http_link_becomes_an_osc8_hyperlink() -> None:
    lines = render_markdown("[https://e.com](https://e.com)", width=60)
    assert _links(lines) == ["https://e.com"]
    assert plain(lines) == ["https://e.com"]


def test_https_link_with_a_different_label_shows_the_host() -> None:
    lines = render_markdown("[docs](https://Example.com/a?b=1)", width=60)
    assert _links(lines) == ["https://Example.com/a?b=1"]
    assert plain(lines) == ["docs → example.com"]


def test_label_naming_another_host_cannot_disguise_the_target() -> None:
    out = "\n".join(plain(render_markdown("[github.com](https://evil.example/x)", width=60)))
    assert "evil.example" in out


def test_label_cannot_imitate_the_host_suffix() -> None:
    out = plain(render_markdown("[docs (github.com)](https://evil.com)", width=60))
    assert out == ["docs (github.com) → evil.com"]


def test_userinfo_cannot_pose_as_the_host() -> None:
    for md in (
        "<https://github.com@evil.com/x>",
        "[https://github.com@evil.com](https://github.com@evil.com)",
    ):
        out = "\n".join(plain(render_markdown(md, width=80)))
        assert out.endswith("→ evil.com"), md


def test_bare_origin_label_gets_no_suffix() -> None:
    assert plain(render_markdown("[https://E.com/](https://e.com/)", width=60)) == [
        "https://E.com/"
    ]


def test_uppercase_scheme_is_linked_and_the_target_not_repeated() -> None:
    lines = render_markdown("<HTTPS://E.com>", width=60)
    assert _links(lines) == ["https://E.com"]
    assert plain(lines) == ["HTTPS://E.com"]


def test_ipv6_literal_keeps_a_working_target_and_the_right_host() -> None:
    lines = render_markdown("[x](https://[::1]/)", width=60)
    assert _links(lines) == ["https://[::1]/"]
    assert plain(lines) == ["x → ::1"]


def test_non_http_scheme_that_survives_the_parser_is_text_with_the_target() -> None:
    # markdown-it's validateLink rejects javascript:, vbscript:, file: and most data:
    # before any link token exists, so only schemes like these reach the flattening.
    for href in ("ftp://h/x", "mailto:a@b.c", "tel:+358"):
        lines = render_markdown(f"[click]({href})", width=60)
        assert not any("\x1b]8" in line for line in lines), href
        assert plain(lines) == [f"click → {href}"]


def test_scheme_the_parser_rejects_is_never_linked() -> None:
    for href in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"):
        lines = render_markdown(f"[click]({href})", width=60)
        assert not any("\x1b]8" in line for line in lines), href


def test_raw_controls_in_a_target_never_reach_the_output() -> None:
    for href in ("https://e.com/%E2%80%AE", "https://e.com/\u202ex", "https://e.com/a\x07b"):
        lines = render_markdown(f"[t](<{href}>)", width=60)
        joined = "\n".join(lines).replace("\x1b\\", "")
        assert "\x07" not in joined and "\u202e" not in joined, href
        assert all("\u202e" not in u and "\x07" not in u for u in _links(lines)), href


def test_port_or_userinfo_in_the_target_is_shown_despite_a_matching_label() -> None:
    for target in ("https://e.com:8443/", "https://e.com:pw@e.com"):
        out = plain(render_markdown(f"[https://e.com]({target})", width=60))
        assert out == ["https://e.com \u2192 e.com"], target


def test_encoded_bracket_in_userinfo_is_left_alone() -> None:
    assert _links(render_markdown("<https://a%5Bb@e.com/>", width=60)) == ["https://a%5Bb@e.com/"]


def test_link_host_guard_rejects_raw_controls_and_non_ascii() -> None:
    from jailbee.outbox.markdown_view import _link_host

    assert _link_host("https://e.com/x") == "e.com"
    for url in (
        "https://e.com/\x07",
        "https://e.com/\x1b[31m",
        "https://e.com/‮",
        "https://e.com/é",
        "https://e.com/a b",
        "https://e.com/\n",
        "ftp://e.com/",
    ):
        assert _link_host(url) is None, repr(url)


def test_safe_style_drops_an_unsafe_link_and_keeps_a_safe_one() -> None:
    from rich.style import Style

    from jailbee.outbox.markdown_view import _safe_style

    assert _safe_style(None) is None
    unsafe = _safe_style(Style(link="javascript:x"))
    assert unsafe is not None and unsafe.link is None
    control = _safe_style(Style(bold=True, link="https://e.com/\x07"))
    assert control is not None and control.link is None
    kept = _safe_style(Style(bold=True, link="https://e.com/"))
    assert kept is not None and kept.link == "https://e.com/" and kept.bold


def test_wrapped_link_keeps_the_whole_target_on_every_line() -> None:
    url = "https://example.com/" + "a" * 70
    links = _links(render_markdown(f"<{url}>", width=30))
    assert len(links) > 1 and set(links) == {url}


def test_markdown_text_lines_carry_the_link_style() -> None:
    from jailbee.outbox.markdown_view import markdown_text_lines

    good = markdown_text_lines("[a](https://e.com) [b](javascript:x)", 60)[0]
    links = {
        span.style.link for span in good.spans if hasattr(span.style, "link") and span.style.link
    }
    assert links == {"https://e.com"}


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


def test_no_hyperlinks_switch_turns_outbox_links_into_text(monkeypatch) -> None:
    monkeypatch.setenv("JAILBEE_NO_HYPERLINKS", "1")
    lines = render_markdown("[https://e.com](https://e.com)", width=60)
    assert _links(lines) == []
    assert plain(lines) == ["https://e.com"]


def test_no_hyperlinks_switch_covers_the_panel_lines(monkeypatch) -> None:
    from jailbee.outbox.markdown_view import markdown_text_lines

    def has_link() -> bool:
        return any(
            span.style and getattr(span.style, "link", None)
            for line in markdown_text_lines("[x](https://e.com)", 60)
            for span in line.spans
        )

    monkeypatch.delenv("JAILBEE_NO_HYPERLINKS", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert has_link()
    monkeypatch.setenv("JAILBEE_NO_HYPERLINKS", "1")
    assert not has_link()
