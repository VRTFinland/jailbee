"""Shared wake filter vocabulary."""

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


