class InboxRefusedError(Exception):
    """A request refused locally before contacting the broker."""


class InboxUsage(Exception):  # noqa: N818 - public interface
    """Invalid inbox input."""


class InboxBadUsage(InboxUsage):
    """Invalid command usage."""
