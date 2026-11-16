import base64
import hashlib

import pytest

from jailbee.container_outbox.attachments import capture_inputs


def test_capture_streams_and_replaces_source(tmp_path):
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 400000
    (tmp_path / "image.png").write_bytes(data)
    received = bytearray()
    metadata = {}

    def send(op, args):
        if op.endswith("begin"):
            metadata.update(args)
            return {"blob": "token"}
        if op.endswith("chunk"):
            chunk = base64.b64decode(args["data"])
            assert len(chunk) <= 192 * 1024
            assert args["offset"] == len(received)
            received.extend(chunk)
            return {"offset": len(received)}
        return {"blob": "token", **metadata}

    manifest = {"attachments": {"picture": {"file": "image.png"}}, "title": "test"}
    result = capture_inputs(manifest, tmp_path, send=send)
    assert received == data
    assert result["attachments"] == {
        "picture": {
            "blob": "token",
            "name": "image.png",
            "content_type": "image/png",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    }
    assert manifest["attachments"] == {"picture": {"file": "image.png"}}


def test_rejects_symlink_and_fifo(tmp_path):
    import os

    (tmp_path / "real.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "link.png").symlink_to("real.png")
    os.mkfifo(tmp_path / "fifo.png")
    for name in ("link.png", "fifo.png"):
        with pytest.raises((ValueError, OSError)):
            capture_inputs(
                {"attachments": {"a": {"file": name}}},
                tmp_path,
                send=lambda *args: pytest.fail("network call"),
            )


def test_mutation_during_transfer_is_rejected(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 400000)
    operations = []

    def send(op, args):
        operations.append(op)
        if op.endswith("begin"):
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"y" * 400000)
            return {"blob": "token"}
        return {}

    with pytest.raises(ValueError, match="changed"):
        capture_inputs({"attachments": {"a": {"file": "a.png"}}}, tmp_path, send=send)
    assert not any(op.endswith("finish") for op in operations)


def test_invalid_input_is_rejected_before_network(tmp_path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    manifest = {"attachments": {"a": {"file": "a.png"}, "b": {"wrong": "value"}}}
    with pytest.raises(ValueError):
        capture_inputs(manifest, tmp_path, send=lambda *args: pytest.fail("network call"))
