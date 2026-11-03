class InboxUsage(Exception):
    """Invalid inbox input."""


class InboxBadUsage(InboxUsage):
    """Invalid command usage."""
