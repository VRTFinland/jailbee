"""Render short, safe inbox follower notifications."""

import textwrap
from typing import Any

from jailbee.inbox.render import clean_c0c1


def render_event(event: dict[str, Any]) -> str | None:
    kind = event.get("kind")
    if kind == "quiet-child":
        child = clean_c0c1(str(event.get("child", "")))
        return (
            f"JailBee inbox: child {child} has reached the quiet threshold "
            "— check it with jailbee inbox children"
        )
    if kind == "digest":
        return "JailBee inbox: child status digest — check it with jailbee inbox children"
    if kind == "grant-changed":
        raw = event.get("changes")
        items = raw if isinstance(raw, list) else []
        changes = "; ".join(clean_c0c1(str(c)) for c in items)[:400]
        detail = f" ({changes})" if changes else ""
        hint = "" if items == ["grant revoked"] else " — see jailbee inbox grant"
        return f"JailBee inbox: your orchestration grant changed{detail}{hint}"
    if kind != "message":
        return None
    item = event["item"]
    ident = clean_c0c1(str(item.get("id", "")))
    sender = clean_c0c1(str(item.get("from", "")))
    item_kind = clean_c0c1(str(item.get("kind", item.get("state", ""))))
    excerpt = textwrap.shorten(
        clean_c0c1(str(item.get("body", item.get("detail", "")))), width=80, placeholder="…"
    )
    return (
        f"JailBee inbox: {ident} from {sender} ({item_kind}): {excerpt} "
        f"— read it with: jailbee inbox read {ident}"
    )
