"""Container storage preserves validation, atomic publication and local boundaries."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, local

import pytest


@pytest.fixture(autouse=True)
def isolated_role(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAILBEE_ROLE", raising=False)


def issue_manifest(body_file="body.md"):
    return {
        "version": 1,
        "actions": [
            {"type": "create", "repo": ".", "ref": "new", "title": "Hi", "body_file": body_file}
        ],
    }


def store_for(home):
    from jailbee.container_outbox.store import Store

    return Store(home)


def snapshot(path):
    return {p.name: p.read_text() for p in path.iterdir()}


def test_store_import_does_not_load_host_modules():
    # An isolated interpreter prevents earlier host tests from hiding an import leak.
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", "import sys; import jailbee.container_outbox.store; "
         "assert 'jailbee.incus' not in sys.modules; "
         "assert 'jailbee.git' not in sys.modules; assert 'jailbee.tui' not in sys.modules"],
        capture_output=True, text=True,
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
    assert not store.outbox("issue").exists()
    assert not store.outbox("pr").exists()


@pytest.mark.parametrize("component", [".jailbee", ".jailbee/issue-outbox"])
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
    for kind in ("issue", "pr"):
        assert store.outbox(kind).stat().st_mode & 0o777 == 0o700


def test_issue_write_roundtrip_and_body_renaming(tmp_path):
    from jailbee.issue_manifest import parse_manifest

    store = store_for(tmp_path)
    manifest = issue_manifest()
    name = store.write_manifest("issue", "hello", manifest, {"body.md": "Text"})
    files = snapshot(store.outbox("issue"))
    assert name == "001-hello.json"
    raw = json.loads(files[name])
    body_name = raw["actions"][0]["body_file"]
    assert body_name.startswith("001-hello-") and body_name.endswith(".md")
    assert files[body_name] == "Text"
    assert parse_manifest(name, files[name], files).actions[0].body == "Text"
    assert manifest == issue_manifest()  # Never mutate a caller's draft.


def test_pr_write_roundtrips_host_parser_with_nested_bodies(tmp_path):
    from jailbee.pr_outbox import parse_manifest

    store = store_for(tmp_path)
    raw = {"version": 1, "repo": "o/r", "pr": 7, "actions": [
        {"type": "review", "body_file": "review.md", "comments": [
            {"path": "src/a.py", "line": 4, "body_file": "line.md"}
        ]}
    ]}
    name = store.write_manifest("pr", "review", raw, {"review.md": "Review", "line.md": "Line"})
    files = snapshot(store.outbox("pr"))
    parsed = parse_manifest(name, files[name], files)
    assert parsed.actions[0].body == "Review"
    assert parsed.actions[0].comments[0].body == "Line"


def test_invalid_manifest_leaves_home_unchanged(tmp_path):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    bad = {"version": 1, "actions": [
        {"type": "edit", "repo": ".", "issue": 1, "title": "New"}
    ]}
    with pytest.raises(OutboxCommandError, match="expected") as exc:
        store.write_manifest("issue", "bad", bad, {})
    assert exc.value.exit_code == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["../x", "/tmp/x", "a/b", "a\\b", "", ".", "..", "x\x00y"])
def test_unsafe_names_do_not_escape_store(tmp_path, name):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    with pytest.raises(OutboxCommandError) as exc:
        store.write_manifest("issue", name, issue_manifest(), {"body.md": "Text"})
    assert exc.value.exit_code == 2
    with pytest.raises(OutboxCommandError):
        store.show("issue", name)
    with pytest.raises(OutboxCommandError):
        store.drop("issue", name)


def test_next_name_is_exclusive(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    store.require_writable({})
    (store.outbox("issue") / "001-a.json").write_text("existing")
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
    name = store.write_manifest("issue", "hello", issue_manifest(), {"body.md": "Text"})
    assert collided
    assert name == "003-hello.json"
    assert (store.outbox("issue") / "001-a.json").read_text() == "existing"
    assert json.loads((store.outbox("issue") / name).read_text())["version"] == 1


def test_two_writers_publish_distinct_complete_manifests(tmp_path, monkeypatch):
    from jailbee.issue_manifest import parse_manifest

    store = store_for(tmp_path)
    store.require_writable({})
    barrier = Barrier(2)
    original = os.open
    writer = local()

    def compete(path, flags, *args, **kwargs):
        if flags & os.O_EXCL and str(path).endswith(".reserve") and not getattr(writer, "ready", False):
            # Both writers must compete for the first reservation, not the retry.
            writer.ready = True
            barrier.wait(timeout=5)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", compete)
    def write(body):
        return store_for(tmp_path).write_manifest("issue", "same", issue_manifest(), {"body.md": body})

    with ThreadPoolExecutor(max_workers=2) as pool:
        names = list(pool.map(write, ["First", "Second"]))
    assert set(names) == {"001-same.json", "002-same.json"}
    files = snapshot(store.outbox("issue"))
    assert {parse_manifest(n, files[n], files).actions[0].body for n in names} == {"First", "Second"}
    assert not any(n.startswith(".") for n in files)


def test_publication_is_body_first_and_manifest_is_never_empty(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    original = os.replace
    published = []

    def observe(src, dst, *args, **kwargs):
        files = snapshot(store.outbox("issue"))
        assert not any(n.endswith(".json") and not text for n, text in files.items())
        if str(dst).endswith(".json"):
            assert any(n.endswith(".md") for n in files)
        result = original(src, dst, *args, **kwargs)
        published.append(str(dst))
        return result

    monkeypatch.setattr(os, "replace", observe)
    store.write_manifest("issue", "atomic", issue_manifest(), {"body.md": "Text"})
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
        store.write_manifest("issue", "broken", issue_manifest(), {"body.md": "Text"})
    assert snapshot(store.outbox("issue")) == {}


def test_list_show_and_drop_preserve_shared_bodies_and_metadata(tmp_path):
    store = store_for(tmp_path)
    store.require_writable({})
    directory = store.outbox("issue")
    for name in ("001-a.json", "002-b.json"):
        (directory / name).write_text(json.dumps(issue_manifest("shared.md")))
    (directory / "shared.md").write_text("Shared")
    (directory / "applied.log").write_text("receipt")
    (directory / "001-a.progress.json").write_text("{}")
    assert [(e.kind, e.name) for e in store.list()] == [
        ("issue", "001-a.json"), ("issue", "002-b.json")
    ]
    assert all(e.summary for e in store.list())
    assert json.loads(store.show("issue", "001-a.json")) == issue_manifest("shared.md")
    store.drop("issue", "001-a.json")
    assert (directory / "shared.md").read_text() == "Shared"
    store.drop("issue", "002-b.json")
    assert not (directory / "shared.md").exists()
    assert (directory / "applied.log").read_text() == "receipt"
    assert (directory / "001-a.progress.json").exists()


@pytest.mark.parametrize("body", ["applied.log", "002-keep.json", "../../outside", ".003-active.reserve"])
def test_drop_does_not_delete_metadata_or_escape(tmp_path, body):
    store = store_for(tmp_path)
    store.require_writable({})
    directory = store.outbox("pr")
    raw = {"version": 1, "repo": "o/r", "pr": 1,
           "actions": [{"type": "comment", "body_file": body}]}
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
    (store.outbox("issue") / "001-link.json").symlink_to(secret)
    with pytest.raises(OutboxCommandError):
        store.show("issue", "001-link.json")
    assert secret.read_text() == "private"


def test_drafts_persist_outside_outbox_and_submit_bodies(tmp_path):
    store = store_for(tmp_path)
    store.draft_save("issue", "one", issue_manifest(), {"body.md": "Saved"})
    assert store.drafts("issue") == tmp_path / ".jailbee/outbox-drafts/issue"
    assert not list(store.outbox("issue").iterdir())
    reloaded = store_for(tmp_path)
    assert reloaded.draft_load("issue", "one") == issue_manifest()
    name = reloaded.draft_submit("issue", "one", "submitted")
    assert reloaded.draft_load("issue", "one") is None
    from jailbee.issue_manifest import parse_manifest
    files = snapshot(store.outbox("issue"))
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
        lambda: store.draft_save("issue", name, {}, {}),
        lambda: store.draft_load("issue", name),
        lambda: store.draft_submit("issue", name, "x"),
        lambda: store.draft_discard("issue", name),
    ):
        with pytest.raises(OutboxCommandError) as exc:
            operation()
        assert exc.value.exit_code == 2


@pytest.mark.parametrize("operation", ["write", "save", "submit", "discard", "drop"])
def test_all_mutations_recheck_child_role(tmp_path, monkeypatch, operation):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    store.draft_save("issue", "one", issue_manifest(), {"body.md": "Saved"})
    name = store.write_manifest("issue", "one", issue_manifest(), {"body.md": "Saved"})
    before = snapshot(store.outbox("issue"))
    monkeypatch.setenv("JAILBEE_ROLE", "child")
    actions = {
        "write": lambda: store.write_manifest("issue", "x", {}, {}),
        "save": lambda: store.draft_save("issue", "one", {}, {}),
        "submit": lambda: store.draft_submit("issue", "one", "x"),
        "discard": lambda: store.draft_discard("issue", "one"),
        "drop": lambda: store.drop("issue", name),
    }
    with pytest.raises(OutboxCommandError) as exc:
        actions[operation]()
    assert exc.value.exit_code == 1
    assert snapshot(store.outbox("issue")) == before
    assert store.draft_load("issue", "one") == issue_manifest()


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
    raw = {"version": 1, "repo": "o/r", "pr": 7,
           "actions": [{"type": "comment", "body": "Safe"}]}
    # require_writable pins the old directory then the actual writer refuses the replacement.
    with pytest.raises(OutboxCommandError):
        store.write_manifest("pr", "safe", raw, {})
    assert not list(outside.iterdir())
    assert not list(held.iterdir())


def test_draft_save_failure_preserves_previous_manifest_and_bodies(tmp_path, monkeypatch):
    from jailbee.container_outbox.store import OutboxCommandError

    store = store_for(tmp_path)
    raw = {"version": 1, "repo": "o/r", "pr": 7,
           "actions": [{"type": "comment", "body_file": "body.md"}]}
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
