"""Bounded text snapshots of container outbox directories."""

from __future__ import annotations

import base64
import io
import tarfile
from collections.abc import Callable

from jailbee.incus import Incus, IncusError
from jailbee.tui import warn


class OutboxReadError(Exception):
    """A container outbox could not be read into a bounded text snapshot."""


def read_text_outbox(
    incus: Incus,
    container: str,
    directory: str,
    *,
    uid: int | None,
    max_file_bytes: int,
    timeout: int = 15,
    warn_fn: Callable[[str], None] = warn,
) -> dict[str, str]:
    """Read regular UTF-8 files from one container directory in one round trip."""
    try:
        raw = incus.exec(
            container,
            [
                "bash",
                "-c",
                'cd "$1" 2>/dev/null || exit 0; tar -cf - . | base64 -w0',
                "bash",
                directory,
            ],
            uid=uid,
            timeout=timeout,
        )
    except IncusError as e:
        raise OutboxReadError(f"could not read the outbox in {container}: {e}") from e
    if not raw.strip():
        return {}
    try:
        blob = base64.b64decode(raw.strip(), validate=True)
    except ValueError as e:  # binascii.Error (invalid base64) is a ValueError subclass
        raise OutboxReadError(f"{container} returned an unreadable outbox archive") from e

    files: dict[str, str] = {}
    skipped = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            for member in tar.getmembers():
                name = member.name
                if name == "." and member.isdir():
                    continue
                if name.startswith("./"):
                    name = name[2:]
                if (
                    not member.isfile()
                    or not name
                    or name.startswith("/")
                    or "/" in name
                    or name == ".."
                    or member.size > max_file_bytes
                ):
                    skipped += 1
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    skipped += 1
                    continue
                try:
                    files[name] = extracted.read().decode("utf-8")
                except UnicodeDecodeError:
                    skipped += 1
    except tarfile.TarError as e:
        raise OutboxReadError(f"{container} returned a corrupt outbox archive: {e}") from e
    if skipped:
        warn_fn(f"{container}: skipped {skipped} hostile outbox member(s)")
    return files
