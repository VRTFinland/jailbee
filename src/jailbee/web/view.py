"""The browser's view model, built from a state-service snapshot.

This slice lists container names only. The front-page parity slice replaces
it with repository groups and the dashboard core's columns, and applies
`remote.web.excluded_repos` here, on the server, before anything reaches a
browser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.web.protocol import ContainerRow, FrontPageView

if TYPE_CHECKING:
    from jailbee.state_service.protocol import Snapshot


def front_page_view(snapshot: Snapshot) -> FrontPageView:
    return FrontPageView(
        seq=snapshot.seq,
        gathered_at=snapshot.gathered_at,
        containers=[
            ContainerRow(name=info.name) for group in snapshot.groups for info in group.containers
        ],
    )
