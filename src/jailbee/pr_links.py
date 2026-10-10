"""Cross-links between a container's superproject PR and its submodule PRs.

The links live in jailbee-managed marker blocks inside each PR description,
so they can be rewritten idempotently without touching the rest of the text.
"""

from __future__ import annotations

SUBMODULE_PRS_MARKER = "submodule-prs"
SUPERPROJECT_PR_MARKER = "superproject-pr"


def upsert_marker_block(body: str, marker: str, content: str) -> str | None:
    """Return `body` with the `marker` block set to `content`, or None if unchanged.

    The block is located from its *last* closing marker back to the nearest
    opening marker before it, so a stray opening marker earlier in the prose
    (quoted, or left unclosed by a hand edit) never makes the replacement
    swallow the text between the two. Without a complete block, a fresh one
    is appended after a blank line.
    """
    start = f"<!-- jailbee:{marker} -->"
    end = f"<!-- /jailbee:{marker} -->"
    block = f"{start}\n{content}\n{end}"
    j = body.rfind(end)
    i = body.rfind(start, 0, j) if j != -1 else -1
    if i != -1:
        new = body[:i] + block + body[j + len(end) :]
    elif body.strip():
        new = body.rstrip() + "\n\n" + block
    else:
        new = block
    return None if new == body else new
