"""Host-only reconciliation of uncertain attachment uploads."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from jailbee.outbox.commands import resolve_target
from jailbee.outbox.models import OutboxChanged, OutboxError
from jailbee.outbox.publication_db import open_publications
from jailbee.outbox.service import instance_identity, load_container

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.outbox.attachment_publication import UploadOutcome
    from jailbee.outbox.models import ProposalId


def recover(
    cfg: Config,
    incus: Incus,
    container: str,
    proposal: ProposalId,
    blob: str,
    repository_id: int,
    *,
    url: str | None,
    allow_duplicate: bool,
    confirm: Callable[[UploadOutcome], bool],
    expected_revision: str | None = None,
) -> int:
    """Resolve one recorded upload, bound to a fresh proposal and host claim."""
    if (url is None) == (not allow_duplicate):
        raise OutboxError("provide exactly one of --url or --retry-duplicate")
    target, full = resolve_target(cfg, incus, container)
    identity, uuid = instance_identity(incus, full)
    publications = open_publications()
    with publications.claim(uuid, proposal.kind, proposal.name) as claim:

        def checked(revision: str | None) -> str:
            fresh = load_container(target, incus, full, raise_errors=True)
            if not fresh.available or fresh.identity != identity:
                raise OutboxChanged(fresh.error or "container changed; refresh required")
            view = next((v for v in fresh.proposals if v.id == proposal), None)
            if view is None or (revision is not None and view.revision != revision):
                raise OutboxChanged("proposal changed; refresh required")
            if view.error:
                raise OutboxError(f"{proposal}: {view.error}")
            return view.revision

        revision = checked(expected_revision)
        digest = publications.stored_digest(uuid, proposal.kind, proposal.name)
        if digest is None:
            raise OutboxChanged("proposal changed; refresh required")
        publisher = publications.image_publisher
        matches = [
            o
            for o in publisher.outcomes(claim, digest)
            if o.attachment.blob == blob
            and o.repository_id == repository_id
            and o.state in ("prepared", "uncertain")
        ]
        if len(matches) != 1:
            raise OutboxError("no unique unresolved image upload matches this identity")
        outcome = matches[0]
        if not confirm(outcome):
            return 0
        checked(revision)
        publisher.reconcile(
            claim,
            digest,
            blob,
            outcome.host,
            repository_id,
            url=url,
            authorize_duplicate=allow_duplicate,
        )
    return 0
