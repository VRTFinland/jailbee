from __future__ import annotations

from dataclasses import replace

from rich.console import Console

from jailbee.dashboard import overlays as ov


def _prompt(**kw) -> ov.TextPrompt:
    base = {"purpose": "new-branch", "title": "New container", "label": "New branch"}
    return ov.TextPrompt(**{**base, **kw})


def _type(prompt: ov.TextPrompt, text: str) -> ov.TextPrompt:
    for ch in text.encode():
        prompt, outcome = ov.handle_prompt_key(prompt, bytes([ch]))
        assert outcome == "editing"
    return prompt


def test_typing_and_backspace_edit_the_text():
    p = _type(_prompt(), "feat")
    p, _ = ov.handle_prompt_key(p, b"\x7f")
    assert p.text == "fea"


def test_multibyte_character_split_across_reads_is_reassembled():
    p = _prompt()
    p, _ = ov.handle_prompt_key(p, b"\xc3")  # first half of "ä"
    assert p.text == "" and p.pending_utf8 == b"\xc3"
    p, _ = ov.handle_prompt_key(p, b"\xa4")
    assert p.text == "ä" and p.pending_utf8 == b""


def test_pasted_multibyte_text_lands_in_one_read():
    p, outcome = ov.handle_prompt_key(_prompt(), "työ-ä".encode())
    assert (p.text, outcome) == ("työ-ä", "editing")


def test_backspace_drops_a_pending_partial_before_touching_the_text():
    p = _type(_prompt(), "ab")
    p, _ = ov.handle_prompt_key(p, b"\xc3")
    p, _ = ov.handle_prompt_key(p, b"\x7f")
    assert (p.text, p.pending_utf8) == ("ab", b"")


def test_escape_ctrl_c_and_eof_cancel():
    for key in (b"\x1b", b"\x03", b""):
        assert ov.handle_prompt_key(_type(_prompt(), "x"), key)[1] == "cancel"


def test_arrow_escape_sequences_are_ignored_not_typed():
    p, outcome = ov.handle_prompt_key(_type(_prompt(), "x"), b"\x1b[A")
    assert (p.text, outcome) == ("x", "editing")


def test_enter_on_blank_answer_stays_open_with_an_inline_error():
    for blank in ("", "   "):
        p, outcome = ov.handle_prompt_key(_type(_prompt(), blank), b"\r")
        assert outcome == "editing"
        assert p.error == "New branch cannot be empty"
    # typing clears the error again
    p, _ = ov.handle_prompt_key(p, b"a")
    assert p.error is None


def test_enter_submits_and_the_caller_trims():
    p, outcome = ov.handle_prompt_key(_type(_prompt(), " feat "), b"\n")
    assert outcome == "submit" and p.text.strip() == "feat"


def test_pr_number_validation():
    assert ov.parse_pr_number("42") == 42
    for bad in ("", "0", "-3", "4x", "١٢", "9" * 5000, "1.5"):
        assert ov.parse_pr_number(bad) is None
    p, outcome = ov.handle_prompt_key(
        _type(_prompt(purpose="new-pr", label="PR number"), "abc"), b"\r"
    )
    assert outcome == "editing" and p.error == "PR number must be a positive whole number"


def test_renderers_show_label_text_error_and_cursor():
    console = Console(width=80, record=True)
    # typing clears the error, so it is set after the text is entered
    console.print(ov.render_prompt(replace(_type(_prompt(), "abc"), error="oops")))
    text = console.export_text()
    for expected in ("New container", "New branch", "abc", "oops"):
        assert expected in text


def test_invalid_bytes_never_put_a_replacement_char_in_the_text():
    for key in (b"\xff", b"\xa4", b"\xff\xfe"):
        text, pending = ov.decode_input(b"", key)
        assert "�" not in text
        assert (text, pending) == ("", b"")


