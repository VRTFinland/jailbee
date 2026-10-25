"""End to end through the real broker, handlers, queue, outbox and inbox code.

Only `Incus` (a small in-memory fake), `start_create`, `live_create_jobs`
and config loading are replaced; everything between an outbox file and an
inbox file is the production code path.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, broker, labels
from jailbee.orchestration.grant import Grant
from jailbee.orchestration.journal import JOURNAL_FILE, Journal
from jailbee.orchestration.outbox import INBOX_DIR, OUTBOX_DIR
from jailbee.orchestration.state import load_queue, pending_dir
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
HEAD = "c" * 40


class FakeIncus:
    """Containers, their labels, outboxes and inbox writes, in memory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.containers: dict[str, dict[str, Any]] = {}
        self.outbox: dict[str, dict[str, str]] = {}
        self.events: list[tuple[Any, ...]] = []

    def add(self, name: str, *, running: bool, **config: str) -> None:
        self.containers[name] = {
            "name": name,
            "status": "Running" if running else "Stopped",
            "config": {"volatile.uuid": f"uuid-{name}", **config},
        }
        self.outbox.setdefault(name, {})

    def set_running(self, name: str, running: bool) -> None:
        self.containers[name]["status"] = "Running" if running else "Stopped"

    # -- the Incus surface the broker uses ---------------------------------

    def list_containers(self) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(c)) for c in self.containers.values()]

    def exists(self, name: str) -> bool:
        return name in self.containers

    def config_get(self, name: str, key: str) -> str | None:
        value = self.containers[name]["config"].get(key)
        return str(value) if value else None

    def config_set(self, name: str, key: str, value: str) -> None:
        self.containers[name]["config"][key] = value

    def config_unset_checked(self, name: str, key: str) -> None:
        self.containers[name]["config"].pop(key, None)

    def stop(self, name: str, force: bool = False, timeout: int | None = None) -> None:
        self.events.append(("stop", name))
        self.set_running(name, False)

    def exec_capped(self, name: str, argv: list[str], **kw: object) -> str:
        assert argv[4] == OUTBOX_DIR
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for file_name, text in sorted(self.outbox[name].items()):
                data = text.encode()
                info = tarfile.TarInfo(f"./{file_name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return base64.b64encode(buf.getvalue()).decode()

    def exec(self, name: str, argv: list[str], **kw: object) -> str:
        if argv[:3] == ["rm", "-f", "--"]:
            journal = (self.root / JOURNAL_FILE).read_text()
            self.events.append(("rm", name, tuple(argv[3:]), journal.count("\n")))
            for file_name in argv[3:]:
                self.outbox[name].pop(file_name, None)
            return ""
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return HEAD + "\n"
        raise AssertionError(f"unexpected exec {argv}")

    def exec_with_input(self, name: str, argv: list[str], text: str, **kw: object) -> str:
        directory, file_name = argv[-3], argv[-2]
        self.events.append(("write", name, directory, file_name, json.loads(text)))
        return ""

    # -- views ---------------------------------------------------------------

    def inbox(self, name: str) -> list[tuple[str, dict[str, Any]]]:
        return [
            (e[3], e[4])
            for e in self.events
            if e[0] == "write" and e[1] == name and e[2] == INBOX_DIR
        ]


def _manifest(incus: FakeIncus, full: str, data: dict[str, Any]) -> None:
    incus.outbox[full][f"{data['id']}.json"] = json.dumps(data)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "state"
    root.mkdir()
    cfg = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
    incus = FakeIncus(root)
    grant = Grant.model_validate({"max_running_children": 1})
    incus.add("proj-orch", running=True, **{labels.GRANT: grant.to_label()})
    incus.add(
        "proj-c1",
        running=True,
        **{labels.SPAWNED_BY: "proj-orch", labels.LAST_IMPORT: HEAD},
    )
    start_create = MagicMock()
    monkeypatch.setattr(actions, "start_create", start_create)
    monkeypatch.setattr(actions, "live_create_jobs", lambda prefix: set())
    monkeypatch.setattr(broker, "repo_configs", lambda memory=None: {"proj": cfg})
    monkeypatch.setattr(broker, "load_global_config", lambda path: (GlobalConfig(), []))
    return {"root": root, "cfg": cfg, "incus": incus, "start_create": start_create}


def test_spawn_queues_then_starts_after_a_stop(world: dict[str, Any]) -> None:
    root: Path = world["root"]
    incus: FakeIncus = world["incus"]
    journal = Journal(root)
    memory = broker.BrokerMemory()
    _manifest(
        incus,
        "proj-orch",
        {
            "version": 1,
            "id": "a-spawn",
            "type": "spawn",
            "kind": "new",
            "branch": "fix/1",
            "prompt": "fix it",
            "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
        },
    )
    _manifest(incus, "proj-orch", {"version": 1, "id": "b-stop", "type": "stop", "child": "c1"})

    # Round 1: the only slot is taken by c1 -> the spawn queues; c1 stops.
    assert broker.tick(incus, root=root, journal=journal, now=T0, memory=memory)  # type: ignore[arg-type]  # fake
    world["start_create"].assert_not_called()
    assert [r.manifest_id for r in load_queue(root)["proj-orch"]] == ["a-spawn"]
    assert incus.containers["proj-c1"]["status"] == "Stopped"
    assert incus.containers["proj-c1"]["config"][labels.STOPPED_HEAD] == HEAD
    assert incus.outbox["proj-orch"] == {}

    # Round 2, orchestrator stopped: the freed slot starts the queued spawn,
    # and its status is parked.
    incus.set_running("proj-orch", False)
    broker.tick(incus, root=root, journal=journal, now=T0 + timedelta(seconds=5), memory=memory)  # type: ignore[arg-type]  # fake
    world["start_create"].assert_called_once()
    opts = world["start_create"].call_args.args[3]
    assert (opts.name, opts.network, opts.spawned_by) == ("proj-fix-1", "strict", "proj-orch")
    assert load_queue(root) == {}
    parked = sorted(p.name for p in pending_dir(root, "proj-orch").iterdir())
    assert len(parked) == 1

    # Round 3, orchestrator back: the parked status is flushed after the others.
    incus.set_running("proj-orch", True)
    broker.tick(incus, root=root, journal=journal, now=T0 + timedelta(seconds=10), memory=memory)  # type: ignore[arg-type]  # fake
    inbox = incus.inbox("proj-orch")
    assert [(m["ref"], m["state"]) for _, m in inbox] == [
        ("a-spawn", "queued"),
        ("b-stop", "done"),
        ("a-spawn", "started"),
    ]
    assert inbox[2][1]["detail"] == "creation of child `fix-1` started"
    assert inbox[2][0] == parked[0]
    assert [name for name, _ in inbox] == sorted(name for name, _ in inbox)
    assert not any(pending_dir(root, "proj-orch").iterdir())

    # Journal before reply, reply before delete, per manifest.
    events = [e for e in incus.events if e[0] in ("rm", "write") and e[1] == "proj-orch"]
    kinds = [
        (e[0], e[2] if e[0] == "rm" else e[4].get("ref"))
        for e in events
        if e[0] == "rm" or e[2] == INBOX_DIR
    ]
    assert kinds[:4] == [
        ("write", "a-spawn"),
        ("rm", ("a-spawn.json",)),
        ("write", "b-stop"),
        ("rm", ("b-stop.json",)),
    ]
    rms = [e for e in events if e[0] == "rm"]
    assert [e[3] for e in rms] == [1, 2]  # each delete saw its journal line on disk
    lines = (root / JOURNAL_FILE).read_text().splitlines()
    assert [json.loads(line)["manifest_id"] for line in lines] == ["a-spawn", "b-stop"]
    assert {json.loads(line)["container_key"] for line in lines} == {"uuid-proj-orch"}
