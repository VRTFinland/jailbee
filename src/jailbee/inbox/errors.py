class InboxUsage(Exception):  # noqa: N818 - public interface
    """Invalid inbox input."""


class InboxBadUsage(InboxUsage):
    """Invalid command usage."""
