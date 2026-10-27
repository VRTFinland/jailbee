from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee.incus import IncusError
from jailbee.orchestration import outbox
from jailbee.orchestration.protocol import InboxStatus, inbox_file_name
from jailbee.orchestration.state import pending_dir

if TYPE_CHECKING:
    from pathlib import Path

AT = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
FULL = "proj-child"


def _item(n: str) -> InboxStatus:
    return InboxStatus(version=1, id=f"h-{n}", type="status", ref="r", state="queued", detail="d")


def _incus() -> MagicMock:
    return MagicMock()


def _finish_item():
    from jailbee.orchestration.protocol import PendingFinish

    return PendingFinish(
        version=1,
        type="finish",
        id="f-1",
        child_full="proj-c",
        child_short="c",
        branch="b",
        done_sha="a" * 40,
        body="done",
        in_reply_to=None,
        uid=1000,
    )


def _finished_message():
    from jailbee.orchestration.protocol import InboxMessage

    return InboxMessage(version=1, id="h-f", type="message", from_="c", body="done")


def test_flush_runs_finish_callback_in_order(tmp_path: Path) -> None:
    from jailbee.orchestration.protocol import finish_file_name

    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / inbox_file_name(_item("1"), AT, seq=0)).write_text(_item("1").model_dump_json())
    path = directory / finish_file_name(_finish_item(), AT, seq=1)
    path.write_text(_finish_item().model_dump_json())
    (directory / inbox_file_name(_item("3"), AT, seq=2)).write_text(_item("3").model_dump_json())
    incus = _incus()
    finish = MagicMock(return_value=_finished_message())
    assert outbox.flush_pending(incus, tmp_path, FULL, uid=1000, finish=finish) == 3
    writes = incus.exec_with_input.call_args_list
    assert [json.loads(c.args[2])["id"] for c in writes] == ["h-1", "h-f", "h-3"]
    assert writes[1].args[1][-2] == path.name.replace(".finish.json", ".json")
    finish.assert_called_once_with(_finish_item())


def test_flush_stops_at_finish_item_without_callback(tmp_path: Path) -> None:
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    path = directory / "a.finish.json"
    path.write_text(_finish_item().model_dump_json())
    incus = _incus()
    assert outbox.flush_pending(incus, tmp_path, FULL, uid=1000) == 0
    assert path.exists()
    incus.exec_with_input.assert_not_called()


def test_flush_keeps_finish_item_when_write_fails(tmp_path: Path) -> None:
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    path = directory / "a.finish.json"
    path.write_text(_finish_item().model_dump_json())
    incus = _incus()
    incus.exec_with_input.side_effect = IncusError("offline")
    finish = MagicMock(return_value=_finished_message())
    assert outbox.flush_pending(incus, tmp_path, FULL, uid=1000, finish=finish) == 0
    assert path.exists()
    incus.exec_with_input.side_effect = None
    assert outbox.flush_pending(incus, tmp_path, FULL, uid=1000, finish=finish) == 1
    assert finish.call_count == 2


def test_flush_quarantines_invalid_finish_and_continues(tmp_path: Path) -> None:
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    path = directory / "a.finish.json"
    path.write_text("invalid")
    (directory / "b.json").write_text(_item("1").model_dump_json())
    assert outbox.flush_pending(_incus(), tmp_path, FULL, uid=1000, finish=MagicMock()) == 1
    assert path.with_name(path.name + ".bad").exists()


def test_helper_script_is_packaged_file() -> None:
    assert outbox.helper_script().startswith("#!/usr/bin/env bash")


def test_ensure_env_makes_dirs_as_user_then_installs_as_root() -> None:
    incus = _incus()
    outbox.ensure_orchestration_env(incus, FULL, uid=1000)
    incus.exec.assert_called_once_with(
        FULL, ["mkdir", "-p", outbox.INBOX_DIR, outbox.OUTBOX_DIR], uid=1000
    )
    args, kwargs = incus.exec_with_input.call_args
    assert args[0] == FULL
    assert args[1][:2] == ["sh", "-c"] and args[1][3:] == ["sh", outbox.INBOX_HELPER]
    assert args[2] == outbox.helper_script()
    assert kwargs.get("uid") is None
    assert [c[0] for c in incus.mock_calls] == ["exec", "exec_with_input"]


