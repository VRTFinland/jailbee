"""Source publication recovery with a fully mocked container store."""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from jailbee.outbox import io as store_io
from jailbee.outbox.models import StoreSnapshot
from jailbee.outbox_io import ContainerIdentity, JournalStore
from jailbee.pr_outbox import FinalizeError, Outbox, consume_manifest


@pytest.fixture
def source_store(mocker, tmp_path):
    identity = ContainerIdentity("c", "created")
    raw = json.dumps(
        {
            "version": 1,
            "repo": "acme/widgets",
            "pr": 42,
            "head_sha": None,
            "source": "refs/heads/topic",
            "source_sha": "a" * 40,
            "actions": [{"type": "description", "body_file": "body.md"}],
        }
    )
    files = {"m.json": raw, "body.md": "original"}
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "c", "created_at": "created"}]

    def read(*args, **kwargs):
        return Outbox(dict(files), identity=identity)

    def mutate(*args, expected, new_manifest, delete_names, **kwargs):
        assert expected == files
        if new_manifest:
            files[new_manifest[0]] = new_manifest[1]
        for name in delete_names:
            del files[name]
        return delete_names

    mocker.patch("jailbee.pr_outbox.read_outbox", side_effect=read)
    mutation = mocker.patch("jailbee.outbox.io.mutate_store", side_effect=mutate)
    return incus, files, read, mutate, mutation, JournalStore(tmp_path / "journals")


@pytest.mark.parametrize("zero", [False, True])
def test_failed_delete_has_inspectable_progress_and_cleanup_retry(source_store, mocker, zero):
    from jailbee.outbox.inspect import build_views

    incus, files, read, mutate, mutation, journals = source_store
    if zero:
        raw = json.loads(files["m.json"])
        raw["actions"] = []
        files["m.json"] = json.dumps(raw)
    original = read()

    def fail_delete(*args, **kwargs):
        if kwargs["delete_names"]:
            raise store_io.MutationExecutionError("delete failed")
        return mutate(*args, **kwargs)

    mutation.side_effect = fail_delete
    with pytest.raises(FinalizeError):
        consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=original)
    store = StoreSnapshot("pr", tuple(sorted(files.items())), (), ())
    view = build_views(original.identity, (store,), journal_store=journals)[0]
    assert view.state == "applied"
    assert view.error is None
    assert all(a.state == "applied" and a.receipt == "url" for a in view.actions)
    mutation.side_effect = mutate
    consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    assert set(files) == ({"applied.log", "body.md"} if zero else {"applied.log"})
    assert files["applied.log"].count("m.json") == 1
    incus.exec.assert_not_called()


@pytest.mark.parametrize("damage", ["lost", "partial", "replacement", "error"])
def test_receipt_write_requires_exact_readback_before_delete(source_store, damage):
    incus, files, read, mutate, mutation, _ = source_store

    def damage_write(*args, **kwargs):
        result = mutate(*args, **kwargs)
        replacement = kwargs["new_manifest"]
        if replacement and replacement[0] == "applied.log":
            if damage == "lost":
                del files["applied.log"]
            elif damage == "partial":
                files["applied.log"] = replacement[1][:-4]
            elif damage == "replacement":
                files["applied.log"] = "replaced\n"
            else:
                raise store_io.MutationExecutionError("write failed")
        return result

    mutation.side_effect = damage_write
    with pytest.raises(FinalizeError):
        consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    assert "m.json" in files and "body.md" in files
    assert not any(c.kwargs["delete_names"] for c in mutation.call_args_list)


