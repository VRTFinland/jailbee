from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from jailbee.orchestration.protocol import (
    MAX_PROMPT_BYTES,
    DestroyManifest,
    InboxMessage,
    InboxStatus,
    ManifestError,
    MessageManifest,
    RestartManifest,
    SpawnManifest,
    body_file_of,
    inbox_file_name,
    new_host_id,
    parse_manifest,
    restart_sets_runner,
)
from jailbee.orchestration.runner import Runner

RIGHTS = {"version": 1, "mounts": [], "loose": False, "egress": []}


def _spawn(name: str = "a", **over: Any) -> str:
    d: dict[str, Any] = {
        "version": 1,
        "id": name,
        "type": "spawn",
        "kind": "fork",
        "branch": "fix/1",
        "prompt": "do it",
        "rights": RIGHTS,
    }
    d.update(over)
    return json.dumps({k: v for k, v in d.items() if v is not None})


def test_update_manifest_parses() -> None:
    m = parse_manifest(
        "a.json", json.dumps({"version": 1, "id": "a", "type": "update", "child": "c"}), {}
    )
    assert m.type == "update" and m.child == "c"


def test_spawn_on_done_values() -> None:
    for value in ("destroy", "keep", None):
        m = parse_manifest("a.json", _spawn(on_done=value), {})
        assert m.on_done == value
    with pytest.raises(ManifestError):
        parse_manifest("a.json", _spawn(on_done="later"), {})


def test_restart_on_done_absent_is_none() -> None:
    m = RestartManifest(version=1, id="a", type="restart", child="c", prompt="go")
    assert m.on_done is None


def test_inbox_message_import_fields_round_trip() -> None:
    m = InboxMessage(
        version=1,
        id="h-1",
        type="message",
        from_="c",
        body="done",
        imported_ref="refs/jailbee/from/c/b",
        imported_sha="a" * 40,
        import_error=None,
        on_done="destroy",
    )
    assert InboxMessage.model_validate(m.model_dump(by_alias=True)) == m


def test_finish_file_name_sorts_with_inbox_names() -> None:
    from jailbee.orchestration.protocol import PendingFinish, finish_file_name

    item = PendingFinish(
        version=1,
        type="finish",
        id="f-1",
        child_full="r-c",
        child_short="c",
        branch="b",
        done_sha="a" * 40,
        body="done",
        in_reply_to=None,
        uid=1000,
    )
    msg = InboxMessage(version=1, id="h-1", type="message", from_="c", body="later")
    now = datetime(2026, 10, 26, tzinfo=UTC)
    assert finish_file_name(item, now, seq=1).endswith(".finish.json")
    assert finish_file_name(item, now, seq=1) < inbox_file_name(msg, now, seq=2)


def test_spawn_from_inline_prompt() -> None:
    m = parse_manifest("fix-1.json", _spawn("fix-1"), {})
    assert isinstance(m, SpawnManifest) and m.prompt == "do it" and m.base is None


def test_prompt_file_resolved_from_files() -> None:
    text = _spawn(prompt=None, prompt_file="p.md")
    m = parse_manifest("a.json", text, {"p.md": "from file"})
    assert isinstance(m, SpawnManifest) and m.prompt == "from file"


def test_prompt_and_prompt_file_both_refused() -> None:
    with pytest.raises(ManifestError, match="exactly one"):
        parse_manifest("a.json", _spawn(prompt_file="p.md"), {"p.md": "x"})


def test_neither_prompt_nor_file_refused() -> None:
    with pytest.raises(ManifestError, match="exactly one"):
        parse_manifest("a.json", _spawn(prompt=None), {})


def test_prompt_file_must_match_pattern() -> None:
    for bad in ["../x.md", "x.txt", "X.md", ".md", "a/b.md"]:
        text = _spawn(prompt=None, prompt_file=bad)
        with pytest.raises(ManifestError):
            parse_manifest("a.json", text, {bad: "x"})


def test_missing_prompt_file_refused() -> None:
    with pytest.raises(ManifestError, match="not found"):
        parse_manifest("a.json", _spawn(prompt=None, prompt_file="p.md"), {})


def test_id_must_equal_stem() -> None:
    with pytest.raises(ManifestError, match="file stem"):
        parse_manifest("a.json", _spawn("b"), {})


