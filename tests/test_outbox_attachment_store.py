import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from jailbee.outbox.attachment_store import MAX_CHUNK, AttachmentError, AttachmentStore

DATA = b"\x89PNG\r\n\x1a\ncontent"


def begin(store, data=DATA, name="image.png", content_type="image/png", digest=None):
    return store.begin(
        "owner", name, content_type, len(data), digest or hashlib.sha256(data).hexdigest(), 1
    )


def test_capture_image(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    data = b"\x89PNG\r\n\x1a\ncontent"
    blob = store.begin(
        "owner", "image.png", "image/png", len(data), hashlib.sha256(data).hexdigest(), 1
    )
    store.append("owner", blob, 0, data, 2)
    descriptor = store.finish("owner", blob, 3)
    assert descriptor == {
        "blob": blob,
        "name": "image.png",
        "content_type": "image/png",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert b"".join(store.iter_bytes("owner", blob)) == data
    assert store.finish("owner", blob, 4) == descriptor


def test_collection_and_transactional_pins(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    blob = begin(store)
    store.append("owner", blob, 0, DATA, 2)
    descriptor = store.finish("owner", blob, 3)
    with pytest.raises(RuntimeError):
        with store.transaction() as con:
            store.pin(con, "owner", "issue", "proposal", blob, descriptor)
            raise RuntimeError("rollback")
    with store.transaction() as con:
        with pytest.raises(AttachmentError):
            store.pin(con, "owner", "issue", "proposal", blob, {**descriptor, "name": "bad.png"})
        assert store.pin(con, "owner", "issue", "proposal", blob, descriptor) == descriptor
    assert store.collect(100_000) == 0
    assert store.metadata("owner", blob) == descriptor
    with store.transaction() as con:
        store.unpin(con, "owner", "issue", "proposal")
    assert store.collect(100_000) == 1
    with pytest.raises(AttachmentError):
        store.metadata("owner", blob)


def test_concurrent_finish_returns_same_descriptor(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    blob = begin(store)
    store.append("owner", blob, 0, DATA, 2)
    gate = Barrier(2)

    def finish():
        gate.wait(timeout=5)
        return store.finish("owner", blob, 3)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(finish) for _ in range(2)]
        assert futures[0].result(timeout=10) == futures[1].result(timeout=10)


def test_pin_rollback_leaves_capture_collectible(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    blob = begin(store)
    store.append("owner", blob, 0, DATA, 2)
    store.finish("owner", blob, 3)
    with pytest.raises(RuntimeError):
        with store.transaction() as con:
            store.pin(con, "owner", "issue", "proposal", blob)
            raise RuntimeError("rollback")
    assert store.collect(100_000) == 1


def test_collection_expires_pending_and_respects_activity(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    old = begin(store)
    fresh = begin(store)
    store.append("owner", fresh, 0, DATA[:2], 90_000)
    assert store.collect(100_000) == 1
    with pytest.raises(AttachmentError):
        store.append("owner", old, 0, DATA, 100_001)
    store.append("owner", fresh, 2, DATA[2:], 100_001)
    store.finish("owner", fresh, 100_002)


def test_name_must_match_mime(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    with pytest.raises(AttachmentError):
        begin(store, name="photo.jpg")
    begin(store, name="photo.PNG")


def test_ownership_order_and_immutability(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    blob = begin(store)
    with pytest.raises(AttachmentError):
        store.append("other", blob, 0, DATA, 1)
    with pytest.raises(AttachmentError):
        store.append("owner", blob, 1, DATA, 1)
    with pytest.raises(AttachmentError):
        store.metadata("owner", blob)
    with pytest.raises(AttachmentError):
        list(store.iter_bytes("owner", blob))
    store.append("owner", blob, 0, DATA[:4], 2)
    store.append("owner", blob, 4, DATA[4:], 3)
    store.finish("owner", blob, 4)
    for operation in (
        lambda: store.metadata("other", blob),
        lambda: store.finish("other", blob, 5),
        lambda: list(store.iter_bytes("other", blob)),
        lambda: store.append("owner", blob, len(DATA), b"x", 5),
    ):
        with pytest.raises(AttachmentError):
            operation()


@pytest.mark.parametrize("name", ["", ".", "..", "../x", "x\\y", "a\n.png", "x\x00.png"])
def test_rejects_unsafe_names(tmp_path, name):
    with pytest.raises(AttachmentError):
        begin(AttachmentStore(tmp_path / "broker.db"), name=name)


def test_bad_declarations_and_content(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    for size in (0, 10_000_001, -1):
        with pytest.raises(AttachmentError):
            store.begin("owner", "x.png", "image/png", size, "0" * 64, 1)
    with pytest.raises(AttachmentError):
        begin(store, content_type="image/svg+xml")
    with pytest.raises(AttachmentError):
        begin(store, digest="not a hash")
    blob = begin(store, digest="0" * 64)
    with pytest.raises(AttachmentError):
        store.finish("owner", blob, 1)
    store.append("owner", blob, 0, DATA, 2)
    with pytest.raises(AttachmentError):
        store.finish("owner", blob, 3)
    blob = begin(store, name="image.jpg", content_type="image/jpeg")
    store.append("owner", blob, 0, DATA, 2)
    with pytest.raises(AttachmentError):
        store.finish("owner", blob, 3)
    with pytest.raises(AttachmentError):
        store.append("owner", blob, 0, b"x" * (MAX_CHUNK + 1), 3)


def test_no_image_count_limit(tmp_path):
    store = AttachmentStore(tmp_path / "broker.db")
    for _ in range(11):
        blob = begin(store, name="diagram (1) #.png")
        store.append("owner", blob, 0, DATA, 2)
        store.finish("owner", blob, 3)
        assert store.metadata("owner", blob)["name"] == "diagram (1) #.png"
