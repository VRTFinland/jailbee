"""The in-container identity snapshot, `/etc/jailbee/container.json`."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from jailbee.container_info import CONTAINER_INFO_FILE, build_payload, write_container_info
from tests.conftest import make_config


def _cfg(tmp_path):
    return make_config(tmp_path, container_prefix="repo")


def test_payload_names_container_and_repo_selector(tmp_path):
    payload = build_payload(
        _cfg(tmp_path),
        "repo-feat-x",
        mode="clone",
        branch="feat/x",
        base_branch="main",
        repo_dir="/home/dev/repo",
        pr=None,
        fork_of=None,
    )

    assert payload["version"] == 1
    assert payload["name"] == "repo-feat-x"
    assert payload["short_name"] == "feat-x"
    assert payload["container_prefix"] == "repo"
    assert payload["mode"] == "clone"
    assert payload["branch"] == "feat/x"
    assert payload["base_branch"] == "main"
    assert payload["repo_dir"] == "/home/dev/repo"


def test_payload_carries_pr_and_fork_when_present(tmp_path):
    payload = build_payload(
        _cfg(tmp_path),
        "repo-pr-7",
        mode="clone",
        branch="feat/y",
        base_branch="main",
        repo_dir="/home/dev/repo",
        pr=7,
        fork_of="repo-feat-x",
    )

    assert payload["pr"] == 7
    assert payload["fork_of"] == "repo-feat-x"


def test_payload_leaves_branch_pr_and_fork_null_for_a_mount_container(tmp_path):
    payload = build_payload(
        _cfg(tmp_path),
        "repo-box",
        mode="mount",
        branch=None,
        base_branch="main",
        repo_dir="/home/dev/repo",
        pr=None,
        fork_of=None,
    )

    assert payload["mode"] == "mount"
    assert payload["branch"] is None
    assert payload["pr"] is None
    assert payload["fork_of"] is None


def test_payload_omits_volatile_and_host_private_state(tmp_path):
    payload = build_payload(
        _cfg(tmp_path),
        "repo-feat-x",
        mode="clone",
        branch="feat/x",
        base_branch="main",
        repo_dir="/home/dev/repo",
        pr=None,
        fork_of=None,
    )

    for key in ("loose_until", "alias", "credential_group", "host_repo_root"):
        assert key not in payload


def test_write_installs_readable_json_atomically(tmp_path):
    incus = MagicMock()
    payload = {"version": 1, "name": "repo-feat-x"}

    write_container_info(incus, "repo-feat-x", payload)

    name, cmd, script = incus.exec_with_input.call_args.args[:3]
    assert name == "repo-feat-x"
    assert cmd == ["bash", "-s"]
    assert CONTAINER_INFO_FILE == "/etc/jailbee/container.json"
    assert f'mv "$tmp" {CONTAINER_INFO_FILE}' in script
    assert "chmod 0644" in script
    body = script.split("<<'JB_EOF'\n", 1)[1].split("\nJB_EOF", 1)[0]
    assert json.loads(body) == payload
