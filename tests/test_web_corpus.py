"""The recorded transcript corpus: its normalisation and the committed files.

`scripts/web_corpus.py` records genuine Claude Code sessions on a toy
repository and normalises them before they are committed. These tests pin
that nothing identifying survives: not the recording host's paths, ids or
account, and no credential.
"""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

from jailbee.transcripts.claude import ClaudeSource
from scripts.web_corpus import (
    CORPUS_DIR,
    MANIFEST,
    SHAPES,
    TOY_PROJECT,
    Identity,
    detect_shapes,
    find_leaks,
    normalise_home,
    scan_corpus,
)

RAW_SID = "3f2a9c1e-8b7d-4e6f-9a1b-2c3d4e5f6a7b"


def _identity(tmp_path: Path) -> Identity:
    return Identity(
        toy_dir=str(tmp_path / "rec-x1y2" / "toy"),
        home="/home/alice",
        secrets=("alice@corp.example", "build-host-17"),
    )


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _user(identity: Identity, text: str, **extra: object) -> dict[str, object]:
    return {
        "type": "user",
        "uuid": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
        "parentUuid": None,
        "sessionId": RAW_SID,
        "cwd": identity.toy_dir,
        "timestamp": "2026-11-23T13:33:00.000Z",
        "message": {"role": "user", "content": text},
        **extra,
    }


def _normalise(tmp_path: Path, records: list[dict[str, object]]) -> tuple[Identity, Path]:
    identity = _identity(tmp_path)
    raw = tmp_path / "raw"
    _write(raw / f"{RAW_SID}.jsonl", records)
    out = tmp_path / "out"
    normalise_home(raw, out, identity, [RAW_SID])
    return identity, out / "projects" / TOY_PROJECT


