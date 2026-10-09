"""Shared test fixtures for the dashboard suite."""

from __future__ import annotations

import dataclasses
import os
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import fleet
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
                # Loose with no revert deadline: LOOSE reads "● ∞" and stays a
                # shown column (a strict row's LOOSE cell is empty).
                network="loose",
            )
        ],
    )


WIDE = ("name", "mode", "state", "created", "network", "pr")


def named_rows_group(tmp_path: Path, n: int) -> dmodel.RepoGroup:
    """Containers whose short names ("row07") cannot collide with anything else."""
    return dmodel.RepoGroup(
        "alpha",
        "/repos/alpha",
        tmp_path / "a.yaml",
        [ci(f"alpha-row{i:02d}", "alpha") for i in range(n)],
    )


FRAME_INSET = 4
NOW = datetime(2026, 6, 8, tzinfo=UTC)


def _table_lines(
    groups,
    *,
    width=200,
    selected=None,
    now=NOW,
    enabled=None,
    folded=frozenset(),
    column_offset=0,
    column_widths=None,
    shown_columns=None,
    hidden_by_preferences=False,
):  # type: ignore[no-untyped-def]  # test utility
    inner = max(0, width - FRAME_INSET)
    model = fleet.table_model(
        groups,
        now=now,
        enabled=enabled,
        folded=folded,
        column_widths=column_widths,
        shown_columns=shown_columns,
        column_offset=column_offset,
        hidden_by_preferences=hidden_by_preferences,
        width=inner,
    )
    if model.empty_text is not None:
        from rich.text import Text

        return [Text(model.empty_text)]
    lines = [fleet.header_line(model.geometry)] if model.has_header else []
    lines += [
        fleet.entry_line(
            entry, model.geometry, model.folded, selected=entry.row == selected, width=inner
        )
        for entry in model.entries
    ]
    return lines


def table_text(
    groups,
    *,
    width=200,
    selected=None,
    now=NOW,
    enabled=None,
    folded=frozenset(),
    column_offset=0,
    column_widths=None,
    shown_columns=None,
    hidden_by_preferences=False,
):  # type: ignore[no-untyped-def]  # test utility
    """Plain table lines at the inset content width of a terminal."""
    return [
        line.plain
        for line in _table_lines(
            groups,
            width=width,
            selected=selected,
            now=now,
            enabled=enabled,
            folded=folded,
            column_offset=column_offset,
            column_widths=column_widths,
            shown_columns=shown_columns,
            hidden_by_preferences=hidden_by_preferences,
        )
    ]


def table_ansi_lines(
    groups,
    *,
    width=200,
    selected=None,
    now=NOW,
    enabled=None,
    folded=frozenset(),
    column_offset=0,
    column_widths=None,
    shown_columns=None,
    hidden_by_preferences=False,
):  # type: ignore[no-untyped-def]  # test utility
    """Styled table lines; override NO_COLOR so cursor assertions see colours."""
    console = Console(
        record=True,
        width=max(1, width - FRAME_INSET),
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
    )
    for line in _table_lines(
        groups,
        width=width,
        selected=selected,
        now=now,
        enabled=enabled,
        folded=folded,
        column_offset=column_offset,
        column_widths=column_widths,
        shown_columns=shown_columns,
        hidden_by_preferences=hidden_by_preferences,
    ):
        console.print(line, end="\n")
    return console.export_text(styles=True).splitlines()


def frame_at(groups, *, width, offset=0, selected=None, folded=frozenset(), enabled=WIDE):  # type: ignore[no-untyped-def]
    """One table at terminal ``width``; retain the frame helper's string shape."""
    return (
        "\n".join(
            table_text(
                groups,
                width=width,
                column_offset=offset,
                selected=selected,
                folded=folded,
                enabled=enabled,
            )
        )
        + "\n"
    )


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


def autostart_ci(phase: str = "autostart") -> ContainerInfo:
    """A container whose job row is an autostart run; os.getpid() keeps the worker alive."""
    return dataclasses.replace(
        ci("alpha-x", "alpha", job_phase=phase, job_pid=os.getpid()), job_kind="autostart"
    )


def mount_group(tmp_path: Path) -> dmodel.RepoGroup:
    """Kinds aws + gcloud configured; gcloud attached to alpha-x."""
    group = cfg_group(
        tmp_path, (dataclasses.replace(ci("alpha-x", "alpha"), optional_mounts=("gcloud",)),)
    )
    group.optional_mounts = ("aws", "gcloud")
    return group


def every_verb_group(tmp_path: Path) -> dmodel.RepoGroup:
    """A container that is offered every terminal-only entry at once."""
    group = mount_group(tmp_path)
    group.containers[0] = dataclasses.replace(
        group.containers[0], job_phase="autostart", job_pid=os.getpid(), job_kind="autostart"
    )
    return group