def test_invalid_byte_before_a_partial_keeps_the_partial():
    # The invalid \xff is dropped; the trailing \xc3 is held and completes to "ä".
    text, pending = ov.decode_input(b"", b"\xff\xc3")
    assert (text, pending) == ("", b"\xc3")
    assert ov.decode_input(pending, b"\xa4") == ("ä", b"")


def test_valid_prefix_is_shown_immediately_with_a_partial_tail():
    assert ov.decode_input(b"", b"a\xc3") == ("a", b"\xc3")
    p, _ = ov.handle_prompt_key(_prompt(), b"a\xc3")
    assert (p.text, p.pending_utf8) == ("a", b"\xc3")
    p, _ = ov.handle_prompt_key(p, b"\x7f")  # drops only the partial
    assert (p.text, p.pending_utf8) == ("a", b"")


def test_four_byte_character_reassembles_from_any_split():
    raw = "😀".encode()
    for cut in (1, 2, 3):
        p, _ = ov.handle_prompt_key(_prompt(), raw[:cut])
        assert p.text == "" and p.pending_utf8 == raw[:cut]
        p, _ = ov.handle_prompt_key(p, raw[cut:])
        assert (p.text, p.pending_utf8) == ("😀", b"")


def test_pending_bytes_followed_by_ascii_do_not_wedge_the_prompt():
    p, _ = ov.handle_prompt_key(_prompt(), b"\xc3")
    p, _ = ov.handle_prompt_key(p, b"b")
    assert (p.text, p.pending_utf8) == ("b", b"")
    p, _ = ov.handle_prompt_key(p, b"c")
    assert p.text == "bc"


def _rows(n: int) -> list[str]:
    return [f"row {i}" for i in range(n)]


def test_window_lines_keeps_a_list_that_fits_or_has_no_limit():
    assert ov.window_lines(_rows(5), 4, 5) == _rows(5)
    assert ov.window_lines(_rows(50), 49, None) == _rows(50)


def test_window_lines_marks_only_the_hidden_end_near_either_edge():
    assert ov.window_lines(_rows(10), 0, 4) == [*_rows(3), "[dim]  ↓ 7 more[/dim]"]
    assert ov.window_lines(_rows(10), 9, 4) == ["[dim]  ↑ 7 more[/dim]", *_rows(10)[7:]]


def test_window_lines_keeps_the_cursor_in_view_with_both_markers_in_the_middle():
    window = ov.window_lines(_rows(20), 10, 5)
    assert window == ["[dim]  ↑ 9 more[/dim]", "row 9", "row 10", "row 11", "[dim]  ↓ 8 more[/dim]"]


def test_window_lines_always_fits_its_budget_and_shows_the_cursor():
    for count in range(1, 15):
        for index in range(count):
            for limit in range(1, 12):
                window = ov.window_lines(_rows(count), index, limit)
                assert len(window) <= max(limit, ov.MIN_LIST_ROWS)
                assert f"row {index}" in window


_DOWN = b"\x1b[B"
_UP = b"\x1b[A"
_BRANCHES = ("main", "feat/maint", "release/main-fix", "develop")


def _branch_prompt(text: str = "", **kw) -> ov.TextPrompt:
    return ov.TextPrompt(
        "new-base", "New container", "Base branch", text=text, suggestions=_BRANCHES, **kw
    )


def test_filter_suggestions_puts_prefix_matches_first_case_insensitively():
    assert ov.filter_suggestions(_BRANCHES, "MAIN") == ["main", "feat/maint", "release/main-fix"]
    assert ov.filter_suggestions(_BRANCHES, "") == list(_BRANCHES)
    assert ov.filter_suggestions(_BRANCHES, "zzz") == []


def test_down_then_enter_submits_the_highlighted_row():
    p, out = ov.handle_prompt_key(_branch_prompt("ma"), _DOWN)
    assert (p.highlight, out) == (0, "editing")
    p, out = ov.handle_prompt_key(p, _DOWN)
    assert p.highlight == 1
    p, out = ov.handle_prompt_key(p, b"\r")
    assert out == "submit"
    assert p.text == "feat/maint"


