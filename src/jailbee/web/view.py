"""The browser's view model, built from a state-service snapshot.

Everything shown comes from the frontend-agnostic dashboard core, the same
functions the terminal dashboard renders with: `default_columns` and
`nonempty_columns` choose the columns, `dashboard_header` / `dashboard_cell`
format the values, `sort_groups` orders the rows and `visible_repo_groups`
filters. This module only converts the core's Rich markup to plain text and
applies `remote.web.excluded_repos`, on the server, before anything is
serialised, so a hidden repository never reaches a REST response or a
WebSocket message.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich.text import Text

from jailbee.dashboard.columns import default_columns, nonempty_columns, visible_fields
from jailbee.dashboard.fork_tree import fork_layout, name_prefix
from jailbee.dashboard.sorting import DEFAULT_SORT, sort_groups
from jailbee.dashboard.visibility import visible_repo_groups
from jailbee.web.protocol import ColumnHead, ContainerRow, FrontPageView, RepoRow

if TYPE_CHECKING:
    from jailbee.lifecycle import ContainerInfo
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope
    from jailbee.state_service.protocol import Snapshot
    from jailbee.table_format import FieldSpec


def _plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def agent_state(info: ContainerInfo) -> str | None:
    """The most urgent agent state in the container (the core ranks them), or None."""
    return info.agent_status[0].state if info.agent_status else None


def front_page_view(snapshot: Snapshot, scope: RemoteRepoScope, now: datetime) -> FrontPageView:
    allowed = [g for g in snapshot.groups if scope.allows(g.prefix)]
    groups = visible_repo_groups(allowed, show_empty_repos=True, hidden_repos=frozenset())
    enabled = default_columns()
    shown = nonempty_columns(groups, now=now, enabled=enabled)
    groups = sort_groups(groups, DEFAULT_SORT, shown, now=now)
    containers = [c for g in groups for c in g.containers]
    specs = visible_fields(now, containers, shown)
    return FrontPageView(
        seq=snapshot.seq,
        gathered_at=snapshot.gathered_at,
        columns=[ColumnHead(name=s.name, header=s.header) for s in specs],
        repos=[
            RepoRow(prefix=group.prefix, containers=_rows(group.containers, specs))
            for group in groups
        ],
    )


def _rows(
    containers: list[ContainerInfo], specs: list[FieldSpec[ContainerInfo]]
) -> list[ContainerRow]:
    layout = fork_layout(containers)
    return [
        ContainerRow(
            name=info.name,
            indent=name_prefix(layout, info.name),
            agent=agent_state(info),
            cells=[_plain(s.cell(info)) for s in specs],
        )
        for info in containers
    ]
