"""Fakes for the remote-SSH file-transfer tests.

`LocalIncus` stands in for `Incus`: instead of `incus exec` it runs the same
argv on the local machine, so the container scripts execute for real against a
`tmp_path`. It is a test double, not production code.
"""

from __future__ import annotations

import subprocess
from typing import Any

from jailbee.incus import ExecResult


def raw_container(
    name: str,
    *,
    repo: str = "app",
    repo_dir: str | None = None,
    status: str = "Running",
    mode: str | None = None,
) -> dict[str, Any]:
    config: dict[str, str] = {}
    if repo_dir is not None:
        config["user.jailbee.repo_dir"] = repo_dir
    if mode is not None:
        config["user.jailbee.mode"] = mode
    return {
        "name": name,
        "status": status,
        "profiles": ["default", f"{repo}-base"],
        "config": config,
    }


class LocalIncus:
    def __init__(self, containers: list[dict[str, Any]] | None = None) -> None:
        self.containers = containers or []
        self.calls: list[tuple[str, list[str], int | None, int | None]] = []

    def list_containers(
        self, *, fast: bool = False, timeout: int | None = None
    ) -> list[dict[str, Any]]:
        return self.containers

    def exec_bytes(
        self,
        name: str,
        cmd: list[str],
        *,
        input_bytes: bytes | None = None,
        uid: int | None = None,
        gid: int | None = None,
        cwd: str | None = None,
        timeout: int | None = None,
        max_bytes: int | None = None,
    ) -> ExecResult:
        self.calls.append((name, cmd, uid, gid))
        if cmd[:3] == ["stat", "-c", "%u %g"]:
            return ExecResult(0, b"1000 1000\n", b"")
        done = subprocess.run(
            cmd, input=input_bytes, capture_output=True, timeout=timeout, check=False
        )
        return ExecResult(done.returncode, done.stdout, done.stderr)
