from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.pr import PendingReview, PrInfo, PrReviewError
from jailbee.pr_flow import PrScope
from jailbee.pr_outbox import ReplyAction, ReviewAction, Target, parse_manifest


def _target():
    manifest = parse_manifest(
        "001-review.json",
        '{"version":1,"repo":"acme/widgets","pr":7,"head_sha":"manifest-sha",'
        '"actions":[{"type":"review","body":"new","comments":[]}]}',
        {},
    )
    return Target(
        manifest=manifest,
        pr=PrInfo(7, "feature", "pr-sha", "OPEN", "main"),
        stale=False,
        scope=PrScope(Path("/repo"), "origin", "", None),
    )


def _pending(*, body="old", commit_oid="manifest-sha"):
    return PendingReview(
        1, "node-1", "https://github.com/acme/widgets/pull/7#review-1", body, commit_oid
    )


def test_review_creates_pending_when_none(mocker):
    from jailbee.pr_draft import DraftSession
    from jailbee.pr_outbox import LineComment

    mocker.patch("jailbee.pr.find_pending_review", return_value=None)
    create = mocker.patch("jailbee.pr.create_pending_review", return_value=_pending())
    action = ReviewAction("new", (LineComment("a.py", 12, "inline"),), "COMMENT")

    receipt = DraftSession(_target()).review(action)

    assert receipt == _pending().url
    create.assert_called_once_with(
        Path("/repo"),
        7,
        commit_id="manifest-sha",
        body="new",
        comments=[{"path": "a.py", "line": 12, "body": "inline", "side": "RIGHT"}],
        repo="acme/widgets",
    )


def test_review_appends_to_existing_pending(mocker):
    from jailbee.pr_draft import DraftSession
    from jailbee.pr_outbox import LineComment

    pending = _pending()
    mocker.patch("jailbee.pr.find_pending_review", return_value=pending)
    create = mocker.patch("jailbee.pr.create_pending_review")
    thread = mocker.patch("jailbee.pr.add_pending_review_thread")
    update = mocker.patch("jailbee.pr.update_pending_review_body")
    mocker.patch(
        "jailbee.pr_outbox._comment_payload",
        side_effect=[{"path": "a.py", "line": 2}, {"path": "b.py", "line": 4}],
    )
    action = ReviewAction(
        "",
        (LineComment("a.py", 2, "one"), LineComment("b.py", 4, "two")),
        "COMMENT",
    )

    receipt = DraftSession(_target()).review(action)

    assert receipt == pending.url
    assert thread.call_count == 2
    assert [call.args[2] for call in thread.call_args_list] == [
        {"path": "a.py", "line": 2},
        {"path": "b.py", "line": 4},
    ]
    update.assert_not_called()
    create.assert_not_called()


def test_review_empty_body_does_not_touch_existing_body(mocker):
    from jailbee.pr_draft import DraftSession

    pending = _pending()
    mocker.patch("jailbee.pr.find_pending_review", return_value=pending)
    update = mocker.patch("jailbee.pr.update_pending_review_body")

    DraftSession(_target()).review(ReviewAction("", (), "COMMENT"))

    update.assert_not_called()


def test_pending_on_other_commit_refuses(mocker):
    from jailbee.pr_draft import DraftSession

    mocker.patch("jailbee.pr.find_pending_review", return_value=_pending(commit_oid="different"))
    create = mocker.patch("jailbee.pr.create_pending_review")
    thread = mocker.patch("jailbee.pr.add_pending_review_thread")
    update = mocker.patch("jailbee.pr.update_pending_review_body")

    with pytest.raises(PrReviewError, match="submit or discard it on GitHub first"):
        DraftSession(_target()).review(ReviewAction("new", (), "COMMENT"))

    create.assert_not_called()
    thread.assert_not_called()
    update.assert_not_called()


def test_cached_pending_on_other_commit_refuses_later_actions(mocker):
    from jailbee.pr_draft import DraftSession
    from jailbee.pr_outbox import LineComment

    find = mocker.patch(
        "jailbee.pr.find_pending_review", return_value=_pending(commit_oid="different")
    )
    create = mocker.patch("jailbee.pr.create_pending_review")
    thread = mocker.patch("jailbee.pr.add_pending_review_thread")
    update = mocker.patch("jailbee.pr.update_pending_review_body")
    reply = mocker.patch("jailbee.pr.add_pending_reply")
    session = DraftSession(_target())

    with pytest.raises(PrReviewError, match="submit or discard it on GitHub first"):
        session.review(ReviewAction("new", (), "COMMENT"))
    with pytest.raises(PrReviewError, match="submit or discard it on GitHub first"):
        session.reply(ReplyAction(42, "reply"))
    with pytest.raises(PrReviewError, match="submit or discard it on GitHub first"):
        session.review(ReviewAction("", (LineComment("a.py", 1, "inline"),), "COMMENT"))

    find.assert_called_once()
    create.assert_not_called()
    thread.assert_not_called()
    update.assert_not_called()
    reply.assert_not_called()


