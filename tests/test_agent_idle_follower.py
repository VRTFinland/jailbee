"""An idle agent whose only background job is the inbox follower reads as idle (#146 ticket 38).

Driven end to end through the dashboards' one gather step
(`tests.follower_fixture.gather`). The result is then
rendered the three ways a user sees it: the terminal dashboard's AGENT cell,
the web front page's row state and `jb ls --json`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from rich.text import Text

from jailbee.dashboard import format as dfmt
from jailbee.lifecycle import ls_field_specs
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.web.view import front_page_view
from tests.follower_fixture import CLAUDE, FOLLOWER, WRAPPER, gather
from tests.web_fakes import snapshot_of

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def _shown(info, group) -> tuple[str, str, str]:
    """(TUI AGENT cell, web row state, `jb ls --json` state)."""
    specs = {s.name: s for s in ls_field_specs(now=NOW)}
    tui = Text.from_markup(dfmt.dashboard_cell(specs["agent"], info, NOW)).plain
    view = front_page_view(snapshot_of(group), RemoteRepoScope(frozenset()), NOW)
    (row,) = view.repos[0].containers
    (entry,) = specs["agent"].json(info)
    return tui, row.agent, entry["state"]


def test_follower_only_reads_idle_everywhere(tmp_path):
    info, group = gather(
        tmp_path,
        "shell",
        [(1011, CLAUDE, "bash", WRAPPER), (1012, 1011, "python", FOLLOWER)],
    )

    tui, web, ls_json = _shown(info, group)
    assert "idle" in tui and "shell" not in tui
    assert web == "idle"
    assert ls_json == "idle"


@pytest.mark.parametrize(
    "other",
    [("npm", "run", "dev"), ("sleep", "600")],
)
def test_follower_beside_another_job_stays_shell(tmp_path, other):
    info, group = gather(
        tmp_path,
        "shell",
        [
            (1011, CLAUDE, "bash", WRAPPER),
            (1012, 1011, "python", FOLLOWER),
            (1013, CLAUDE, "bash", ("/bin/bash", "-c", " ".join(other))),
            (1014, 1013, other[0], other),
        ],
    )

    tui, web, ls_json = _shown(info, group)
    assert "shell" in tui
    assert (web, ls_json) == ("shell", "shell")


@pytest.mark.parametrize("state", ["busy", "waiting"])
def test_busy_and_waiting_are_untouched(tmp_path, state):
    info, group = gather(tmp_path, state, [(1012, CLAUDE, "python", FOLLOWER)])

    _, web, ls_json = _shown(info, group)
    assert (web, ls_json) == (state, state)


def test_shell_without_children_is_left_as_claude_said(tmp_path):
    info, group = gather(tmp_path, "shell", [])

    _, web, ls_json = _shown(info, group)
    assert (web, ls_json) == ("shell", "shell")



def test_a_child_gone_before_its_argv_was_read_leaves_shell(tmp_path):
    """The sampler saw the process; its /proc entry vanished before the argv read."""
    info, group = gather(tmp_path, "shell", [(1012, CLAUDE, "python", FOLLOWER)], no_argv={1012})

    _, web, ls_json = _shown(info, group)
    assert (web, ls_json) == ("shell", "shell")


def test_non_shell_children_such_as_mcp_servers_are_not_background_jobs(tmp_path):
    """Claude runs every background task in a shell wrapper; a stdio MCP server
    or language server is a direct non-shell child and no task."""
    info, group = gather(
        tmp_path,
        "shell",
        [
            (1011, CLAUDE, "bash", WRAPPER),
            (1012, 1011, "python", FOLLOWER),
            (1013, CLAUDE, "node", ("node", "/usr/lib/mcp/server.js")),
            (1014, 1013, "node", ("node", "worker.js")),
        ],
    )

    _, web, ls_json = _shown(info, group)
    assert (web, ls_json) == ("idle", "idle")

