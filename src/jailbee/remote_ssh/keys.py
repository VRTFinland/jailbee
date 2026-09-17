"""Manage dedicated client keys and the persistent SSH server identity."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jailbee.global_config import default_global_config_path
from jailbee.paths import xdg_data_home


@dataclass(frozen=True)
class SSHPaths:
    config_dir: Path
    data_dir: Path
    authorized_keys: Path
    host_key: Path


@dataclass(frozen=True)
class AuthorizedKey:
    algorithm: str
    public_text: str
    comment: str
    fingerprint: str


class SSHKeyError(ValueError):
    """A client key or key selection is invalid."""


class SSHDependencyError(RuntimeError):
    """The optional SSH dependency is unavailable."""


def ssh_paths() -> SSHPaths:
    """Resolve dedicated key files using Jailbee's XDG locations."""
    config_dir = default_global_config_path().parent / "ssh"
    data_dir = xdg_data_home() / "jailbee" / "ssh"
    return SSHPaths(config_dir, data_dir, config_dir / "authorized_keys", data_dir / "host_key")


def _asyncssh() -> Any:
    try:
        import asyncssh
    except ImportError as exc:
        raise SSHDependencyError(
            "The SSH server requires the optional 'ssh' extra. "
            "Install it with: uv tool install 'jailbee[ssh]'"
        ) from exc
    return asyncssh


def _parse_public_key(text: str) -> AuthorizedKey:
    lines = text.splitlines()
    if len(lines) != 1:
        raise SSHKeyError("expected a single plain OpenSSH public key")
    match = re.fullmatch(r"[ \t]*([^\s]+)[ \t]+([^\s]+)(?:[ \t](.*))?", lines[0])
    if match is None:
        raise SSHKeyError("expected a plain OpenSSH public key with algorithm and base64 data")
    algorithm, encoded, comment = match.groups()
    if algorithm not in {
        "ssh-ed25519",
        "ssh-ed448",
        "ssh-rsa",
        "ssh-dss",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }:
        raise SSHKeyError("expected a plain public key; options and certificates are not supported")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SSHKeyError("invalid public key base64 data") from exc
    if base64.b64encode(blob).decode("ascii") != encoded:
        raise SSHKeyError("invalid public key base64 encoding")
    size = int.from_bytes(blob[:4], "big")
    if (
        len(blob) < 4
        or size == 0
        or 4 + size >= len(blob)
        or blob[4 : 4 + size] != algorithm.encode("ascii")
    ):
        raise SSHKeyError("public key wire algorithm does not match its text algorithm")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    public_text = f"{algorithm} {encoded}"
    if comment is not None:
        public_text += " " + comment
    return AuthorizedKey(algorithm, public_text, comment or "", fingerprint)


def authorized_fingerprint(public_text: str) -> str:
    """Fingerprint the exact SSH blob without loading the optional SSH library."""
    return _parse_public_key(public_text).fingerprint


def _read_entries(path: Path) -> list[tuple[bytes, AuthorizedKey | None]]:
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return []
    entries: list[tuple[bytes, AuthorizedKey | None]] = []
    for line in contents.splitlines(keepends=True):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SSHKeyError("authorized keys must be UTF-8 text") from exc
        key = None if not text.strip() or text.lstrip().startswith("#") else _parse_public_key(text)
        entries.append((line, key))
    return entries


def read_authorized_keys(*, paths: SSHPaths | None = None) -> list[AuthorizedKey]:
    """Read the current client keys, ignoring blank and comment-only lines."""
    paths = paths or ssh_paths()
    return [key for _, key in _read_entries(paths.authorized_keys) if key is not None]


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_key_file(target: Path, contents: bytes, *, no_replace: bool = False) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o600)
        if no_replace:
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        else:
            os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ensure_key_files(*, paths: SSHPaths | None = None) -> None:
    """Create private key files without replacing an existing server identity."""
    paths = paths or ssh_paths()
    _private_directory(paths.config_dir)
    _private_directory(paths.data_dir)
    try:
        descriptor = os.open(paths.authorized_keys, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    paths.authorized_keys.chmod(0o600)
    if not paths.host_key.exists():
        key = _asyncssh().generate_private_key("ssh-ed25519")
        _write_key_file(paths.host_key, key.export_private_key("openssh"), no_replace=True)
    paths.host_key.chmod(0o600)


def add_authorized_key(public_text: str, *, paths: SSHPaths | None = None) -> AuthorizedKey:
    """Validate and atomically add one plain key, preserving its comment."""
    paths = paths or ssh_paths()
    key = _parse_public_key(public_text)
    asyncssh = _asyncssh()
    try:
        asyncssh.import_public_key(" ".join(key.public_text.split()[:2]))
    except (ValueError, asyncssh.KeyImportError) as exc:
        raise SSHKeyError(f"invalid public key: {exc}") from exc
    entries = _read_entries(paths.authorized_keys)
    if any(existing and existing.fingerprint == key.fingerprint for _, existing in entries):
        raise SSHKeyError(f"public key already authorized: {key.fingerprint}")
    contents = b"".join(line for line, _ in entries)
    if contents and not contents.endswith(b"\n"):
        contents += b"\n"
    contents += (key.public_text + "\n").encode("utf-8")
    _private_directory(paths.config_dir)
    _write_key_file(paths.authorized_keys, contents)
    return key


def remove_authorized_key(fingerprint: str, *, paths: SSHPaths | None = None) -> None:
    """Atomically remove exactly one key selected by its full fingerprint."""
    paths = paths or ssh_paths()
    entries = _read_entries(paths.authorized_keys)
    matches = [index for index, (_, key) in enumerate(entries) if key and key.fingerprint == fingerprint]
    if not matches:
        raise SSHKeyError(f"unknown full public key fingerprint: {fingerprint}")
    if len(matches) != 1:
        raise SSHKeyError(f"ambiguous public key fingerprint: {fingerprint}")
    contents = b"".join(line for index, (line, _) in enumerate(entries) if index != matches[0])
    _private_directory(paths.config_dir)
    _write_key_file(paths.authorized_keys, contents)
