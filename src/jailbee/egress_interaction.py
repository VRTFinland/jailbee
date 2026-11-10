"""Terminal prompts for interactive Egress commands."""

from __future__ import annotations


def prompt_add_entry() -> str | None:
    """Ask for an Egress destination; None when the user cancels."""
    from jailbee import prompting

    try:
        return prompting.ask_text(
            "destination (host, host:port, *.domain, IPv4, or CIDR)",
            validate=_validate_entry,
        )
    except prompting.Cancelled:
        return None


def _validate_entry(value: str) -> str | None:
    """The problem with `value` as an Egress destination, or None when it is valid."""
    from jailbee.egress import validate_allow_entry

    try:
        validate_allow_entry(value)
    except ValueError as exc:
        return str(exc)
    return None


def pick_remove_entry(entries: list[str], *, scope: str) -> str | None:
    """Select an Egress override to remove, or cancel."""
    if not entries:
        return None
    from jailbee import prompting

    try:
        return prompting.choose_one(
            f"{scope} override to remove",
            [prompting.Option(value=entry, title=entry, label=entry) for entry in entries],
            destructive=True,
        )
    except prompting.Cancelled:
        return None
