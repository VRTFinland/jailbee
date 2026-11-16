"""Claim-bound creation of a PR from a staged description."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from jailbee import attachments, pr_flow, pr_outbox
from jailbee.outbox import sent
from jailbee.outbox.inspect import safe_text
from jailbee.outbox.models import OutboxChanged, OutboxError
from jailbee.outbox.publication_db import open_publications
from jailbee.outbox.service import instance_identity
from jailbee.outbox_io import proposal_digest
from jailbee.pr_manifest import DescriptionAction

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.pr import PrCreated
    from jailbee.pr_outbox import OutboxPrText


def create_with_description(
    cfg: Config,
    incus: Incus,
    container: str,
    source: OutboxPrText,
    body: str,
    create: Callable[[str], PrCreated],
    confirm: Callable[[str], bool],
    *,
    publisher: sent.Publisher,
    title: str | None = None,
    operation: str = "Create",
) -> PrCreated:
    """Approve and publish a selected description under its publication claim.

    The caller must hold the existing PR management identity guard throughout.
    """
    if source.text.body != body:
        raise OutboxChanged("description was overridden; staged attachments cannot be published")
    identity, uuid = instance_identity(incus, container)
    store = open_publications()
    with store.claim(uuid, "pr", source.manifest) as claim:
        if identity != source.identity or source.manifest_text is None:
            raise OutboxChanged("description source changed; refresh required")
        pr_flow.validate_outbox_source(cfg, incus, container, source)
        bodies = dict(source.body_files)
        digest = proposal_digest(source.manifest, source.manifest_text, bodies)
        if store.stored_digest(uuid, "pr", source.manifest) != digest:
            raise OutboxChanged("description source changed; refresh required")
        manifest = pr_outbox.parse_manifest(source.manifest, source.manifest_text, bodies)
        if not 0 <= source.index < len(manifest.actions):
            raise OutboxChanged("description action is unavailable")
        action = manifest.actions[source.index]
        if not isinstance(action, DescriptionAction) or action.body != body:
            raise OutboxChanged("description action changed; refresh required")
        journal = store.load(uuid, "pr", source.manifest)
        if journal and any(item.index == source.index for item in journal.actions):
            raise OutboxChanged("description action is already recorded or uncertain")
        actual_title = title if title is not None else source.text.title
        print(safe_text(f"Title: {actual_title}\n\n{body}"))
        for identifier, item in sorted(manifest.attachments.items()):
            print(
                safe_text(
                    f"Attachment {identifier}: {item.name} ({item.content_type}, "
                    f"{item.size} bytes, sha256 {item.sha256})"
                )
            )
        for ref in attachments.references(body):
            print(f"Attachment {ref.id}: characters {ref.start}-{ref.end}")
        if not confirm(f"{operation} PR with this description and upload its attachments?"):
            raise OutboxError("PR publication cancelled")
        pr_flow.validate_outbox_source(cfg, incus, container, source)
        if store.stored_digest(uuid, "pr", source.manifest) != digest:
            raise OutboxChanged("description source changed; refresh required")
        journal = store.begin(
            claim,
            digest=digest,
            action_count=len(manifest.actions),
            container_full=container,
            now=time.time(),
        )
        if any(item.index == source.index for item in journal.actions):
            raise OutboxChanged("description action is already recorded or uncertain")
        rendered = store.image_publisher.resolve_body(
            claim, digest, body, manifest.attachments, cfg.repo_root, manifest.repo
        )
        pr_flow.validate_outbox_source(cfg, incus, container, source)
        store.mark_prepared(claim, digest, source.index, repo=manifest.repo, now=time.time())
        result = create(rendered)
        if result.already_existed:
            store.clear_prepared(claim, digest, source.index)
            return result
        sent_action = pr_outbox._sent_action(
            manifest,
            digest,
            source.index,
            replace(action, body=rendered, title=actual_title),
            result.url,
            result.number,
        )
        receipt = sent.entry(
            sent_action,
            instance=container,
            uuid=uuid,
            container_prefix=cfg.container_prefix,
            publisher=publisher,
        )
        store.settle(
            claim,
            digest,
            source.index,
            outcome="applied",
            url=result.url,
            issue=result.number,
            sent=receipt,
            now=time.time(),
        )
        return result