def test_source_progress_preflight_exact_boundary_and_maximum_url(source_store):
    from jailbee.pr_outbox import (
        SOURCE_RECEIPT_URL_BYTES,
        ManifestError,
        _source_progress_payload,
        preflight_source_progress,
    )

    _, files, read, _, _, _ = source_store
    # Three copies: action URL, source URL, and the URL inside the receipt line.
    maximum_url = "x" * (SOURCE_RECEIPT_URL_BYTES - 2)
    size = len(_source_progress_payload(read(), "m.json", maximum_url).encode())
    files["m.json"] += " " * (store_io.FILE_LIMIT - size)
    payload = _source_progress_payload(read(), "m.json", maximum_url)
    assert len(payload.encode()) == store_io.FILE_LIMIT
    preflight_source_progress(read(), "m.json")
    # Escaping can only reduce available literal characters for the same budget.
    escaped_url = "\\" * ((SOURCE_RECEIPT_URL_BYTES - 2) // 2)
    assert len(json.dumps(escaped_url).encode()) == SOURCE_RECEIPT_URL_BYTES
    assert len(_source_progress_payload(read(), "m.json", escaped_url).encode()) == len(
        payload.encode()
    )
    with pytest.raises(ManifestError, match="URL exceeds"):
        _source_progress_payload(read(), "m.json", maximum_url + "x")
    files["m.json"] += " "
    with pytest.raises(ManifestError, match="recovery progress exceeds"):
        preflight_source_progress(read(), "m.json")


@pytest.mark.parametrize("bad", [None, [], {"identity": []}, {"inputs": []}])
def test_malformed_source_progress_is_refused(source_store, bad):
    incus, files, read, _, mutation, _ = source_store
    files["m.json.progress.json"] = json.dumps(
        {"applied": [0], "urls": {"0": "url"}, "source": bad}
    )
    with pytest.raises(FinalizeError):
        consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    mutation.assert_not_called()


def test_inspection_refuses_receipt_from_replaced_container(source_store):
    from jailbee.outbox.inspect import build_views

    incus, files, read, mutate, mutation, journals = source_store

    def fail_delete(*args, **kwargs):
        if kwargs["delete_names"]:
            raise store_io.MutationExecutionError("delete failed")
        return mutate(*args, **kwargs)

    mutation.side_effect = fail_delete
    with pytest.raises(FinalizeError):
        consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    store = StoreSnapshot("pr", tuple(sorted(files.items())), (), ())
    view = build_views(ContainerIdentity("c", "replacement"), (store,), journal_store=journals)[0]
    assert view.state == "uncertain"
    assert "another container" in view.error


def test_source_receipt_preserves_nonterminated_history(source_store):
    incus, files, read, _, _, _ = source_store
    files["applied.log"] = "previous receipt"
    consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    assert files["applied.log"].startswith("previous receipt\n")
    assert len(files["applied.log"].splitlines()) == 2


@pytest.mark.parametrize("removed", [1, 2])
def test_partial_cleanup_retains_recoverable_inputs(source_store, removed):
    incus, files, read, mutate, mutation, _ = source_store

    def partial(*args, **kwargs):
        names = kwargs["delete_names"]
        if names:
            assert names == ("body.md", "m.json", "m.json.progress.json")
            for name in names[:removed]:
                del files[name]
            raise store_io.MutationExecutionError("partial", names[:removed])
        return mutate(*args, **kwargs)

    mutation.side_effect = partial
    with pytest.raises(FinalizeError):
        consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    assert "m.json.progress.json" in files
    from jailbee.outbox.inspect import build_views

    store = StoreSnapshot("pr", tuple(sorted(files.items())), (), ())
    views = build_views(read().identity, (store,), journal_store=source_store[-1])
    assert len(views) == 1 and views[0].state == "applied"
    mutation.side_effect = mutate
    consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    assert set(files) == {"applied.log"}


@pytest.mark.parametrize("change", ["body", "manifest", "identity", "shared"])
def test_cleanup_recovery_never_deletes_changed_or_shared_inputs(source_store, change):
    incus, files, read, mutate, mutation, _ = source_store

    def fail(*args, **kwargs):
        if kwargs["delete_names"]:
            raise store_io.MutationExecutionError("failed")
        return mutate(*args, **kwargs)

    mutation.side_effect = fail
    with pytest.raises(FinalizeError):
        consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    mutation.side_effect = mutate
    if change == "body":
        files["body.md"] = "changed"
    elif change == "manifest":
        files["m.json"] += " "
    elif change == "identity":
        original_read = read

        def read():
            return replace(original_read(), identity=ContainerIdentity("c", "replacement"))
    else:
        neighbor = json.loads(files["m.json"])
        neighbor.pop("source")
        neighbor.pop("source_sha")
        neighbor["actions"] = [{"type": "comment", "body_file": "body.md"}]
        files["neighbor.json"] = json.dumps(neighbor)
    if change == "shared":
        consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    else:
        with pytest.raises(FinalizeError):
            consume_manifest(incus, "c", "m.json", "url", uid=1000, preview=read())
    assert "body.md" in files
