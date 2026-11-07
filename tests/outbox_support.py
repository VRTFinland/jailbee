"""Real proposal inputs shared by outbox service tests."""

import json
from typing import Any

from jailbee.outbox.models import Kind, StoreSnapshot
from jailbee.outbox_io import ContainerIdentity

IDENTITY = ContainerIdentity("acme-feature", "2026-09-30T00:00:00Z")


def issue_files() -> dict[str, str]:
    return {
        "001.json": json.dumps(
            {
                "version": 1,
                "actions": [
                    {
                        "type": "create",
                        "repo": ".",
                        "ref": "new",
                        "title": "Example",
                        "body_file": "body.md",
                    },
                    {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Follow-up"},
                    {"type": "comment", "repo": ".", "issue": 42, "body": "Independent"},
                ],
            }
        ),
        "body.md": "Original body",
    }


def pr_files() -> dict[str, str]:
    return {
        "001.json": json.dumps(
            {
                "version": 1,
                "repo": ".",
                "pr": 42,
                "head_sha": "a" * 40,
                "actions": [
                    {
                        "type": "review",
                        "event": "COMMENT",
                        "body": "Review body",
                        "comments": [
                            {"path": "a.py", "line": 1, "body": "First"},
                            {"path": "a.py", "line": 2, "body": "Second"},
                        ],
                    }
                ],
            }
        )
    }


def store(kind: Kind, files: dict[str, str], *, rejected: tuple[str, ...] = ()) -> StoreSnapshot:
    return StoreSnapshot(kind, tuple(sorted(files.items())), rejected, ())


def route_issue_snapshot(mocker: Any, service: Any) -> Any:
    """Serve both kinds' proposals from a canned fake store on ``service``.

    The tests that use this predate the host outbox store's test helpers: they
    install two test-only fakes on the service module, ``service.read_store``
    (``(incus, container, kind, uid=...) -> StoreSnapshot``) and
    ``service.mutate_store`` (observes a deletion's scope), and this routes the
    store reads and row deletions to them, looked up per call so a test's later
    re-patch applies. The real store paths are tested in
    tests/test_outbox_service_issue_store.py and test_outbox_service_pr_store.py.
    """

    def mutate(
        container: str, uuid: str | None, plan: Any, snapshot: Any, *, kind: str = "issue"
    ) -> tuple[str, ...]:
        return service.mutate_store(  # type: ignore[no-any-return]
            None,
            container,
            plan.proposal.kind,
            uid=None,
            expected=snapshot.as_dict(),
            new_manifest=(plan.proposal.name, plan.new_text) if plan.new_text is not None else None,
            delete_names=plan.delete_names,
            forbidden_progress=None,
            rejected_names=snapshot.rejected,
        )

    mocker.patch.object(service, "_delete_store_rows", side_effect=mutate)
    mocker.patch.object(
        service,
        "pr_snapshot",
        side_effect=lambda container, uuid: service.read_store(None, container, "pr", uid=None),
    )
    mocker.patch.object(service, "pr_journals", return_value=None)
    return mocker.patch.object(
        service,
        "issue_snapshot",
        side_effect=lambda container, uuid: service.read_store(None, container, "issue", uid=None),
    )


def pr_journal(
    files: dict[str, str],
    name: str = "001.json",
    *,
    applied: tuple[int, ...] = (),
    uncertain: tuple[int, ...] = (),
) -> Any:
    """A store PR publication journal of proposal `name` in `files` (digest matching)."""
    from jailbee.outbox.publication_db import Journal
    from jailbee.outbox_io import JournalAction, proposal_digest

    raw = files[name]
    refs: set[str] = set()
    pending: list[object] = [json.loads(raw)]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if isinstance(item.get("body_file"), str):
                refs.add(item["body_file"])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    actions = tuple(
        JournalAction(i, "applied", "acme/repo", url=f"https://x/{i}") for i in applied
    ) + tuple(JournalAction(i, "uncertain", "acme/repo") for i in uncertain)
    digest = proposal_digest(name, raw, {n: files[n] for n in refs if n in files})
    count = len(json.loads(raw)["actions"])
    return Journal("uuid", "pr", name, digest, IDENTITY.full_name, count, actions)
