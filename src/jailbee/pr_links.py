"""Cross-links between a container's superproject PR and its submodule PRs.

The links live in jailbee-managed marker blocks inside each PR description,
so they can be rewritten idempotently without touching the rest of the text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.tui import info, warn

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.incus import Incus

SUBMODULE_PRS_MARKER = "submodule-prs"
SUPERPROJECT_PR_MARKER = "superproject-pr"


def upsert_marker_block(body: str, marker: str, content: str) -> str | None:
    """Return `body` with the `marker` block set to `content`, or None if unchanged.

    The last complete block is selected by pairing each closing marker with
    the nearest opening marker not already closed. This keeps stray markers
    and prose outside the block intact. Without a complete block, a fresh one
    is appended after a blank line.
    """
    start = f"<!-- jailbee:{marker} -->"
    end = f"<!-- /jailbee:{marker} -->"
    block = f"{start}\n{content}\n{end}"
    i = -1
    j = -1
    open_start = -1
    position = 0
    while position < len(body):
        next_start = body.find(start, position)
        next_end = body.find(end, position)
        if next_end == -1 or (next_start != -1 and next_start < next_end):
            if next_start == -1:
                break
            open_start = next_start
            position = next_start + len(start)
        else:
            if open_start != -1:
                i, j = open_start, next_end
                open_start = -1
            position = next_end + len(end)
    if i != -1:
        new = body[:i] + block + body[j + len(end) :]
    elif body.strip():
        new = body.rstrip() + "\n\n" + block
    else:
        new = block
    return None if new == body else new


def link_pr_family(cfg: Config, incus: Incus, full: str, short: str) -> None:
    """Refresh recorded PR cross-links best-effort, without editing foreign PRs."""
    from jailbee import pr as pr_mod
    from jailbee import pr_flow, pr_outbox, submodule_pr
    from jailbee.incus import IncusError

    try:
        super_record = pr_flow.ContainerLabelState(
            incus, full, short=short, prefix=pr_flow.STACKED_LABEL_PREFIX
        ).read()
        if super_record.number is None:
            super_record = pr_flow.ContainerLabelState(incus, full, short=short).read()
        entries = []
        for path in sorted(submodule_pr.recorded_paths(incus, full)):
            record = submodule_pr.SubmodulePrState(incus, full, path).read()
            if record.number is None:
                continue
            slug = pr_outbox.scope_slug(pr_flow.PrScope.for_submodule(cfg, path))
            if slug:
                entries.append((path, slug, record))
    except (pr_flow.MalformedPrLabelError, IncusError) as exc:
        warn(f"Could not read the PR links for container '{short}': {exc}")
        return

    if not entries:
        return
    super_number = super_record.number
    if super_number is None:
        return
    super_slug = pr_outbox.scope_slug(pr_flow.PrScope.for_repo(cfg))

    def update(root: Path, number: int, slug: str, marker: str, content: str) -> None:
        try:
            body = pr_mod.pr_body(root, number, repo=slug)
            new = upsert_marker_block(body, marker, content)
            if new is not None:
                pr_mod.edit_pr(root, number, body=new, repo=slug)
        except pr_mod.PrError as exc:
            warn(f"Could not update the links in {slug}#{number}: {exc}")

    if not super_record.author:
        info(
            f"Submodule PRs for PR #{super_number}: "
            + ", ".join(f"{slug}#{record.number}" for _, slug, record in entries)
        )
    elif super_slug:
        content = "**Submodule PRs** (merge these first):\n" + "\n".join(
            f"- {slug}#{record.number} — `{path}`" for path, slug, record in entries
        )
        update(cfg.repo_root, super_number, super_slug, SUBMODULE_PRS_MARKER, content)
    if super_slug:
        for path, slug, record in entries:
            if record.author and record.number is not None:
                update(
                    cfg.repo_root / path,
                    record.number,
                    slug,
                    SUPERPROJECT_PR_MARKER,
                    f"Part of {super_slug}#{super_number}",
                )
