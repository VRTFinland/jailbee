"""Dedicated SSH keys reject options and survive failed or racing writes."""

from __future__ import annotations

import base64
import builtins
import os
import stat
import subprocess
import sys

import asyncssh
import pytest

from jailbee.remote_ssh import keys
from jailbee.remote_ssh.keys import (
    SSHKeyError,
    SSHPaths,
    add_authorized_key,
    authorized_fingerprint,
    ensure_key_files,
    read_authorized_keys,
    remove_authorized_key,
    ssh_paths,
)

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBsz47IcK4hPdHS7xOXNGafb/Uw3epmEsD7xIJn434n6"
FINGERPRINT = "SHA256:qLBHzrI/tje39Belv8gH7aaz1iprjQMjKh4sbnQnFT4"


@pytest.fixture
def key_paths(tmp_path):
    config = tmp_path / "config" / "ssh"
    data = tmp_path / "data" / "ssh"
    return SSHPaths(config, data, config / "authorized_keys", data / "host_key")


@pytest.fixture
def populated_paths(key_paths):
    key_paths.config_dir.mkdir(parents=True)
    key_paths.authorized_keys.write_bytes(f"# dedicated clients\n\n{PUBLIC_KEY} workstation  \n".encode())
    return key_paths


@pytest.fixture
def without_asyncssh(monkeypatch):
    original = builtins.__import__

    def import_without_asyncssh(name, *args, **kwargs):
        if name == "asyncssh" or name.startswith("asyncssh."):
            raise ImportError("SSH extra is absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_asyncssh)


def test_paths_follow_xdg(private_home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    paths = ssh_paths()
    assert paths.authorized_keys == tmp_path / "cfg/jailbee/ssh/authorized_keys"
    assert paths.host_key == tmp_path / "data/jailbee/ssh/host_key"


def test_paths_default_to_private_home(private_home, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    paths = ssh_paths()
    assert paths.authorized_keys == private_home / ".config/jailbee/ssh/authorized_keys"
    assert paths.host_key == private_home / ".local/share/jailbee/ssh/host_key"
    assert not paths.config_dir.exists()
    assert not paths.data_dir.exists()


def test_missing_authorized_file_reads_empty_without_creating_files(key_paths):
    assert read_authorized_keys(paths=key_paths) == []
    assert not key_paths.config_dir.exists()


def test_fingerprint_uses_exact_wire_blob_and_ignores_comment():
    assert authorized_fingerprint(PUBLIC_KEY + " user@example  ") == FINGERPRINT


def test_add_list_remove_round_trip(key_paths):
    added = add_authorized_key(PUBLIC_KEY + " laptop", paths=key_paths)
    assert read_authorized_keys(paths=key_paths) == [added]
    assert added.algorithm == "ssh-ed25519"
    assert added.public_text == PUBLIC_KEY + " laptop"
    assert added.comment == "laptop"
    assert added.fingerprint == FINGERPRINT
    remove_authorized_key(added.fingerprint, paths=key_paths)
    assert read_authorized_keys(paths=key_paths) == []
    assert not key_paths.host_key.exists()


def test_add_preserves_comment_spacing_and_accepts_one_terminal_newline(key_paths):
    text = PUBLIC_KEY + ' owner  "quoted"\tcomment  '
    added = add_authorized_key(text + "\n", paths=key_paths)
    assert added.comment == 'owner  "quoted"\tcomment  '
    assert added.public_text == text
    assert key_paths.authorized_keys.read_text() == text + "\n"


def test_add_preserves_existing_lines_and_supplies_missing_newline(populated_paths):
    before = populated_paths.authorized_keys.read_bytes().rstrip(b"\n")
    populated_paths.authorized_keys.write_bytes(before)
    second = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
    add_authorized_key(second, paths=populated_paths)
    assert populated_paths.authorized_keys.read_bytes() == before + b"\n" + second.encode() + b"\n"


def test_duplicate_blob_with_different_comment_preserves_original_bytes(populated_paths):
    before = populated_paths.authorized_keys.read_bytes()
    with pytest.raises(SSHKeyError, match="already|duplicate"):
        add_authorized_key(PUBLIC_KEY + " renamed", paths=populated_paths)
    assert populated_paths.authorized_keys.read_bytes() == before


@pytest.mark.parametrize(
    "prefix",
    [
        'command="jailbee"',
        'environment="A=B"',
        'from="127.0.0.1"',
        "@cert-authority",
        "cert-authority",
        "restrict",
        "no-pty",
        'no-pty,command="jailbee"',
    ],
)
def test_options_are_rejected_without_changing_original_bytes(prefix, populated_paths):
    before = populated_paths.authorized_keys.read_bytes()
    with pytest.raises(SSHKeyError):
        add_authorized_key(prefix + " " + PUBLIC_KEY, paths=populated_paths)
    assert populated_paths.authorized_keys.read_bytes() == before


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ssh-ed25519",
        "ssh-ed25519 !!!",
        PUBLIC_KEY.replace("AAAAC", "AAA!AC", 1),
        PUBLIC_KEY + "\n" + PUBLIC_KEY,
        PUBLIC_KEY + "\n# extra source line",
        "\n" + PUBLIC_KEY,
        PUBLIC_KEY + "\n\n",
        PUBLIC_KEY.replace("ssh-ed25519", "ssh-ed25519-cert-v01@openssh.com"),
        PUBLIC_KEY.replace("ssh-ed25519", "ssh-rsa"),
        "ssh-ed25519 AAAA",
        "ssh-ed25519 /////w==",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5",
    ],
)
def test_invalid_source_is_rejected_without_changing_original_bytes(text, populated_paths):
    before = populated_paths.authorized_keys.read_bytes()
    with pytest.raises(SSHKeyError):
        add_authorized_key(text, paths=populated_paths)
    assert populated_paths.authorized_keys.read_bytes() == before


def test_pure_parser_rejects_wire_algorithm_disagreement(populated_paths, without_asyncssh):
    populated_paths.authorized_keys.write_text(PUBLIC_KEY.replace("ssh-ed25519", "ssh-rsa"))
    with pytest.raises(SSHKeyError):
        read_authorized_keys(paths=populated_paths)


def test_add_validates_complete_key_material(key_paths):
    blob = base64.b64decode(PUBLIC_KEY.split()[1])[:-1]
    truncated = "ssh-ed25519 " + base64.b64encode(blob).decode()
    with pytest.raises(SSHKeyError):
        add_authorized_key(truncated, paths=key_paths)
    assert not key_paths.authorized_keys.exists()


def test_remove_keeps_other_lines_byte_for_byte(populated_paths):
    remove_authorized_key(FINGERPRINT, paths=populated_paths)
    assert populated_paths.authorized_keys.read_bytes() == b"# dedicated clients\n\n"


@pytest.mark.parametrize("reference", ["SHA256:unknown", FINGERPRINT[:20], "workstation", ""])
def test_remove_requires_exact_known_full_fingerprint(reference, populated_paths):
    before = populated_paths.authorized_keys.read_bytes()
    with pytest.raises(SSHKeyError):
        remove_authorized_key(reference, paths=populated_paths)
    assert populated_paths.authorized_keys.read_bytes() == before


def test_remove_rejects_ambiguous_duplicate_entries(populated_paths):
    before = populated_paths.authorized_keys.read_bytes() + (PUBLIC_KEY + " duplicate\n").encode()
    populated_paths.authorized_keys.write_bytes(before)
    with pytest.raises(SSHKeyError, match="ambiguous"):
        remove_authorized_key(FINGERPRINT, paths=populated_paths)
    assert populated_paths.authorized_keys.read_bytes() == before


def test_module_import_works_without_extra():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['asyncssh'] = None; import jailbee.remote_ssh.keys",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_list_and_remove_work_without_extra(populated_paths, without_asyncssh):
    assert read_authorized_keys(paths=populated_paths)[0].fingerprint == FINGERPRINT
    assert authorized_fingerprint(PUBLIC_KEY) == FINGERPRINT
    remove_authorized_key(FINGERPRINT, paths=populated_paths)
    assert read_authorized_keys(paths=populated_paths) == []


def test_add_missing_extra_gives_install_instruction_and_preserves_bytes(
    populated_paths, without_asyncssh
):
    before = populated_paths.authorized_keys.read_bytes()
    with pytest.raises(keys.SSHDependencyError) as error:
        add_authorized_key(PUBLIC_KEY, paths=populated_paths)
    assert str(error.value) == (
        "The SSH server requires the optional 'ssh' extra. "
        "Install it with: uv tool install 'jailbee[ssh]'"
    )
    assert populated_paths.authorized_keys.read_bytes() == before


def test_ensure_creates_private_ed25519_host_key_and_empty_authorized_file(key_paths):
    ensure_key_files(paths=key_paths)
    assert asyncssh.read_private_key(key_paths.host_key).get_algorithm() == "ssh-ed25519"
    assert key_paths.authorized_keys.read_bytes() == b""
    for directory in (key_paths.config_dir, key_paths.data_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file in (key_paths.authorized_keys, key_paths.host_key):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600


def test_ensure_existing_files_is_idempotent_and_tightens_permissions(populated_paths):
    ensure_key_files(paths=populated_paths)
    original_key = populated_paths.host_key.read_bytes()
    original_clients = populated_paths.authorized_keys.read_bytes()
    for directory in (populated_paths.config_dir, populated_paths.data_dir):
        directory.chmod(0o755)
    for file in (populated_paths.authorized_keys, populated_paths.host_key):
        file.chmod(0o644)
    ensure_key_files(paths=populated_paths)
    assert populated_paths.host_key.read_bytes() == original_key
    assert populated_paths.authorized_keys.read_bytes() == original_clients
    for directory in (populated_paths.config_dir, populated_paths.data_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file in (populated_paths.authorized_keys, populated_paths.host_key):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600


def test_add_and_remove_enforce_private_permissions(populated_paths):
    populated_paths.config_dir.chmod(0o755)
    populated_paths.authorized_keys.chmod(0o644)
    remove_authorized_key(FINGERPRINT, paths=populated_paths)
    assert stat.S_IMODE(populated_paths.config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(populated_paths.authorized_keys.stat().st_mode) == 0o600
    populated_paths.config_dir.chmod(0o755)
    add_authorized_key(PUBLIC_KEY, paths=populated_paths)
    assert stat.S_IMODE(populated_paths.config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(populated_paths.authorized_keys.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["add", "remove"])
@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_failed_atomic_edit_keeps_original_and_cleans_temp(
    operation, failure, populated_paths, monkeypatch
):
    before = populated_paths.authorized_keys.read_bytes()
    second = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()

    def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(OSError, match="write failed"):
        if operation == "add":
            add_authorized_key(second, paths=populated_paths)
        else:
            remove_authorized_key(FINGERPRINT, paths=populated_paths)
    assert populated_paths.authorized_keys.read_bytes() == before
    assert list(populated_paths.config_dir.iterdir()) == [populated_paths.authorized_keys]


def test_host_key_publication_never_replaces_concurrent_winner(key_paths, monkeypatch):
    winner = asyncssh.generate_private_key("ssh-ed25519").export_private_key()
    link = os.link

    def publish_after_competitor(source, destination):
        if destination == key_paths.host_key:
            key_paths.host_key.write_bytes(winner)
        return link(source, destination)

    monkeypatch.setattr(os, "link", publish_after_competitor)
    ensure_key_files(paths=key_paths)
    assert key_paths.host_key.read_bytes() == winner
    assert stat.S_IMODE(key_paths.host_key.stat().st_mode) == 0o600
    assert list(key_paths.data_dir.iterdir()) == [key_paths.host_key]


def test_failed_host_publication_cleans_temporary_file(key_paths, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("link failed")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError, match="link failed"):
        ensure_key_files(paths=key_paths)
    assert not key_paths.host_key.exists()
    assert list(key_paths.data_dir.iterdir()) == []
