"""Publish review and reply actions into one GitHub pending review."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from jailbee import pr

if TYPE_CHECKING:
    from jailbee.pr import PendingReview
    from jailbee.pr_outbox import ReplyAction, ReviewAction, Target


class DraftSession:
    """One manifest's draft publication: finds the viewer's pending review lazily, once."""

    def __init__(self, target: Target) -> None:
        self.target = target
        self._pending: PendingReview | None = None
        self._looked_up = False

    def _get_pending(self) -> PendingReview | None:
        assert self.target.pr is not None
        if not self._looked_up:
            self._pending = pr.find_pending_review(
                self.target.scope.repo_root,
                self.target.pr.number,
                repo=self.target.manifest.repo,
            )
            self._looked_up = True
            expected = self.target.manifest.head_sha or self.target.pr.head_sha
            if self._pending is not None and self._pending.commit_oid != expected:
                raise pr.PrReviewError(
                    f"your pending review on PR #{self.target.pr.number} is on commit "
                    f"{self._pending.commit_oid[:12]}; submit or discard it on GitHub first"
                )
        return self._pending

    def _create_pending(self) -> PendingReview:
        assert self.target.pr is not None
        self._pending = pr.create_pending_review(
            self.target.scope.repo_root,
            self.target.pr.number,
            commit_id=self.target.manifest.head_sha or self.target.pr.head_sha,
            body="",
            comments=[],
            repo=self.target.manifest.repo,
        )
        self._looked_up = True
        return self._pending

    def review(self, action: ReviewAction) -> str:
        from jailbee.pr_outbox import _comment_payload

        assert self.target.pr is not None
        pending = self._get_pending()
        comments = [_comment_payload(comment) for comment in action.comments]
        if pending is None:
            self._pending = pr.create_pending_review(
                self.target.scope.repo_root,
                self.target.pr.number,
                commit_id=self.target.manifest.head_sha or self.target.pr.head_sha,
                body=action.body,
                comments=comments,
                repo=self.target.manifest.repo,
            )
            self._looked_up = True
            return self._pending.url

        appended = False
        try:
            for comment in comments:
                pr.add_pending_review_thread(self.target.scope.repo_root, pending, comment)
                appended = True
            if action.body:
                body = f"{pending.body}\n\n{action.body}" if pending.body else action.body
                pr.update_pending_review_body(
                    self.target.scope.repo_root,
                    self.target.pr.number,
                    pending,
                    body,
                    repo=self.target.manifest.repo,
                )
                # The frozen value models GitHub's current pending review; later
                # actions in this session must append to this updated body.
                self._pending = replace(pending, body=body)
        except pr.PrReviewError as exc:
            if appended:
                raise pr.PrReviewError(
                    f"{exc}; some comments may already be in your pending review on PR "
                    f"#{self.target.pr.number} — check it before re-running"
                ) from exc
            raise
        return pending.url

    def reply(self, action: ReplyAction) -> str:
        assert self.target.pr is not None
        pending = self._get_pending()
        if pending is None:
            pending = self._create_pending()
        pr.add_pending_reply(
            self.target.scope.repo_root,
            pending,
            action.comment_id,
            action.body,
            repo=self.target.manifest.repo,
        )
        return pending.url