def test_ensure_env_script_replaces_only_when_different(tmp_path: Path) -> None:
    target = tmp_path / "bin" / "jailbee-inbox"
    target.parent.mkdir()

    def install(text: str) -> None:
        subprocess.run(
            ["sh", "-c", outbox._INSTALL_HELPER, "sh", str(target)],
            input=text,
            text=True,
            check=True,
        )

    install("v1\n")
    assert target.read_text() == "v1\n"
    assert target.stat().st_mode & 0o777 == 0o755
    os.utime(target, (1, 1))
    install("v1\n")
    assert target.stat().st_mtime == 1
    install("v2\n")
    assert target.read_text() == "v2\n"
    assert not list(target.parent.glob(".jailbee-inbox.*"))


def test_read_outbox_caps_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {f"m{i:02d}.json": "{}" for i in range(40)}
    files["p.md"] = "body"
    seen: dict[str, object] = {}

    def fake(incus: object, container: str, directory: str, **kw: object) -> dict[str, str]:
        seen.update(container=container, directory=directory, **kw)
        return files

    monkeypatch.setattr(outbox, "read_text_outbox", fake)
    out = outbox.read_outbox(_incus(), FULL, uid=1000)
    assert [m.file_name for m in out] == [f"m{i:02d}.json" for i in range(32)]
    assert "p.md" in out[0].files
    assert seen["directory"] == outbox.OUTBOX_DIR
    assert seen["max_file_bytes"] == outbox.MAX_FILE_BYTES
    assert seen["uid"] == 1000
    assert seen["select"] == (("*.json", 32), ("*.md", 64))


def test_read_outbox_selects_manifests_and_bodies_separately() -> None:
    """40 manifests plus 40 bodies sorting after every manifest: all 32 read
    manifests still arrive with their bodies (one name-ordered cap of 64 would
    have cut m25..m32's bodies)."""
    incus = MagicMock()
    incus.exec_capped.return_value = ""
    outbox.read_outbox(incus, FULL, uid=1000)
    argv = incus.exec_capped.call_args.args[1]
    assert argv[-4:] == ["*.json", "32", "*.md", "64"]
    per_file = outbox.MAX_FILE_BYTES + 2048
    assert argv[-5] == str((32 + 64) * per_file + 10240)  # no legit round is truncated


def test_first_32_manifests_parse_with_their_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.orchestration.protocol import MessageManifest, parse_manifest

    everything: dict[str, str] = {}
    for i in range(1, 41):
        everything[f"m{i:02d}.json"] = json.dumps(
            {"version": 1, "id": f"m{i:02d}", "type": "message", "body_file": f"p{i:02d}.md"}
        )
        everything[f"p{i:02d}.md"] = f"body {i}"
    # What the container-side selection yields: first 32 json + first 64 md.
    jsons = sorted(n for n in everything if n.endswith(".json"))[:32]
    mds = sorted(n for n in everything if n.endswith(".md"))[:64]
    selected = {n: everything[n] for n in [*jsons, *mds]}
    monkeypatch.setattr(outbox, "read_text_outbox", lambda *a, **k: selected)
    raws = outbox.read_outbox(MagicMock(), FULL, uid=1000)
    assert [r.file_name for r in raws] == jsons
    for i, raw in enumerate(raws, start=1):
        m = parse_manifest(raw.file_name, raw.text, raw.files)
        assert isinstance(m, MessageManifest)
        assert m.body == f"body {i}"


def test_read_outbox_ignores_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "read_text_outbox", lambda *a, **k: {"x.md": "y"})
    assert outbox.read_outbox(_incus(), FULL, uid=None) == []


def test_write_inbox_is_atomic_rename() -> None:
    incus = _incus()
    item = _item("1")
    outbox.write_inbox(incus, FULL, item, uid=1000, now=AT)
    args, kwargs = incus.exec_with_input.call_args
    argv = args[1]
    name = inbox_file_name(item, AT)
    assert args[0] == FULL
    assert argv[:2] == ["sh", "-c"]
    assert "mv" in argv[2]
    assert argv[3:] == ["sh", outbox.INBOX_DIR, name, "0644"]
    assert ".$2.tmp" in argv[2]
    assert '"type":"status"' in args[2].replace(" ", "")
    assert kwargs["uid"] == 1000


