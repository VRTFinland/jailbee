"""Render short, safe inbox follower notifications."""

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
        changes = "; ".join(clean_c0c1(str(c)) for c in event.get("changes", []))
        return (
            f"JailBee inbox: your orchestration grant changed ({changes}) — see jailbee inbox grant"
        )
    if kind != "message":
        return None
    item = event["item"]
    ident = clean_c0c1(str(item.get("id", "")))
    sender = clean_c0c1(str(item.get("from", "")))
    item_kind = clean_c0c1(str(item.get("kind", item.get("state", ""))))
    excerpt = clean_c0c1(str(item.get("body", item.get("detail", ""))))[:80]
    return (
        f"JailBee inbox: {ident} from {sender} ({item_kind}): {excerpt} "
        f"— read it with: jailbee inbox read {ident}"
    )
