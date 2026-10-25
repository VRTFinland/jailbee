from __future__ import annotations

import json
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