def test_deliver_parks_when_not_running(tmp_path: Path) -> None:
    incus = _incus()
    item = _item("1")
    outbox.deliver(incus, tmp_path, FULL, item, running=False, uid=None, now=AT)
    incus.exec_with_input.assert_not_called()
    incus.exec.assert_not_called()
    assert (pending_dir(tmp_path, FULL) / inbox_file_name(item, AT)).is_file()


def test_deliver_writes_when_running(tmp_path: Path) -> None:
    incus = _incus()
    outbox.deliver(incus, tmp_path, FULL, _item("1"), running=True, uid=None, now=AT)
    incus.exec_with_input.assert_called_once()
    assert not pending_dir(tmp_path, FULL).exists()


def test_deliver_parks_on_incus_error(tmp_path: Path) -> None:
    incus = _incus()
    incus.exec_with_input.side_effect = IncusError("boom")
    item = _item("1")
    outbox.deliver(incus, tmp_path, FULL, item, running=True, uid=None, now=AT)
    assert (pending_dir(tmp_path, FULL) / inbox_file_name(item, AT)).is_file()


def test_park_then_flush_delivers_in_name_order(tmp_path: Path) -> None:
    incus = _incus()
    later = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
    i1, i2 = _item("a"), _item("b")
    outbox.deliver(incus, tmp_path, FULL, i2, running=False, uid=None, now=later)
    outbox.deliver(incus, tmp_path, FULL, i1, running=False, uid=None, now=AT)
    assert outbox.flush_pending(incus, tmp_path, FULL, uid=None) == 2
    names = [c.args[1][5] for c in incus.exec_with_input.call_args_list]
    assert names == [inbox_file_name(i1, AT), inbox_file_name(i2, later)]
    assert list(pending_dir(tmp_path, FULL).iterdir()) == []


def test_flush_stops_at_first_failure(tmp_path: Path) -> None:
    incus = _incus()
    later = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
    outbox.deliver(incus, tmp_path, FULL, _item("a"), running=False, uid=None, now=AT)
    outbox.deliver(incus, tmp_path, FULL, _item("b"), running=False, uid=None, now=later)
    incus.exec_with_input.side_effect = [None, IncusError("x")]
    assert outbox.flush_pending(incus, tmp_path, FULL, uid=None) == 1
    left = [p.name for p in pending_dir(tmp_path, FULL).iterdir()]
    assert left == [inbox_file_name(_item("b"), later)]


def test_flush_without_pending_dir(tmp_path: Path) -> None:
    assert outbox.flush_pending(_incus(), tmp_path, FULL, uid=None) == 0


def test_delete_runs_one_rm_in_outbox_cwd() -> None:
    incus = _incus()
    outbox.delete_outbox_files(incus, FULL, ["a.json", "-b.md"], uid=1000)
    incus.exec.assert_called_once_with(
        FULL, ["rm", "-f", "--", "a.json", "-b.md"], uid=1000, cwd=outbox.OUTBOX_DIR
    )


def test_delete_refuses_path_names() -> None:
    incus = _incus()
    with pytest.raises(ValueError):
        outbox.delete_outbox_files(incus, FULL, ["ok.json", "../x.json"], uid=None)
    incus.exec.assert_not_called()


def test_write_grant_json_is_read_only_atomic() -> None:
    incus = _incus()
    outbox.write_grant_json(incus, FULL, '{"version": 1}', uid=1000)
    args, _ = incus.exec_with_input.call_args
    assert args[1][3:] == ["sh", outbox.GRANT_FILE.rsplit("/", 1)[0], "grant.json", "0444"]
    assert args[2] == '{"version": 1}'
    assert outbox.GRANT_FILE.endswith("/.jailbee/orchestration/grant.json")


def test_flush_pending_follows_emission_order_within_a_second(tmp_path: Path) -> None:
    incus = _incus()
    first, second = _item("ffff"), _item("0000")
    outbox.deliver(incus, tmp_path, FULL, first, running=False, uid=None, now=AT, seq=7)
    outbox.deliver(incus, tmp_path, FULL, second, running=False, uid=None, now=AT, seq=8)
    outbox.flush_pending(incus, tmp_path, FULL, uid=None)
    written = [c.args[1][-2] for c in incus.exec_with_input.call_args_list]
    assert written == [inbox_file_name(first, AT, seq=7), inbox_file_name(second, AT, seq=8)]