def test_reply_without_pending_creates_empty_pending(mocker):
    from jailbee.pr_draft import DraftSession

    mocker.patch("jailbee.pr.find_pending_review", return_value=None)
    create = mocker.patch("jailbee.pr.create_pending_review", return_value=_pending(body=""))
    reply = mocker.patch("jailbee.pr.add_pending_reply")

    receipt = DraftSession(_target()).reply(ReplyAction(42, "reply"))

    assert receipt == _pending(body="").url
    create.assert_called_once_with(
        Path("/repo"), 7, commit_id="manifest-sha", body="", comments=[], repo="acme/widgets"
    )
    reply.assert_called_once_with(
        Path("/repo"), _pending(body=""), 42, "reply", repo="acme/widgets"
    )


def test_lookup_happens_once_per_session(mocker):
    from jailbee.pr_draft import DraftSession

    find = mocker.patch("jailbee.pr.find_pending_review", return_value=None)
    mocker.patch("jailbee.pr.create_pending_review", return_value=_pending(body=""))
    mocker.patch("jailbee.pr.add_pending_reply")
    session = DraftSession(_target())

    session.review(ReviewAction("new", (), "COMMENT"))
    session.reply(ReplyAction(42, "reply"))

    find.assert_called_once()


def test_partial_append_failure_names_pending_review(mocker):
    from jailbee.pr_draft import DraftSession
    from jailbee.pr_outbox import LineComment

    mocker.patch("jailbee.pr.find_pending_review", return_value=_pending())
    thread = mocker.patch(
        "jailbee.pr.add_pending_review_thread", side_effect=[None, PrReviewError("HTTP 500")]
    )

    with pytest.raises(
        PrReviewError, match="some comments may already be in your pending review on PR #7"
    ):
        DraftSession(_target()).review(
            ReviewAction(
                "",
                (LineComment("a.py", 1, "one"), LineComment("b.py", 2, "two")),
                "COMMENT",
            )
        )

    assert thread.call_count == 2


@pytest.mark.parametrize("with_comments", [False, True])
def test_existing_pending_summary_refuses_before_any_mutation(mocker, with_comments):
    from jailbee.pr_draft import DraftSession
    from jailbee.pr_outbox import LineComment

    mocker.patch("jailbee.pr.find_pending_review", return_value=_pending())
    create = mocker.patch("jailbee.pr.create_pending_review")
    thread = mocker.patch("jailbee.pr.add_pending_review_thread")
    update = mocker.patch("jailbee.pr.update_pending_review_body")
    comments = (LineComment("a.py", 1, "inline"),) if with_comments else ()

    with pytest.raises(PrReviewError, match="add the summary in GitHub or stage an empty summary"):
        DraftSession(_target()).review(ReviewAction("unseen summary", comments, "COMMENT"))

    create.assert_not_called()
    thread.assert_not_called()
    update.assert_not_called()


def test_body_only_concurrent_submission_never_edits_public_review(mocker):
    from jailbee.pr_draft import DraftSession

    remote = {"state": "PENDING", "body": "old"}

    def lookup(*args, **kwargs):
        snapshot = _pending(body=remote["body"])
        remote["state"] = "COMMENTED"
        return snapshot

    def update_body(*args, **kwargs):
        remote["body"] = args[3]

    mocker.patch("jailbee.pr.find_pending_review", side_effect=lookup)
    update = mocker.patch("jailbee.pr.update_pending_review_body", side_effect=update_body)
    with pytest.raises(PrReviewError, match="empty summary"):
        DraftSession(_target()).review(ReviewAction("unseen summary", (), "COMMENT"))

    assert remote == {"state": "COMMENTED", "body": "old"}
    update.assert_not_called()


@pytest.mark.parametrize("created_by", ["review", "reply"])
def test_summary_refuses_for_pending_created_earlier_in_session(mocker, created_by):
    from jailbee.pr_draft import DraftSession
    from jailbee.pr_outbox import LineComment

    find = mocker.patch("jailbee.pr.find_pending_review", return_value=None)
    create = mocker.patch("jailbee.pr.create_pending_review", return_value=_pending())
    reply = mocker.patch("jailbee.pr.add_pending_reply")
    thread = mocker.patch("jailbee.pr.add_pending_review_thread")
    update = mocker.patch("jailbee.pr.update_pending_review_body")
    session = DraftSession(_target())
    if created_by == "review":
        session.review(ReviewAction("first summary", (), "COMMENT"))
    else:
        session.reply(ReplyAction(42, "reply"))

    with pytest.raises(PrReviewError, match="empty summary"):
        session.review(ReviewAction("later summary", (LineComment("a.py", 1, "inline"),), "COMMENT"))

    find.assert_called_once()
    create.assert_called_once()
    assert reply.call_count == (created_by == "reply")
    thread.assert_not_called()
    update.assert_not_called()
