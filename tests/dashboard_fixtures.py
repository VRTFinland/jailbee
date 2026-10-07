"""Shared test fixtures for the dashboard suite."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import frame as tframe
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from jailbee.git_status import GitStatus
from jailbee.lifecycle import ContainerInfo


def ci(
    name: str,
    repo: str,
    state: str = "Running",
    *,
    mode: str = "clone",
    pr_number: int | None = None,
    job_phase: str | None = None,
    job_pid: int | None = None,
    git_status: GitStatus | None = None,
) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        state=state,
        network="strict",
        ip=None,
        memory_limit=None,
        repo=repo,
        mode=mode,
        pr_number=pr_number,
        job_phase=job_phase,
        job_pid=job_pid,
        git_status=git_status,
    )


def wide_group(tmp_path: Path) -> dmodel.RepoGroup:
    return dmodel.RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [
            dataclasses.replace(
                ci("alpha-one", "alpha", pr_number=4, mode="mount"),
                created_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            )
        ],
    )


WIDE = ("name", "state", "network", "mode", "pr", "created")


def named_rows_group(tmp_path: Path, n: int) -> dmodel.RepoGroup:
    """Containers whose short names ("row07") cannot collide with anything else."""
    return dmodel.RepoGroup(
        "alpha",
        "/repos/alpha",
        tmp_path / "a.yaml",
        [ci(f"alpha-row{i:02d}", "alpha") for i in range(n)],
    )


def frame_at(groups, *, width, offset=0, selected=None, folded=frozenset(), enabled=WIDE):  # type: ignore[no-untyped-def]
    """One frame of ``groups`` at ``width``, as plain text."""
    console = Console(record=True, width=width)
    console.print(
        tframe.render(
            groups,
            selected,
            now=datetime(2026, 6, 8, tzinfo=UTC),
            git_enabled=True,
            enabled=enabled,
            folded=folded,
            column_offset=offset,
        )
    )
    return console.export_text()


def header(text: str) -> str:
    """The table's column-heading line of a rendered frame."""
    return next(line for line in text.splitlines() if "NAME" in line)


def retarget_group(tmp_path: Path) -> dmodel.RepoGroup:
    """Repo ``alpha`` whose one container is based on ``feat/a``."""
    info = dataclasses.replace(ci("alpha-x", "alpha"), base_branch="feat/a")
    return dmodel.RepoGroup("alpha", str(tmp_path), None, [info])


def fake_branches(_root, *, exclude=None):  # type: ignore[no-untyped-def]
    return tuple(b for b in ("main", "feat/a", "develop") if b != exclude)


def cfg_group(tmp_path: Path, containers: tuple[ContainerInfo, ...] = ()) -> dmodel.RepoGroup:
    """Repo ``alpha`` with a config path: a local child gets ``--config``, an SSH one must not."""
    return dmodel.RepoGroup(
        "alpha", str(tmp_path), tmp_path / ".jailbee" / "config.yaml", list(containers)
    )


def repo_menu_verbs(menu: tmenu.RepoMenuState | None) -> set[str]:
    """Every leaf verb of a repo menu, submenus included."""
    assert menu is not None
    return {
        leaf[1]
        for item in menu.actions
        for leaf in (item.actions if isinstance(item, dmenus.MenuGroup) else (item,))
    }


TEAM_ROWS = (
    '[{"agent": "claude", "group": "team", "account": null, "state": "empty",'
    ' "repos": [], "containers": []}]'
)

# Same rows as `ROWS` in tests/test_dashboard_accounts.py: a live login in
# "team", a parked login, an empty "spare" group.
ACCOUNT_ROWS = (
    '[{"agent": "claude", "group": "team", "account": "a@x.io#org12345", "state": "live",'
    ' "repos": ["alpha"], "containers": ["alpha-x"]},'
    ' {"agent": "claude", "group": null, "account": "b@x.io~2", "state": "parked",'
    ' "repos": [], "containers": []},'
    ' {"agent": "claude", "group": "spare", "account": null, "state": "empty",'
    ' "repos": [], "containers": []}]'
)
ACCOUNT_LS = tsession.da.account_ls_argv()


def groups_listing(stdout: str) -> tsession.da.CliResult:
    return tsession.da.CliResult(True, "done", stdout)


def fake_account_cli(mocker, *, listing, change=None):  # type: ignore[no-untyped-def]
    """Patch the quiet CLI runner: the group listing answers ``listing``, a change ``change``."""
    change = change or tsession.da.CliResult(True, "Set.")

    def fake(argv, **_kwargs):  # type: ignore[no-untyped-def]
        return listing if argv[:3] == ["account", "group", "ls"] else change

    return mocker.patch.object(tsession.da, "run_cli_quiet", side_effect=fake)


def fake_accounts_cli(mocker, *, listing=None, change=None):  # type: ignore[no-untyped-def]
    """Patch the quiet CLI runner for the Accounts panel.

    Every `account ls` answers ``listing``; anything else is a change and
    answers ``change``.
    """
    listing = listing or groups_listing(ACCOUNT_ROWS)
    change = change or tsession.da.CliResult(True, "Done.")

    def fake(argv, **_kwargs):  # type: ignore[no-untyped-def]
        return listing if argv[:2] == ["account", "ls"] else change

    return mocker.patch.object(tsession.da, "run_cli_quiet", side_effect=fake)


def alpha_group(tmp_path: Path) -> dmodel.RepoGroup:
    """Repo ``alpha`` with its one container ``alpha-x``."""
    return dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
