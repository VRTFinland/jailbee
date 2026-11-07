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
    """Serve issue proposals through whatever fake `service.read_store` is.

    Issue proposals come from the host store (`service.issue_snapshot`, keyed
    by instance uuid). Fixtures written for the file layout fake `read_store`
    for both kinds; routing the issue kind to it (looked up per call, so a
    test's later re-patch applies) keeps those canned snapshots in force. The
    routed call carries ``uid=None``: the host store has no container user.
    """

    # Deleting follows the same route: fixtures that observe `mutate_store` keep
    # seeing the deletion scope; the real store path is tested in
    # tests/test_outbox_service_issue_store.py.
    def mutate(container: str, uuid: str | None, plan: Any, snapshot: Any) -> tuple[str, ...]:
        return service.mutate_store(  # type: ignore[no-any-return]
            None,
            container,
            "issue",
            uid=None,
            expected=snapshot.as_dict(),
            new_manifest=(plan.proposal.name, plan.new_text) if plan.new_text is not None else None,
            delete_names=plan.delete_names,
            forbidden_progress=None,
            rejected_names=snapshot.rejected,
        )

    mocker.patch.object(service, "_delete_issue_rows", side_effect=mutate)
    return mocker.patch.object(
        service,
        "issue_snapshot",
        side_effect=lambda container, uuid: service.read_store(None, container, "issue", uid=None),
    )
