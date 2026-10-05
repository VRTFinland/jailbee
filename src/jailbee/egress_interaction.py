"""Terminal prompts for interactive Egress commands."""

from __future__ import annotations


def prompt_add_entry() -> str | None:
    """Ask for an Egress destination."""
    import questionary

    answer = questionary.text(
        "Destination (host, host:port, *.domain, IPv4, or CIDR):",
        validate=lambda value: _validate_entry(value),
    ).ask()
    return answer if isinstance(answer, str) else None


def _validate_entry(value: str) -> bool | str:
    from jailbee.egress import validate_allow_entry

    try:
        validate_allow_entry(value)
    except ValueError as exc:
        return str(exc)
    return True


def pick_remove_entry(entries: list[str], *, scope: str) -> str | None:
    """Select an Egress override to remove, or cancel."""
    if not entries:
        return None
    import questionary

    choices = [questionary.Choice(entry, value=entry) for entry in entries]
    choices.append(questionary.Choice("cancel — change nothing", value="__cancel__"))
    selected = questionary.select(f"Select {scope} override to remove:", choices=choices).ask()
    return selected if isinstance(selected, str) and selected != "__cancel__" else None