def test_id_pattern() -> None:
    with pytest.raises(ManifestError):
        parse_manifest("A.json", _spawn("A"), {})


def test_unknown_field_refused() -> None:
    with pytest.raises(ManifestError):
        parse_manifest("a.json", _spawn(extra="x"), {})


def test_invalid_json_and_non_object_refused() -> None:
    with pytest.raises(ManifestError, match="JSON"):
        parse_manifest("a.json", "{nope", {})
    with pytest.raises(ManifestError, match="object"):
        parse_manifest("a.json", "[]", {})


def test_unknown_type_refused() -> None:
    with pytest.raises(ManifestError):
        parse_manifest("a.json", json.dumps({"version": 1, "id": "a", "type": "x"}), {})


def test_prompt_over_cap_refused() -> None:
    with pytest.raises(ManifestError, match="exceeds"):
        parse_manifest("a.json", _spawn(prompt="x" * (MAX_PROMPT_BYTES + 1)), {})
    parse_manifest("a.json", _spawn(prompt="x" * MAX_PROMPT_BYTES), {})


def test_base_only_for_new() -> None:
    with pytest.raises(ManifestError, match="base"):
        parse_manifest("a.json", _spawn(base="main"), {})
    m = parse_manifest("a.json", _spawn(kind="new", base="main"), {})
    assert isinstance(m, SpawnManifest) and m.base == "main"


def test_child_message_has_kind_and_no_to() -> None:
    text = json.dumps({"version": 1, "id": "m1", "type": "message", "body": "hi", "kind": "done"})
    m = parse_manifest("m1.json", text, {})
    assert isinstance(m, MessageManifest) and m.kind == "done" and m.to is None


@pytest.mark.parametrize("reply", ["h-0000abcd", "m1"])
def test_message_in_reply_to_accepts_ids(reply: str) -> None:
    text = json.dumps(
        {"version": 1, "id": "m1", "type": "message", "body": "hi", "in_reply_to": reply}
    )
    m = parse_manifest("m1.json", text, {})
    assert isinstance(m, MessageManifest) and m.in_reply_to == reply


@pytest.mark.parametrize("reply", ["", "UPPER", "x\u001b]52;c;Zm9v\u0007", "a" * 65, "-x"])
def test_message_in_reply_to_must_be_an_id(reply: str) -> None:
    """It is relayed verbatim into the other container's inbox."""
    text = json.dumps(
        {"version": 1, "id": "m1", "type": "message", "body": "hi", "in_reply_to": reply}
    )
    with pytest.raises(ManifestError, match="in_reply_to"):
        parse_manifest("m1.json", text, {})


def test_message_body_file() -> None:
    text = json.dumps({"version": 1, "id": "m1", "type": "message", "to": "x", "body_file": "b.md"})
    m = parse_manifest("m1.json", text, {"b.md": "long"})
    assert isinstance(m, MessageManifest) and m.body == "long" and m.to == "x"


def test_destroy_force_default() -> None:
    text = json.dumps({"version": 1, "id": "d", "type": "destroy", "child": "c"})
    m = parse_manifest("d.json", text, {})
    assert isinstance(m, DestroyManifest) and m.force is False


def test_restart_prompt_file() -> None:
    text = json.dumps(
        {"version": 1, "id": "r", "type": "restart", "child": "c", "prompt_file": "p.md"}
    )
    assert parse_manifest("r.json", text, {"p.md": "again"}).model_dump()["prompt"] == "again"


def test_body_file_of() -> None:
    assert body_file_of("a.json", _spawn(prompt=None, prompt_file="p.md")) == "p.md"
    assert body_file_of("a.json", json.dumps({"body_file": "b.md"})) == "b.md"
    assert body_file_of("a.json", _spawn()) is None
    assert body_file_of("a.json", "{broken") is None
    assert body_file_of("a.json", json.dumps({"prompt_file": "../x.md"})) is None


def test_inbox_file_name_format() -> None:
    s = InboxStatus(
        version=1,
        id="h-0123abcd",
        type="status",
        ref="fix-1",
        state="queued",
        detail="queued at position 1",
    )
    now = datetime(2026, 10, 25, 8, 0, 0, tzinfo=UTC)
    assert inbox_file_name(s, now) == "20261025T080000Z-00000000-h-0123abcd.json"
    assert inbox_file_name(s, now, seq=42) == "20261025T080000Z-00000042-h-0123abcd.json"