def test_write_inbox_uses_the_sequence(tmp_path: Path) -> None:
    incus = _incus()
    item = _item("1")
    outbox.deliver(incus, tmp_path, FULL, item, running=True, uid=None, now=AT, seq=3)
    assert incus.exec_with_input.call_args.args[1][-2] == inbox_file_name(item, AT, seq=3)


def test_read_outbox_skips_dot_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.name.tmp`-style temp files are half-written; never manifests."""
    files = {".m1.json": "{}", "m2.json": "{}", ".p.md": "x"}
    monkeypatch.setattr(outbox, "read_text_outbox", lambda *a, **k: files)
    out = outbox.read_outbox(_incus(), FULL, uid=None)
    assert [m.file_name for m in out] == ["m2.json"]
    assert ".p.md" not in out[0].files


def test_bounded_selection_excludes_dot_files_in_the_container() -> None:
    """Temp files must not use up the per-round selection count either."""
    incus = MagicMock()
    incus.exec_capped.return_value = ""
    outbox.read_outbox(incus, FULL, uid=1000)
    script = incus.exec_capped.call_args.args[1][2]
    assert "! -name '.*'" in script


def test_deliver_returns_delivered_when_written(tmp_path: Path) -> None:
    result = outbox.deliver(_incus(), tmp_path, FULL, _item("1"), running=True, uid=None, now=AT)
    assert result == "delivered"


def test_deliver_returns_parked_when_not_running(tmp_path: Path) -> None:
    result = outbox.deliver(_incus(), tmp_path, FULL, _item("1"), running=False, uid=None, now=AT)
    assert result == "parked"


def test_deliver_returns_parked_on_incus_error(tmp_path: Path) -> None:
    incus = _incus()
    incus.exec_with_input.side_effect = IncusError("boom")
    result = outbox.deliver(incus, tmp_path, FULL, _item("1"), running=True, uid=None, now=AT)
    assert result == "parked"


def test_deliver_parks_behind_existing_pending(tmp_path: Path) -> None:
    incus = _incus()
    outbox.deliver(incus, tmp_path, FULL, _item("a"), running=False, uid=None, now=AT)
    later = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
    item = _item("b")
    result = outbox.deliver(incus, tmp_path, FULL, item, running=True, uid=None, now=later)
    assert result == "parked"
    incus.exec_with_input.assert_not_called()
    assert (pending_dir(tmp_path, FULL) / inbox_file_name(item, later)).is_file()


def test_deliver_ignores_dot_files_in_pending(tmp_path: Path) -> None:
    incus = _incus()
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / ".x.tmp").write_text("{}")
    result = outbox.deliver(incus, tmp_path, FULL, _item("1"), running=True, uid=None, now=AT)
    assert result == "delivered"


def test_has_pending_ignores_quarantined_bad_files(tmp_path: Path) -> None:
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.finish.json.bad").write_text("invalid")
    assert outbox.has_pending(tmp_path, FULL) is False
    (directory / "b.json").write_text("{}")
    assert outbox.has_pending(tmp_path, FULL) is True


def test_flush_quarantine_notes_the_recipient(tmp_path: Path) -> None:
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.finish.json").write_text("invalid")
    incus = _incus()
    assert outbox.flush_pending(incus, tmp_path, FULL, uid=1000, finish=MagicMock()) == 0
    notes = [json.loads(c.args[2]) for c in incus.exec_with_input.call_args_list]
    assert len(notes) == 1
    assert notes[0]["kind"] == "note"
    assert "a.finish.json.bad" in notes[0]["body"]
    assert "could not be read" in notes[0]["body"]


def test_flush_quarantine_note_is_parked_when_the_write_fails(tmp_path: Path) -> None:
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.finish.json").write_text("invalid")
    incus = _incus()
    incus.exec_with_input.side_effect = IncusError("offline")
    outbox.flush_pending(incus, tmp_path, FULL, uid=1000, finish=MagicMock())
    parked = [p for p in directory.iterdir() if p.name.endswith(".json")]
    assert len(parked) == 1
    assert json.loads(parked[0].read_text())["kind"] == "note"
