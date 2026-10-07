"""Shared test fixtures for the dashboard suite."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from jailbee.dashboard import model as dmodel
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
