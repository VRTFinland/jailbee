"""An in-memory stand-in for the store's issue journals, for view/plan tests.

The host outbox store's journals reach the views as ``issue_journals`` (a
mapping by manifest name). A leftover ``prepared`` action is read as
``uncertain``, exactly as the store reads it.
"""

from __future__ import annotations

from dataclasses import dataclass

from jailbee.outbox_io import ContainerIdentity, IssueJournal, JournalAction


@dataclass(frozen=True)
class JournalKey:
    identity: ContainerIdentity
    manifest_name: str


def journal_key(identity: ContainerIdentity, manifest_name: str) -> JournalKey:
    return JournalKey(identity, manifest_name)


class MemoryJournals:
    def __init__(self) -> None:
        self._journals: dict[str, IssueJournal] = {}

    def create(self, key: JournalKey, digest: str, count: int) -> None:
        self._journals[key.manifest_name] = IssueJournal(
            key.identity, key.manifest_name, digest, count, ()
        )

    def _put(self, key: JournalKey, action: JournalAction) -> None:
        journal = self._journals[key.manifest_name]
        kept = tuple(a for a in journal.actions if a.index != action.index)
        actions = tuple(sorted((*kept, action), key=lambda a: a.index))
        self._journals[key.manifest_name] = IssueJournal(
            journal.identity, journal.manifest_name, journal.digest, journal.action_count, actions
        )

    def mark_prepared(self, key: JournalKey, index: int, *, repo: str) -> None:
        # The store reads a leftover prepared action as uncertain.
        self._put(key, JournalAction(index, "uncertain", repo))

    def mark_applied(
        self, key: JournalKey, index: int, *, repo: str, url: str | None, issue: int | None
    ) -> None:
        self._put(key, JournalAction(index, "applied", repo, url, issue))

    def load(self, key: JournalKey) -> IssueJournal | None:
        return self._journals.get(key.manifest_name)

    def snapshot(self) -> dict[str, IssueJournal]:
        return dict(self._journals)