def test_sessions_get_stable_ids_in_recording_order(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    other = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    raw = tmp_path / "raw"
    _write(raw / f"{RAW_SID}.jsonl", [_user(identity, "first")])
    _write(raw / f"{other}.jsonl", [_user(identity, "second", sessionId=other)])
    out = tmp_path / "out"

    normalise_home(raw, out, identity, [other, RAW_SID])

    project = out / "projects" / TOY_PROJECT
    names = sorted(p.name for p in project.glob("*.jsonl"))
    assert names == [
        "00000001-0000-4000-8000-000000000001.jsonl",
        "00000002-0000-4000-8000-000000000002.jsonl",
    ]
    second = _read(project / "00000001-0000-4000-8000-000000000001.jsonl")
    assert second[0]["sessionId"] == "00000001-0000-4000-8000-000000000001"
    assert second[0]["message"] == {"role": "user", "content": "second"}


def test_paths_point_into_the_toy_repository_only(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    parent = str(Path(identity.toy_dir).parent)
    text = (
        f"edited {identity.toy_dir}/calc.py; scratch in {parent}/other;"
        f" config at {identity.home}/.claude/settings.json"
    )
    _, project = _normalise(tmp_path, [_user(identity, text)])

    [record] = _read(next(project.glob("*.jsonl")))
    assert record["cwd"] == "/work/toy"
    assert record["message"] == {
        "role": "user",
        "content": "edited /work/toy/calc.py; scratch in /work/other;"
        " config at /home/user/.claude/settings.json",
    }


def test_only_transcript_records_and_fields_survive(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    assistant = {
        "type": "assistant",
        "uuid": "11111111-2222-4333-8444-555555555555",
        "sessionId": RAW_SID,
        "userType": "external",
        "entrypoint": "sdk-cli",
        "requestId": "req_011ChKwk",
        "message": {
            "id": "msg_011ChKwkYYS5",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "usage": {"input_tokens": 2, "service_tier": "standard"},
            "content": [{"type": "thinking", "thinking": "", "signature": "CAQS8AYKEAgUGAI4"}],
        },
    }
    records = [
        {"type": "session-owner", "accountUuid": "a6445300-300b-4e42-acbd-28578a5f5265"},
        {"type": "api-request-blob", "message": {"role": "system", "content": "prompt"}},
        _user(identity, "hello"),
        {
            "type": "attachment",
            "attachment": {"type": "session_context", "context": {"userEmail": "x"}},
        },
        {"type": "attachment", "attachment": {"type": "date", "date": "2026-11-23"}},
        assistant,
        {"type": "system", "subtype": "stop_hook_summary", "hookInfos": [{"command": "x"}]},
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"},
    ]

    _, project = _normalise(tmp_path, records)

    kept = _read(next(project.glob("*.jsonl")))
    assert [(r["type"], r.get("subtype")) for r in kept] == [
        ("user", None),
        ("attachment", None),
        ("assistant", None),
        ("system", "compact_boundary"),
    ]
    assert kept[2] == {
        "type": "assistant",
        "uuid": "00000003-0000-4000-8000-000000000003",
        "sessionId": "00000001-0000-4000-8000-000000000001",
        "message": {
            "id": "msg_000001",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "thinking", "thinking": "", "signature": "signature"}],
        },
    }


def test_names_and_credentials_are_redacted(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    text = (
        "host build-host-17, mail alice@corp.example or bob@elsewhere.example,"
        " keys sk-ant-oat01-AbCdEf123456_789xyz ghp_1234567890abcdefghijABCDEFGHIJ123456"
        " github_pat_11ABCDEFG0123456789_abcdefghijklmnop gho_abcdefghijklmnopqrstuvwxyz0123456789"
        " header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    )
    _, project = _normalise(tmp_path, [_user(identity, text)])

    [record] = _read(next(project.glob("*.jsonl")))
    assert record["message"] == {
        "role": "user",
        "content": "host REDACTED, mail REDACTED or REDACTED,"
        " keys REDACTED REDACTED"
        " REDACTED REDACTED"
        " header Authorization: Bearer REDACTED",
    }


def test_subagents_follow_their_session_with_stable_agent_ids(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    raw = tmp_path / "raw"
    agent = "a00d6719ec73a3a88"
    call = _user(
        identity,
        "",
        toolUseResult={"status": "completed", "agentId": agent, "agentType": "general-purpose"},
    )
    _write(raw / f"{RAW_SID}.jsonl", [call])
    subagents = raw / RAW_SID / "subagents"
    _write(subagents / f"agent-{agent}.jsonl", [_user(identity, "task", agentId=agent)])
    (subagents / f"agent-{agent}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "Count lines",
                "toolUseId": "toolu_011JE7JvHsPnytR9zHCwTooH",
                "requestShape": "foreground",
                "pickedEffortModel": "claude-haiku-5-5",
            }
        )
    )
    out = tmp_path / "out"

    normalise_home(raw, out, identity, [RAW_SID])

    session = out / "projects" / TOY_PROJECT / "00000001-0000-4000-8000-000000000001"
    new_agent = "a0000000000000001"
    [main] = _read(session.with_suffix(".jsonl"))
    assert main["toolUseResult"] == {
        "status": "completed",
        "agentId": new_agent,
        "agentType": "general-purpose",
    }
    [sub] = _read(session / "subagents" / f"agent-{new_agent}.jsonl")
    assert sub["agentId"] == new_agent
    meta = json.loads((session / "subagents" / f"agent-{new_agent}.meta.json").read_text())
    assert meta == {
        "agentType": "general-purpose",
        "description": "Count lines",
        "toolUseId": "toolu_000001",
    }


CLEAN = (
    "cwd /work/toy, settings /home/user/.claude, session 00000002-0000-4000-8000-000000000002,"
    " tool toolu_000003, Co-Authored-By: Claude <noreply@anthropic.com>, see /usr/bin/python3"
)


def test_find_leaks_passes_normalised_text(tmp_path: Path) -> None:
    assert find_leaks(CLEAN, _identity(tmp_path)) == []
    assert find_leaks(CLEAN) == []


@pytest.mark.parametrize(
    "leak",
    [
        "{toy}/calc.py",
        "/home/alice/notes",
        "/home/bob/notes",
        "/tmp/rec-x1y2",
        "/root/.claude",
        "build-host-17",
        "alice@corp.example",
        "carol@elsewhere.example",
        "sk-ant-oat01-AbCdEf123456_789xyz",
        "ghp_1234567890abcdefghijABCDEFGHIJ123456",
        "a6445300-300b-4e42-acbd-28578a5f5265",
    ],
)
def test_find_leaks_reports_each_identifying_string(tmp_path: Path, leak: str) -> None:
    identity = _identity(tmp_path)
    leak = leak.format(toy=identity.toy_dir)

    assert find_leaks(f"{CLEAN} {leak} end", identity) != []


# --- The committed corpus ----------------------------------------------------


def _manifest() -> dict[str, Any]:
    data = json.loads((CORPUS_DIR / MANIFEST).read_text())
    assert isinstance(data, dict)
    return data


def test_committed_corpus_identifies_no_one() -> None:
    assert scan_corpus(CORPUS_DIR) == {}
    host = socket.gethostname()
    if len(host) >= 5:
        assert not any(host in path.read_text() for path in CORPUS_DIR.rglob("*") if path.is_file())


def test_a_leak_injected_into_the_corpus_is_caught(tmp_path: Path) -> None:
    copy = tmp_path / "corpus"
    shutil.copytree(CORPUS_DIR, copy)
    session = sorted((copy / "projects" / TOY_PROJECT).glob("*.jsonl"))[0]
    lines = session.read_text().splitlines()
    record = json.loads(lines[-1])
    record["cwd"] = "/home/maintainer/private-repo"
    session.write_text("\n".join([*lines[:-1], json.dumps(record)]) + "\n")

    assert list(scan_corpus(copy)) == [str(session.relative_to(copy))]


def test_manifest_matches_the_recorded_sessions() -> None:
    manifest = _manifest()
    project = CORPUS_DIR / "projects" / TOY_PROJECT
    sessions = manifest["sessions"]
    assert sorted(p.stem for p in project.glob("*.jsonl")) == sorted(
        s["session_id"] for s in sessions
    )
    covered: set[str] = set()
    for session in sessions:
        shapes = detect_shapes(project / f"{session['session_id']}.jsonl")
        assert session["shapes"] == shapes, session["name"]
        covered.update(shapes)
    assert sorted(covered) == sorted(set(SHAPES) - set(manifest["missing_shapes"]))


def test_the_reader_lists_and_reads_every_recorded_session() -> None:
    source = ClaudeSource()
    listed = {info.session_id: info for info in source.list_sessions(CORPUS_DIR)}
    for session in _manifest()["sessions"]:
        info = listed[session["session_id"]]
        assert info.title is not None and info.title.startswith("CORPUS-")
        files = source.locate(CORPUS_DIR, session["session_id"])
        assert files is not None
        for path in [files.transcript, *files.subagents.values()]:
            assert source.read(path).entries, path


def test_claude_scratch_paths_name_the_toy_project_only(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    root = str(Path(identity.toy_dir).parent)
    identity = Identity(
        toy_dir=identity.toy_dir,
        home=identity.home,
        renames=((f"{root}/tmp/claude-53023", "/work/claude-tmp"),),
    )
    encoded = "-" + identity.toy_dir.strip("/").replace("/", "-").replace("_", "-")
    output = f"{root}/tmp/claude-53023/{encoded}/{RAW_SID}/tasks/b1.output"
    raw = tmp_path / "raw"
    _write(raw / f"{RAW_SID}.jsonl", [_user(identity, f"written to {output}")])
    normalise_home(raw, tmp_path / "out", identity, [RAW_SID])

    [record] = _read(next((tmp_path / "out" / "projects" / TOY_PROJECT).glob("*.jsonl")))
    assert record["message"] == {
        "role": "user",
        "content": "written to /work/claude-tmp/-work-toy/"
        "00000001-0000-4000-8000-000000000001/tasks/b1.output",
    }
    assert find_leaks(output, identity) != []
