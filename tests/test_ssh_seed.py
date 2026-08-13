"""Tests for ssh_seed — seeds host ~/.ssh into <shared_dir>/ssh."""

from __future__ import annotations

import os
from pathlib import Path

from jailbee.ssh_seed import seed_ssh_dir


def test_returns_zero_when_host_ssh_missing(tmp_path: Path) -> None:
    target = tmp_path / "ssh"
    target.mkdir()
    host = tmp_path / "no-such-dir"  # does not exist

    n = seed_ssh_dir(target, host)

    assert n == 0
    assert list(target.iterdir()) == []


def test_returns_zero_when_host_ssh_is_file_not_dir(tmp_path: Path) -> None:
    target = tmp_path / "ssh"
    target.mkdir()
    host = tmp_path / "not-a-dir"
    host.write_text("oops")

    n = seed_ssh_dir(target, host)

    assert n == 0
    assert list(target.iterdir()) == []


def test_seeds_allowlist_files(tmp_path: Path) -> None:
    target = tmp_path / "ssh"
    target.mkdir()

    host = tmp_path / "host_ssh"
    host.mkdir()
    (host / "config").write_text("Host github.com\n  User git\n")
    (host / "known_hosts").write_text("github.com ssh-ed25519 AAAA...\n")

    n = seed_ssh_dir(target, host)

    assert n == 2
    assert (target / "config").read_text() == "Host github.com\n  User git\n"
    assert (target / "known_hosts").read_text() == "github.com ssh-ed25519 AAAA...\n"


def test_partial_host_files(tmp_path: Path) -> None:
    """Missing allowlist files are skipped silently."""
    target = tmp_path / "ssh"
    target.mkdir()

    host = tmp_path / "host_ssh"
    host.mkdir()
    (host / "config").write_text("Host *\n  IdentityAgent SSH_AUTH_SOCK\n")
    # No known_hosts.

    n = seed_ssh_dir(target, host)

    assert n == 1
    assert (target / "config").exists()
    assert not (target / "known_hosts").exists()


def test_skips_when_target_non_empty(tmp_path: Path) -> None:
    """Pre-populated target must not be overwritten. Idempotent re-init."""
    target = tmp_path / "ssh"
    target.mkdir()
    (target / "preexisting").write_text("container wrote me")

    host = tmp_path / "host_ssh"
    host.mkdir()
    (host / "config").write_text("Host *")

    n = seed_ssh_dir(target, host)

    assert n == 0
    assert (target / "preexisting").read_text() == "container wrote me"
    assert not (target / "config").exists()


def test_seeds_config_d_dir(tmp_path: Path) -> None:
    """config.d/ is copied recursively with nested files preserved."""
    target = tmp_path / "ssh"
    target.mkdir()

    host = tmp_path / "host_ssh"
    host.mkdir()
    (host / "config.d").mkdir()
    (host / "config.d" / "personal").write_text("Host personal-server")
    (host / "config.d" / "work").mkdir()
    (host / "config.d" / "work" / "bastion").write_text("Host work-bastion")

    n = seed_ssh_dir(target, host)

    assert n == 1
    assert (target / "config.d" / "personal").read_text() == "Host personal-server"
    assert (target / "config.d" / "work" / "bastion").read_text() == "Host work-bastion"


def test_partial_host_dirs(tmp_path: Path) -> None:
    """Missing allowlist dirs are skipped silently."""
    target = tmp_path / "ssh"
    target.mkdir()

    host = tmp_path / "host_ssh"
    host.mkdir()
    (host / "config").write_text("Host *")
    # No config.d.

    n = seed_ssh_dir(target, host)

    assert n == 1
    assert (target / "config").exists()
    assert not (target / "config.d").exists()


def test_preserves_config_mode_0600(tmp_path: Path) -> None:
    """shutil.copy2 preserves mode — host `config` 0600 → target 0600."""
    target = tmp_path / "ssh"
    target.mkdir()
    host = tmp_path / "host_ssh"
    host.mkdir()
    cfg = host / "config"
    cfg.write_text("Host *\n  IdentityAgent ${SSH_AUTH_SOCK}\n")
    os.chmod(cfg, 0o600)

    n = seed_ssh_dir(target, host)

    assert n == 1
    mode = (target / "config").stat().st_mode & 0o777
    assert mode == 0o600, f"Expected mode 0600, got {oct(mode)}"


def test_ignores_unlisted_items(tmp_path: Path) -> None:
    """Anything not in SEED_FILES/SEED_DIRS is ignored.

    Protects against accidentally copying:
    - private keys (delivered via SSH_AUTH_SOCK, not seeded)
    - authorized_keys (containers don't accept inbound SSH)
    - ControlMaster sockets (must be container-local)
    - any user-added files
    """
    target = tmp_path / "ssh"
    target.mkdir()
    host = tmp_path / "host_ssh"
    host.mkdir()
    # Items that explicitly must not be copied:
    (host / "id_rsa").write_text("PRIVATE KEY")
    (host / "id_rsa.pub").write_text("PUBLIC KEY")
    (host / "id_ed25519").write_text("PRIVATE KEY 2")
    (host / "authorized_keys").write_text("inbound key")
    (host / "authorized_keys2").write_text("legacy")
    (host / "environment").write_text("MY_VAR=1")
    (host / "cm-git@github.com:22").write_text("socket placeholder")
    (host / "agent.sock").write_text("socket placeholder")
    (host / "random_user_file").write_text("nope")
    # Plus one allowlisted item to confirm the seed still ran:
    (host / "config").write_text("Host *")

    n = seed_ssh_dir(target, host)

    assert n == 1
    assert (target / "config").exists()
    for unlisted in (
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "authorized_keys",
        "authorized_keys2",
        "environment",
        "cm-git@github.com:22",
        "agent.sock",
        "random_user_file",
    ):
        assert not (target / unlisted).exists(), f"Unlisted item leaked: {unlisted}"
