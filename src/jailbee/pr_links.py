"""Cross-links between a container's superproject PR and its submodule PRs.

The links live in jailbee-managed marker blocks inside each PR description,
so they can be rewritten idempotently without touching the rest of the text.
"""

from __future__ import annotations

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
