import io
import json
import subprocess
import sys

import pytest

from jailbee.orchestration import wire
from jailbee.orchestration.wire import (
    BAD_REQUEST,
    MAX_LINE_BYTES,
    VERSION,
    WireError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    fail,
    ok,
    read_line,
)


def test_request_round_trip():
    req = wire.Request(op="send", args={"manifest": {"id": "a"}, "wait": True})
    line = encode_request(req)
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert json.loads(line) == {
        "v": VERSION,
        "op": "send",
        "args": {"manifest": {"id": "a"}, "wait": True},
    }
    assert decode_request(line) == req


def test_request_args_default_to_empty_dict():
    req = decode_request(b'{"v": 1, "op": "list"}\n')
    assert req == wire.Request(op="list", args={})


def test_ok_response_round_trip():
    resp = ok({"kind": "timeout"})
    assert resp == wire.Response(ok=True, result={"kind": "timeout"})
    line = encode_response(resp)
    assert json.loads(line) == {"v": VERSION, "ok": True, "result": {"kind": "timeout"}}
    assert decode_response(line) == resp


def test_error_response_round_trip():
    resp = fail(wire.NOT_FOUND, "no item x")
    assert resp.ok is False
    assert resp.error_code == wire.NOT_FOUND
    line = encode_response(resp)
    assert json.loads(line) == {
        "v": VERSION,
        "ok": False,
        "error": {"code": "not_found", "detail": "no item x"},
    }
    assert decode_response(line) == resp


def test_error_codes_frozenset():
    assert wire.ERROR_CODES == frozenset(
        {
            "version",
            "bad_request",
            "not_orchestrated",
            "store",
            "refused",
            "busy",
            "not_found",
            "duplicate",
        }
    )


def test_dataclasses_are_frozen():
    req = wire.Request(op="x", args={})
    with pytest.raises(AttributeError):
        req.op = "y"  # type: ignore[misc]  # frozen dataclass assignment is the point


def test_encode_request_rejects_oversize():
    req = wire.Request(op="send", args={"body": "x" * MAX_LINE_BYTES})
    with pytest.raises(WireError) as exc:
        encode_request(req)
    assert exc.value.code == BAD_REQUEST


def test_encode_response_rejects_oversize():
    with pytest.raises(WireError) as exc:
        encode_response(ok("x" * MAX_LINE_BYTES))
    assert exc.value.code == BAD_REQUEST


def test_decode_rejects_oversize():
    line = b'{"v":1,"op":"x","args":{"s":"' + b"a" * MAX_LINE_BYTES + b'"}}\n'
    with pytest.raises(WireError) as exc:
        decode_request(line)
    assert exc.value.code == BAD_REQUEST
    with pytest.raises(WireError) as exc:
        decode_response(line)
    assert exc.value.code == BAD_REQUEST


def test_decode_rejects_bad_json():
    with pytest.raises(WireError) as exc:
        decode_request(b"{not json\n")
    assert exc.value.code == BAD_REQUEST


def test_decode_rejects_invalid_utf8():
    with pytest.raises(WireError) as exc:
        decode_request(b'{"v":1,"op":"\xff"}\n')
    assert exc.value.code == BAD_REQUEST


def test_decode_rejects_non_object():
    with pytest.raises(WireError) as exc:
        decode_request(b'["send"]\n')
    assert exc.value.code == BAD_REQUEST
    with pytest.raises(WireError) as exc:
        decode_response(b"42\n")
    assert exc.value.code == BAD_REQUEST


def test_decode_rejects_missing_or_non_str_op():
    with pytest.raises(WireError) as exc:
        decode_request(b'{"v":1,"args":{}}\n')
    assert exc.value.code == BAD_REQUEST
    with pytest.raises(WireError) as exc:
        decode_request(b'{"v":1,"op":3,"args":{}}\n')
    assert exc.value.code == BAD_REQUEST


def test_decode_rejects_non_dict_args():
    with pytest.raises(WireError) as exc:
        decode_request(b'{"v":1,"op":"x","args":[1]}\n')
    assert exc.value.code == BAD_REQUEST


def test_decode_version_mismatch_is_version_error():
    with pytest.raises(WireError) as exc:
        decode_request(b'{"v":2,"op":"x","args":{}}\n')
    assert exc.value.code == wire.VERSION_ERROR
    with pytest.raises(WireError) as exc:
        decode_response(b'{"v":2,"ok":true,"result":null}\n')
    assert exc.value.code == wire.VERSION_ERROR


def test_decode_response_requires_bool_ok():
    with pytest.raises(WireError) as exc:
        decode_response(b'{"v":1,"ok":"yes"}\n')
    assert exc.value.code == BAD_REQUEST


def test_decode_response_error_must_be_object_with_str_fields():
    with pytest.raises(WireError) as exc:
        decode_response(b'{"v":1,"ok":false,"error":"boom"}\n')
    assert exc.value.code == BAD_REQUEST
    with pytest.raises(WireError) as exc:
        decode_response(b'{"v":1,"ok":false,"error":{"code":1,"detail":"x"}}\n')
    assert exc.value.code == BAD_REQUEST


def test_read_line_returns_line_then_none_at_eof():
    buf = io.BytesIO(b'{"a":1}\n{"b":2}\n')
    assert read_line(buf) == b'{"a":1}\n'
    assert read_line(buf) == b'{"b":2}\n'
    assert read_line(buf) is None


def test_read_line_empty_stream_is_eof():
    assert read_line(io.BytesIO(b"")) is None


def test_read_line_rejects_oversize():
    buf = io.BytesIO(b"x" * (MAX_LINE_BYTES + 10) + b"\n")
    with pytest.raises(WireError) as exc:
        read_line(buf)
    assert exc.value.code == BAD_REQUEST


def test_read_line_accepts_exact_cap_with_newline():
    body = b"x" * (MAX_LINE_BYTES - 1) + b"\n"
    assert len(body) == MAX_LINE_BYTES
    assert read_line(io.BytesIO(body)) == body


def test_read_line_rejects_missing_newline_at_cap():
    buf = io.BytesIO(b"x" * (MAX_LINE_BYTES + 5))
    with pytest.raises(WireError) as exc:
        read_line(buf)
    assert exc.value.code == BAD_REQUEST


def test_read_line_rejects_exactly_cap_without_newline_at_eof():
    with pytest.raises(WireError) as exc:
        read_line(io.BytesIO(b"x" * MAX_LINE_BYTES))
    assert exc.value.code == BAD_REQUEST
    assert "without newline" in exc.value.detail


@pytest.mark.parametrize("v", ["true", "1.0", '"1"', "null"])
def test_decode_rejects_non_int_versions(v):
    with pytest.raises(WireError) as exc:
        wire.decode_request(f'{{"v":{v},"op":"x"}}'.encode())
    assert exc.value.code == wire.VERSION_ERROR


def test_encode_rejects_non_serialisable_result():
    with pytest.raises(WireError) as exc:
        encode_response(ok({"x": object()}))
    assert exc.value.code == BAD_REQUEST


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_encode_rejects_nan(bad):
    with pytest.raises(WireError) as exc:
        encode_response(ok({"x": bad}))
    assert exc.value.code == BAD_REQUEST
    with pytest.raises(WireError):
        wire.encode_request(wire.Request(op="x", args={"x": bad}))


def test_wire_is_stdlib_only():
    code = (
        "import jailbee.orchestration.wire; import sys;"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'pydantic','yaml','typer','rich'} or m in "
        "{'jailbee.config','jailbee.orchestration.outbox','jailbee.orchestration.protocol'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
