from jailbee.comment_edit import CommentSnapshot, diff_lines, edit_refusals

URL = "https://github.com/acme/app/issues/7#issuecomment-5"


def _snapshot(**changes):
    values = {"id": 5, "author": "Alice", "body": "old", "url": URL, "parent": 7}
    values.update(changes)
    return CommentSnapshot(**values)


def test_own_unchanged_comment_on_the_named_thread_may_be_edited():
    # Logins compare case-insensitively, as GitHub treats them.
    assert (
        edit_refusals(_snapshot(), login="alice", expected_body="old", parent=7, context="c") == []
    )


def test_every_reason_to_refuse_is_reported_together():
    refusals = edit_refusals(
        _snapshot(author="bob", body="new", parent=8),
        login="alice",
        expected_body="old",
        parent=7,
        context="m action 0",
    )
    assert refusals == [
        "m action 0: comment 5 belongs to #8, not #7",
        "m action 0: comment 5 is authored by bob, not alice",
        "m action 0: expected.body differs from the current comment",
    ]


def test_unknown_publisher_is_never_taken_as_the_author():
    (refusal,) = edit_refusals(_snapshot(), login="", expected_body="old", context="c")
    assert "not the unresolved publishing account" in refusal


def test_parent_is_checked_only_when_named():
    assert (
        edit_refusals(_snapshot(parent=None), login="alice", expected_body="old", context="c") == []
    )
    (refusal,) = edit_refusals(
        _snapshot(parent=None), login="alice", expected_body="old", parent=7, context="c"
    )
    assert refusal == "c: comment 5 belongs to another thread, not #7"


def test_diff_lines_show_removed_added_and_context_lines():
    assert diff_lines("keep\nold\ntail", "keep\nnew\ntail", indent="  ") == [
        "  @@ -1,3 +1,3 @@",
        "   keep",
        "  -old",
        "  +new",
        "   tail",
    ]


def test_comment_of_another_repository_is_refused_case_insensitively():
    assert (
        edit_refusals(
            _snapshot(repo="Acme/App"),
            login="alice",
            expected_body="old",
            parent=7,
            repo="acme/app",
            context="c",
        )
        == []
    )
    (refusal,) = edit_refusals(
        _snapshot(repo="acme/other"),
        login="alice",
        expected_body="old",
        parent=7,
        repo="acme/app",
        context="c",
    )
    assert refusal == "c: comment 5 belongs to acme/other, not acme/app"


def test_unknown_comment_repository_is_refused_when_one_is_named():
    (refusal,) = edit_refusals(
        _snapshot(repo=None), login="alice", expected_body="old", repo="acme/app", context="c"
    )
    assert "not acme/app" in refusal


def test_already_applied_edit_says_how_to_resolve_it():
    (refusal,) = edit_refusals(
        _snapshot(body="new"),
        login="alice",
        expected_body="old",
        new_body="new",
        context="m action 0",
    )
    assert "already reads the new body" in refusal
    assert "--applied" in refusal and URL in refusal
    assert "expected.body differs" not in refusal


def test_differing_comment_that_is_not_the_new_body_keeps_the_generic_wording():
    (refusal,) = edit_refusals(
        _snapshot(body="third"),
        login="alice",
        expected_body="old",
        new_body="new",
        context="c",
    )
    assert "expected.body differs" in refusal