def test_application_cursor_arrows_move_the_highlight_too():
    p, _ = ov.handle_prompt_key(_branch_prompt(), b"\x1bOB")
    assert p.highlight == 0
    p, _ = ov.handle_prompt_key(p, b"\x1bOA")
    assert p.highlight is None


def test_down_stops_at_the_last_match_and_up_from_the_first_leaves_the_list():
    p = _branch_prompt("dev")
    for _ in range(3):
        p, _ = ov.handle_prompt_key(p, _DOWN)
    assert p.highlight == 0
    p, _ = ov.handle_prompt_key(p, _UP)
    assert p.highlight is None


def test_tab_completes_the_first_match_without_a_highlight():
    p, out = ov.handle_prompt_key(_branch_prompt("dev"), b"\t")
    assert (p.text, p.highlight, out) == ("develop", None, "editing")


def test_tab_completes_the_highlighted_row():
    p, _ = ov.handle_prompt_key(_branch_prompt("ma"), _DOWN)
    p, _ = ov.handle_prompt_key(p, _DOWN)
    p, _ = ov.handle_prompt_key(p, b"\t")
    assert (p.text, p.highlight) == ("feat/maint", None)


def test_typing_after_arrowing_resets_the_highlight_so_enter_takes_the_text():
    p, _ = ov.handle_prompt_key(_branch_prompt("ma"), _DOWN)
    p, _ = ov.handle_prompt_key(p, b"i")
    assert p.highlight is None
    p, out = ov.handle_prompt_key(p, b"\r")
    assert (out, p.text) == ("submit", "mai")  # free text is fine for new-base


def test_backspace_also_resets_the_highlight():
    p, _ = ov.handle_prompt_key(_branch_prompt("ma"), _DOWN)
    p, _ = ov.handle_prompt_key(p, b"\x7f")
    assert (p.text, p.highlight) == ("m", None)


def test_require_suggestion_rejects_an_unlisted_name_and_keeps_editing():
    p, out = ov.handle_prompt_key(_branch_prompt("nope", require_suggestion=True), b"\r")
    assert out == "editing"
    assert p.error == "'nope' is not one of the listed branches"
    p, out = ov.handle_prompt_key(replace(p, text="develop"), b"\r")
    assert out == "submit"


def test_empty_suggestions_keep_a_plain_prompt():
    p = ov.TextPrompt(
        "container-retarget", "t", "Base branch", text="x", suggestions=(), require_suggestion=True
    )
    for key in (_DOWN, b"\t"):
        p, out = ov.handle_prompt_key(p, key)
        assert (p.text, p.highlight, out) == ("x", None, "editing")
    _, out = ov.handle_prompt_key(p, b"\r")
    assert out == "submit"  # nothing to check against: the CLI is the backstop


def _text(renderable) -> str:
    console = Console(width=60, record=True)
    console.print(renderable)
    return console.export_text()


def test_render_lists_matches_under_the_input_and_marks_the_highlight():
    out = _text(ov.render_prompt(replace(_branch_prompt("ma"), highlight=1)))
    assert "> ma" in out
    assert "▸ feat/maint" in out
    assert "  main" in out
    assert "develop" not in out


def test_render_keeps_markup_in_branch_names_literal():
    p = ov.TextPrompt("new-base", "t", "Base branch", suggestions=("feat/[wip]",))
    assert "feat/[wip]" in _text(ov.render_prompt(p))


def test_render_says_when_nothing_matches():
    assert "(no matching branch)" in _text(ov.render_prompt(_branch_prompt("zzz")))


def test_render_without_suggestions_is_unchanged():
    out = _text(ov.render_prompt(ov.TextPrompt("new-branch", "t", "New branch", text="ab")))
    assert "no matching" not in out
    assert "▸" not in out
