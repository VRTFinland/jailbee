"""Shared inbox filter vocabulary, parsing and matching (stdlib only)."""

from jailbee.inbox.errors import InboxUsage

MESSAGE_KINDS = ("note", "question", "done", "blocked", "idle")
STATUS_STATES = ("queued", "started", "done", "refused", "failed")
# A bare token matches a message kind or a status state; `msg:<kind>` and
# `status:<state>` match only their own record type.
WAKE_TOKENS = tuple(dict.fromkeys((*MESSAGE_KINDS, *STATUS_STATES)))
MSG_PREFIX = "msg:"
STATUS_PREFIX = "status:"


def valid_wake_token(token: str) -> bool:
    if token.startswith(MSG_PREFIX):
        return token[len(MSG_PREFIX) :] in MESSAGE_KINDS
    if token.startswith(STATUS_PREFIX):
        return token[len(STATUS_PREFIX) :] in STATUS_STATES
    return token in WAKE_TOKENS


def parse_wake_filter(value: str | None) -> frozenset[str] | None:
    if value is None:
        return None
    tokens = value.split(",")
    if not all(valid_wake_token(token) for token in tokens):
        raise InboxUsage(
            "--wake-on needs comma-separated message kinds or status states "
            "(optionally msg:<kind> or status:<state>)"
        )
    return frozenset(tokens)


def matches_wake_filter(record_type: str, token: object, wake: frozenset[str] | None) -> bool:
    """Match only record type and kind/state; waiter first-message rules are separate."""
    if wake is None:
        return True
    prefix = STATUS_PREFIX if record_type == "status" else MSG_PREFIX
    return isinstance(token, str) and (token in wake or prefix + token in wake)
