"""Tests for `pr_links`: the marker-block upsert and PR family linking."""

from __future__ import annotations

from jailbee.pr_links import upsert_marker_block

S, E = "<!-- jailbee:m -->", "<!-- /jailbee:m -->"


def test_appends_block_after_a_blank_line():
    assert upsert_marker_block("Hello.\n", "m", "x") == f"Hello.\n\n{S}\nx\n{E}"


def test_empty_body_becomes_the_block():
    assert upsert_marker_block("", "m", "x") == f"{S}\nx\n{E}"


def test_replaces_in_place_keeping_text_on_both_sides():
    body = f"Intro\n\n{S}\nold\n{E}\n\nOutro"
    assert upsert_marker_block(body, "m", "new") == f"Intro\n\n{S}\nnew\n{E}\n\nOutro"


def test_unchanged_returns_none():
    body = f"Intro\n\n{S}\nx\n{E}"
    assert upsert_marker_block(body, "m", "x") is None


def test_stray_unclosed_start_is_kept_and_a_block_appended():
    body = f"Intro {S} mentioned in prose\nMore text"
    out = upsert_marker_block(body, "m", "x")
    assert out == f"{body}\n\n{S}\nx\n{E}"
    # Second run pairs the closing marker with its nearest start, so the
    # prose before it survives.
    assert upsert_marker_block(out, "m", "y") == f"{body}\n\n{S}\ny\n{E}"


def test_other_markers_are_ignored():
    body = "<!-- jailbee:other -->\nz\n<!-- /jailbee:other -->"
    assert upsert_marker_block(body, "m", "x") == f"{body}\n\n{S}\nx\n{E}"
