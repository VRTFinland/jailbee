"""Container storage preserves validation, atomic publication and local boundaries."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local

import pytest


@pytest.fixture(autouse=True)
def isolated_role(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


def pr_manifest(body_file="body.md"):
    return {
        "version": 1,
        "repo": "acme/repo",
        "pr": 1,
        "actions": [{"type": "comment", "body_file": body_file}],
    }


def store_for(home):
    from jailbee.container_outbox.store import Store

    return Store(home)


def test_concurrent_pr_publications_enforce_cap(tmp_path):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    manifest = {
        "version": 1,
        "repo": "acme/repo",
        "pr": 1,
        "actions": [{"type": "comment", "body": "x"}],
    }
    for _ in range(19):
        store.write_manifest("pr", "create", manifest, {})

    def publish(_):
        try:
            store.write_manifest("pr", "create", manifest, {})
            return True
        except OutboxCommandError as exc:
            assert "20" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(publish, range(2))) == 1
    assert len(list(store.outbox("pr").glob("*.json"))) == 20


def snapshot(path):
    return {p.name: p.read_text() for p in path.iterdir()}


def test_store_import_does_not_load_host_modules():
    # An isolated interpreter prevents earlier host tests from hiding an import leak.
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import jailbee.container_outbox.store; "
            "assert 'jailbee.incus' not in sys.modules; "
            "assert 'jailbee.git' not in sys.modules; assert 'jailbee.tui' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("marker", [False, True])
def test_child_refuses_before_creating_directories(tmp_path, marker):
    from jailbee.container_outbox.store import OutboxCommandError

    if marker:
        role = tmp_path / ".jailbee/orchestration/role"
        role.parent.mkdir(parents=True)
        role.write_text("child\n")
    store = store_for(tmp_path)
    with pytest.raises(OutboxCommandError, match="child container") as exc:
        store.require_writable({} if marker else {"JAILBEE_ROLE": "child"})
    assert exc.value.exit_code == 1
    assert "jailbee inbox send message --text" in str(exc.value)
    assert not store.outbox("pr").exists()
    assert not store.outbox("pr").exists()


@pytest.mark.parametrize("component", [".jailbee", ".jailbee/pr-outbox"])
@pytest.mark.parametrize("symlink", [False, True])
def test_writable_rejects_symlinks_and_non_directories(tmp_path, component, symlink):
    from jailbee.container_outbox.store import OutboxCommandError

    target = tmp_path / component
    target.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if symlink:
        target.symlink_to(elsewhere, target_is_directory=True)
    else:
        target.write_text("not a directory")
    with pytest.raises(OutboxCommandError):
        store_for(tmp_path).require_writable({})
    assert not list(elsewhere.iterdir())


def test_writable_directories_are_private(tmp_path):
    store = store_for(tmp_path)
    store.require_writable({})
    assert store.outbox("pr").stat().st_mode & 0o777 == 0o700


def test_pr_write_roundtrip_and_body_renaming(tmp_path):
    from jailbee.pr_manifest import parse_manifest

    store = store_for(tmp_path)
    manifest = pr_manifest()
    name = store.write_manifest("pr", "hello", manifest, {"body.md": "Text"})
    files = snapshot(store.outbox("pr"))
    assert name == "001-hello.json"
    raw = json.loads(files[name])
    body_name = raw["actions"][0]["body_file"]
    assert body_name.startswith("001-hello-") and body_name.endswith(".md")
    assert files[body_name] == "Text"
    assert parse_manifest(name, files[name], files).actions[0].body == "Text"
    assert manifest == pr_manifest()  # Never mutate a caller's draft.


def test_pr_write_roundtrips_host_parser_with_nested_bodies(tmp_path):
    from jailbee.pr_outbox import parse_manifest

    store = store_for(tmp_path)
    raw = {
        "version": 1,
        "repo": "o/r",
        "pr": 7,
        "actions": [
            {
                "type": "review",
                "body_file": "review.md",
                "comments": [{"path": "src/a.py", "line": 4, "body_file": "line.md"}],
            }
        ],
    }
    name = store.write_manifest("pr", "review", raw, {"review.md": "Review", "line.md": "Line"})
    files = snapshot(store.outbox("pr"))
    parsed = parse_manifest(name, files[name], files)
    assert parsed.actions[0].body == "Review"
    assert parsed.actions[0].comments[0].body == "Line"


def test_invalid_manifest_leaves_home_unchanged(tmp_path):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    bad = {"version": 1, "actions": [{"type": "edit", "repo": ".", "pr": 1, "title": "New"}]}
    with pytest.raises(OutboxCommandError, match="repo") as exc:
        store.write_manifest("pr", "bad", bad, {})
    assert exc.value.exit_code == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["../x", "/tmp/x", "a/b", "a\\b", "", ".", "..", "x\x00y"])
def test_unsafe_names_do_not_escape_store(tmp_path, name):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    with pytest.raises(OutboxCommandError) as exc:
        store.write_manifest("pr", name, pr_manifest(), {"body.md": "Text"})
    assert exc.value.exit_code == 2
    with pytest.raises(OutboxCommandError):
        store.show("pr", name)
    with pytest.raises(OutboxCommandError):
        store.drop("pr", name)


def test_next_name_is_exclusive(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    store.require_writable({})
    (store.outbox("pr") / "001-a.json").write_text("existing")
    original = os.open
    collided = False

    def collide(path, flags, *args, **kwargs):
        nonlocal collided
        if flags & os.O_EXCL and not collided:
            collided = True
            fd = original(path, flags, *args, **kwargs)
            os.close(fd)
            raise FileExistsError(path)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", collide)
    name = store.write_manifest("pr", "hello", pr_manifest(), {"body.md": "Text"})
    assert collided
    assert name == "003-hello.json"
    assert (store.outbox("pr") / "001-a.json").read_text() == "existing"
    assert json.loads((store.outbox("pr") / name).read_text())["version"] == 1


def test_two_writers_publish_distinct_complete_manifests(tmp_path):
    from jailbee.pr_manifest import parse_manifest

    store = store_for(tmp_path)
    store.require_writable({})
    barrier = Barrier(2)

    def write(body):
        barrier.wait(timeout=5)
        return store_for(tmp_path).write_manifest("pr", "same", pr_manifest(), {"body.md": body})

    with ThreadPoolExecutor(max_workers=2) as pool:
        names = list(pool.map(write, ["First", "Second"]))
    assert set(names) == {"001-same.json", "002-same.json"}
    files = snapshot(store.outbox("pr"))
    assert {parse_manifest(n, files[n], files).actions[0].body for n in names} == {
        "First",
        "Second",
    }
    assert not any(n.startswith(".") for n in files)


def test_publication_is_body_first_and_manifest_is_never_empty(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    original = os.replace
    published = []

    def observe(src, dst, *args, **kwargs):
        files = snapshot(store.outbox("pr"))
        assert not any(n.endswith(".json") and not text for n, text in files.items())
        if str(dst).endswith(".json"):
            assert any(n.endswith(".md") for n in files)
        result = original(src, dst, *args, **kwargs)
        published.append(str(dst))
        return result

    monkeypatch.setattr(os, "replace", observe)
    store.write_manifest("pr", "atomic", pr_manifest(), {"body.md": "Text"})
    assert published[-1] == "001-atomic.json"


def test_failed_publication_cleans_own_bodies_and_reservation(tmp_path, monkeypatch):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    original = os.replace

    def fail(src, dst, *args, **kwargs):
        if str(dst).endswith(".json"):
            raise OSError("disk full")
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OutboxCommandError, match="disk full"):
        store.write_manifest("pr", "broken", pr_manifest(), {"body.md": "Text"})
    assert snapshot(store.outbox("pr")) == {}


def test_list_show_and_drop_preserve_shared_bodies_and_metadata(tmp_path):
    store = store_for(tmp_path)
    store.require_writable({})
    directory = store.outbox("pr")
    for name in ("001-a.json", "002-b.json"):
        (directory / name).write_text(json.dumps(pr_manifest("shared.md")))
    (directory / "shared.md").write_text("Shared")
    (directory / "applied.log").write_text("receipt")
    (directory / "001-a.progress.json").write_text("{}")
    assert [(e.kind, e.name) for e in store.list()] == [
        ("pr", "001-a.json"),
        ("pr", "002-b.json"),
    ]
    assert all(e.summary for e in store.list())
    assert json.loads(store.show("pr", "001-a.json")) == pr_manifest("shared.md")
    store.drop("pr", "001-a.json")
    assert (directory / "shared.md").read_text() == "Shared"
    store.drop("pr", "002-b.json")
    assert not (directory / "shared.md").exists()
    assert (directory / "applied.log").read_text() == "receipt"
    assert (directory / "001-a.progress.json").exists()


@pytest.mark.parametrize(
    "body", ["applied.log", "002-keep.json", "../../outside", ".003-active.reserve"]
)
def test_drop_does_not_delete_metadata_or_escape(tmp_path, body):
    store = store_for(tmp_path)
    store.require_writable({})
    directory = store.outbox("pr")
    raw = {
        "version": 1,
        "repo": "o/r",
        "pr": 1,
        "actions": [{"type": "comment", "body_file": body}],
    }
    (directory / "001-drop.json").write_text(json.dumps(raw))
    (directory / "applied.log").write_text("receipt")
    (directory / "002-keep.json").write_text("{}")
    (directory / ".003-active.reserve").write_text("")
    outside = tmp_path / "outside"
    outside.write_text("safe")
    store.drop("pr", "001-drop.json")
    assert (directory / "applied.log").exists()
    assert (directory / "002-keep.json").exists()
    assert (directory / ".003-active.reserve").exists()
    assert outside.read_text() == "safe"


def test_store_reads_refuse_symlink_files(tmp_path):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    store.require_writable({})
    secret = tmp_path / "secret"
    secret.write_text("private")
    (store.outbox("pr") / "001-link.json").symlink_to(secret)
    with pytest.raises(OutboxCommandError):
        store.show("pr", "001-link.json")
    assert secret.read_text() == "private"


def test_drafts_persist_outside_outbox_and_submit_bodies(tmp_path):
    store = store_for(tmp_path)
    store.draft_save("pr", "one", pr_manifest(), {"body.md": "Saved"})
    assert store.drafts("pr") == tmp_path / ".jailbee/outbox-drafts/pr"
    assert not list(store.outbox("pr").iterdir())
    reloaded = store_for(tmp_path)
    assert reloaded.draft_load("pr", "one") == pr_manifest()
    name = reloaded.draft_submit("pr", "one", "submitted")
    assert reloaded.draft_load("pr", "one") is None
    from jailbee.pr_manifest import parse_manifest

    files = snapshot(store.outbox("pr"))
    assert parse_manifest(name, files[name], files).actions[0].body == "Saved"


def test_incomplete_draft_survives_failed_submit_and_can_be_discarded(tmp_path):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    store.draft_save("pr", "one", {"version": 1}, {})
    with pytest.raises(OutboxCommandError) as exc:
        store.draft_submit("pr", "one", "bad")
    assert exc.value.exit_code == 2
    assert store.draft_load("pr", "one") == {"version": 1}
    assert not list(store.outbox("pr").iterdir())
    store.draft_discard("pr", "one")
    assert store.draft_load("pr", "one") is None


@pytest.mark.parametrize("name", ["", "UPPER", "a_b", "a" * 41, "../outside"])
def test_draft_names_are_validated(tmp_path, name):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    for operation in (
        lambda: store.draft_save("pr", name, {}, {}),
        lambda: store.draft_load("pr", name),
        lambda: store.draft_submit("pr", name, "x"),
        lambda: store.draft_discard("pr", name),
    ):
        with pytest.raises(OutboxCommandError) as exc:
            operation()
        assert exc.value.exit_code == 2


@pytest.mark.parametrize("operation", ["write", "save", "submit", "discard", "drop"])
def test_all_mutations_recheck_child_role(tmp_path, monkeypatch, operation):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    store.draft_save("pr", "one", pr_manifest(), {"body.md": "Saved"})
    name = store.write_manifest("pr", "one", pr_manifest(), {"body.md": "Saved"})
    before = snapshot(store.outbox("pr"))
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    actions = {
        "write": lambda: store.write_manifest("pr", "x", {}, {}),
        "save": lambda: store.draft_save("pr", "one", {}, {}),
        "submit": lambda: store.draft_submit("pr", "one", "x"),
        "discard": lambda: store.draft_discard("pr", "one"),
        "drop": lambda: store.drop("pr", name),
    }
    with pytest.raises(OutboxCommandError) as exc:
        actions[operation]()
    assert exc.value.exit_code == 1
    assert snapshot(store.outbox("pr")) == before
    assert store.draft_load("pr", "one") == pr_manifest()


def test_replaced_outbox_ancestor_cannot_redirect_write(tmp_path, monkeypatch):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    store.require_writable({})
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = store.outbox("pr")
    held = directory.with_name("held")
    original = os.open
    changed = False

    def replace_after_open(path, flags, *args, **kwargs):
        nonlocal changed
        fd = original(path, flags, *args, **kwargs)
        if path == "pr-outbox" and not changed:
            changed = True
            directory.rename(held)
            directory.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(os, "open", replace_after_open)
    raw = {"version": 1, "repo": "o/r", "pr": 7, "actions": [{"type": "comment", "body": "Safe"}]}
    # require_writable pins the old directory then the actual writer refuses the replacement.
    with pytest.raises(OutboxCommandError):
        store.write_manifest("pr", "safe", raw, {})
    assert not list(outside.iterdir())
    assert not list(held.iterdir())


def test_draft_save_failure_preserves_previous_manifest_and_bodies(tmp_path, monkeypatch):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    raw = {
        "version": 1,
        "repo": "o/r",
        "pr": 7,
        "actions": [{"type": "comment", "body_file": "body.md"}],
    }
    store.draft_save("pr", "one", raw, {"body.md": "Original"})
    original = os.replace

    def fail(src, dst, *args, **kwargs):
        if dst == "one.json":
            raise OSError("disk full")
        return original(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        with pytest.raises(OutboxCommandError, match="disk full"):
            store.draft_save("pr", "one", raw, {"body.md": "Lost"})
    name = store.draft_submit("pr", "one", "one")
    text = json.loads(store.show("pr", name))
    body_name = text["actions"][0]["body_file"]
    assert (store.outbox("pr") / body_name).read_text() == "Original"
    assert not list(store.drafts("pr").iterdir())


def test_reload_append_save_preserves_all_body_files(tmp_path):
    from jailbee.pr_manifest import parse_manifest

    store = store_for(tmp_path)
    store.draft_save("pr", "batch", pr_manifest("first.md"), {"first.md": "First"})
    state = store.draft_load_state("pr", "batch")
    assert state is not None
    manifest, bodies = state
    manifest["actions"].append({"type": "comment", "body_file": "second.md"})
    bodies["second.md"] = "Second"
    store.draft_save("pr", "batch", manifest, bodies)
    state = store_for(tmp_path).draft_load_state("pr", "batch")
    assert state is not None
    assert state[1] == {"first.md": "First", "second.md": "Second"}
    name = store.draft_submit("pr", "batch", "batch")
    files = snapshot(store.outbox("pr"))
    assert [action.body for action in parse_manifest(name, files[name], files).actions] == [
        "First",
        "Second",
    ]


def test_concurrent_save_during_submit_survives(tmp_path, monkeypatch):
    import fcntl

    store = store_for(tmp_path)
    store.draft_save("pr", "batch", pr_manifest(), {"body.md": "Original"})
    submitting = Event()
    continue_submit = Event()
    save_boundary = Event()
    thread = local()
    original_write = store.write_manifest
    original_replace = os.replace
    original_flock = fcntl.flock

    def pause_write(*args, **kwargs):
        submitting.set()
        assert continue_submit.wait(5)
        return original_write(*args, **kwargs)

    def observe_lock(fd, operation):
        if getattr(thread, "saving", False) and operation == fcntl.LOCK_EX:
            save_boundary.set()
        return original_flock(fd, operation)

    def observe_replace(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        if getattr(thread, "saving", False) and dst == "batch.json":
            save_boundary.set()
        return result

    def save_newer():
        thread.saving = True
        store_for(tmp_path).draft_save("pr", "batch", pr_manifest(), {"body.md": "Newer"})

    monkeypatch.setattr(store, "write_manifest", pause_write)
    monkeypatch.setattr(fcntl, "flock", observe_lock)
    monkeypatch.setattr(os, "replace", observe_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted = pool.submit(store.draft_submit, "pr", "batch", "original")
        try:
            assert submitting.wait(5)
            saved = pool.submit(save_newer)
            assert save_boundary.wait(5)
        finally:
            continue_submit.set()
        name = submitted.result(timeout=5)
        saved.result(timeout=5)
    assert store.draft_load("pr", "batch") == pr_manifest()
    next_name = store_for(tmp_path).draft_submit("pr", "batch", "newer")
    from jailbee.pr_manifest import parse_manifest

    files = snapshot(store.outbox("pr"))
    assert parse_manifest(name, files[name], files).actions[0].body == "Original"
    assert parse_manifest(next_name, files[next_name], files).actions[0].body == "Newer"