def test_inbox_names_sort_in_emission_order_within_a_second() -> None:
    """Random ids must not reorder two items written in the same second."""
    now = datetime(2026, 10, 25, 8, 0, 0, tzinfo=UTC)
    first = InboxStatus(
        version=1, id="h-ffffffff", type="status", ref="r", state="queued", detail="q"
    )
    second = InboxStatus(
        version=1, id="h-00000000", type="status", ref="r", state="started", detail="s"
    )
    names = [inbox_file_name(first, now, seq=1), inbox_file_name(second, now, seq=2)]
    assert sorted(names) == names


def test_inbox_message_serialises_from_key() -> None:
    m = InboxMessage(version=1, id="h-0123abcd", type="message", from_="orch", body="hi")
    assert m.model_dump(by_alias=True)["from"] == "orch"
    parsed = InboxMessage.model_validate(
        {"version": 1, "id": "x", "type": "message", "from": "orch", "body": "b"}
    )
    assert parsed.from_ == "orch"


def test_new_host_id_shape() -> None:
    import re

    assert re.fullmatch(r"h-[0-9a-f]{8}", new_host_id())


BAD_REFS = [
    "",
    "-rf",
    "--upload-pack=x",
    "fix 1",
    "fix\t1",
    "fix\n1",
    "fix\x7f",
    "a..b",
    "fix.lock",
    "fix/a.lock",
    "fix/",
    "/fix",
    "fix//1",
    "fix/.hidden",
    "a@{1}",
    "@",
    "a~1",
    "a^",
    "a:b",
    "a?",
    "a*",
    "a[b",
    "a\\b",
    "fix.",
]


@pytest.mark.parametrize("ref", BAD_REFS)
def test_spawn_branch_rejects_unsafe_refs(ref: str) -> None:
    with pytest.raises(ManifestError, match="branch"):
        parse_manifest("a.json", _spawn(branch=ref), {})


@pytest.mark.parametrize("ref", BAD_REFS)
def test_spawn_base_rejects_unsafe_refs(ref: str) -> None:
    with pytest.raises(ManifestError, match="base"):
        parse_manifest("a.json", _spawn(kind="new", base=ref), {})


@pytest.mark.parametrize("ref", ["fix/1", "feat/a-b_c.d", "main", "release/1.2", "x@y"])
def test_spawn_accepts_ordinary_refs(ref: str) -> None:
    m = parse_manifest("a.json", _spawn(kind="new", branch=ref, base=ref), {})
    assert isinstance(m, SpawnManifest)
    assert (m.branch, m.base) == (ref, ref)


def test_inbox_status_accepts_failed() -> None:
    raw = {
        "version": 1,
        "id": "h-0123abcd",
        "type": "status",
        "ref": "fix-1",
        "state": "failed",
        "detail": "boot failed",
    }
    s = InboxStatus.model_validate(raw)
    assert s.state == "failed"
    assert s.model_dump() == {**raw, "version": 1}


def test_spawn_manifest_accepts_runner() -> None:
    m = parse_manifest("a.json", _spawn(runner={"agent": "claude", "model": "opus"}), {})
    assert isinstance(m, SpawnManifest)
    assert m.runner == Runner(agent="claude", model="opus")


@pytest.mark.parametrize(
    "bad",
    [
        {"agent": "claude", "effort": "high"},
        {"agent": "claude", "model": "gpt-5"},
        {"agent": "codex"},
    ],
)
def test_spawn_manifest_rejects_unknown_runner_key_and_tier(bad: dict[str, str]) -> None:
    with pytest.raises(ManifestError):
        parse_manifest("a.json", _spawn(runner=bad), {})


def test_restart_distinguishes_absent_and_null_runner() -> None:
    base = {"version": 1, "id": "r", "type": "restart", "child": "c", "prompt": "again"}
    absent = parse_manifest("r.json", json.dumps(base), {})
    cleared = parse_manifest("r.json", json.dumps({**base, "runner": None}), {})
    assert isinstance(absent, RestartManifest) and isinstance(cleared, RestartManifest)
    assert not restart_sets_runner(absent)
    assert restart_sets_runner(cleared) and cleared.runner is None
