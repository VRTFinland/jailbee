"""Notice that JailBee was upgraded under the running web server. Standard library only."""

from __future__ import annotations

from collections.abc import Callable


def watch_for_update(
    running: str,
    on_change: Callable[[str], None],
    *,
    installed: Callable[[], str | None],
    wait: Callable[[float], bool],
    interval: float,
) -> str | None:
    """Poll ``installed`` every ``interval`` seconds until it differs from ``running``.

    ``wait(interval)`` sleeps and returns True when the watch should end (the
    server stopped by itself): the injectable seam that keeps this testable
    without sleeping. A None version is "unknown right now" (mid-upgrade, no
    dist-info), never a change. On a change ``on_change`` is called with the
    new version and it is returned.
    """
    while not wait(interval):
        current = installed()
        if current is not None and current != running:
            on_change(current)
            return current
    return None
