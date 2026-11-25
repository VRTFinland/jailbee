import pytest

from jailbee.inbox.follow_render import render_event


@pytest.mark.parametrize(
    ("body", "excerpt"),
    [
        ("alpha " * 12 + "waiting-for-input forever", "alpha " * 11 + "alpha…"),
        (
            "[jailbee] child `proj-kid` has been waiting for input for 30m; last message was done",
            "[jailbee] child `proj-kid` has been waiting for input for 30m; last message was…",
        ),
        ("x" * 100, "x" * 79 + "…"),
    ],
)
def test_message_excerpt_prefers_words_but_keeps_an_overlong_first_word(body: str, excerpt: str) -> None:
    assert render_event(
        {
            "kind": "message",
            "item": {"id": "m1", "from": "kid", "kind": "idle", "body": body},
        }
    ) == (f"JailBee inbox: m1 from kid (idle): {excerpt} — read it with: jailbee inbox read m1")


def test_grant_changed_renders_the_diff_and_where_to_look() -> None:
    line = render_event(
        {
            "kind": "grant-changed",
            "changes": ["max_running_children: 2 -> 5", "publish.issues: false -> true"],
        }
    )
    assert line is not None
    assert "max_running_children: 2 -> 5; publish.issues: false -> true" in line
    assert "jailbee inbox grant" in line


def test_grant_changed_detail_is_cut_to_400_characters() -> None:
    line = render_event({"kind": "grant-changed", "changes": ["x" * 300, "y" * 300]})
    assert line is not None
    detail = line.split(" (", 1)[1].split(") — ", 1)[0]
    assert len(detail) == 400
    assert detail.startswith("x" * 300 + "; y")


def test_grant_revoked_does_not_point_at_the_grant_command() -> None:
    line = render_event({"kind": "grant-changed", "changes": ["grant revoked"]})
    assert line is not None
    assert "grant revoked" in line
    assert "jailbee inbox grant" not in line
